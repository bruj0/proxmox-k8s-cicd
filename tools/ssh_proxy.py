"""ssh_proxy -- interactive shell into a cluster VM through the PVE jump host.

Day-to-day operator entry point. Reads `infra/clusters/<name>/output.json`
to find the SDN IP, builds the ProxyCommand-tunnelled argv, and hands
control of the terminal to ssh via `os.execvp` (so the operator's TTY,
resize signals, and ~/.ssh/config aliases all behave the same as
a plain `ssh user@host`).

Usage examples:
  # SSH into the first control-plane VM of the cicd cluster
  python -m tools.ssh_proxy --cluster cicd

  # SSH into the first worker
  python -m tools.ssh_proxy --cluster cicd --role worker

  # Run a one-off command non-interactively
  python -m tools.ssh_proxy --cluster apps --role control_plane -- hostname

  # Forward the k8s apiserver to a local port (great for k9s).
  # Bare --port-forward (no value) defaults to local 6443 -> first CP's
  # loopback 6443, the canonical k3s apiserver forward.
  python -m tools.ssh_proxy --cluster cicd --port-forward

  # Custom forward (e.g. to a non-standard local port)
  python -m tools.ssh_proxy --cluster cicd --port-forward 6444:127.0.0.1:6443

  # Multiple forwards (one bare, one explicit)
  python -m tools.ssh_proxy --cluster cicd --port-forward \
      --port-forward 9001:127.0.0.1:9001

Why a CLI and not just a shell alias: the operator's machine is on
10.x.x.x, NOT on the SDN. The cluster VMs (10.0.0.50-200) need a
double-ssh through PVE. This script bakes in:
  - the right jump host (root@kvm.bruj0.net -p 6022)
  - the right user (`ubuntu`, because root login is rejected on the
    cloud image -- see step 4a.3.5 of the skill)
  - the ProxyCommand quoting (see step 4a.3.8 -- `-W` alone is wrong
    for exec targets; the ProxyCommand form is mandatory)

When `--port-forward` is given, an `ssh -L` tunnel is opened in the
background and the script blocks on a foreground `ssh` exec target.
Ctrl-C kills the foreground ssh; the background tunnel is left running
on purpose (use `lsof -iTCP:6443 -sTCP:LISTEN` to find it; pid is
printed at startup). The forwarded port makes the apiserver reachable
at `https://127.0.0.1:<local_port>` for kubectl/k9s without exposing
any host port on PVE.

This tool does NOT modify cluster state. It is read-only with respect
to the bootstrap pipeline; safe to run any time.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Sequence

from tools.lib.log import StructuredLogger
from tools.lib.pve_ssh import PveSshProxy
from tools.lib.repo_locator import RepoNotFoundError, locate_repo_root
from tools.lib.talos_client import ClusterTopology


@dataclass(frozen=True)
class ParsedForward:
    """`<local_port>:[<remote_bind>]:<remote_port>` triple.

    Same shape kubectl/SSH use for the `-L` spec; a missing remote_bind
    defaults to `127.0.0.1` (because k3s binds the apiserver on the
    CP node's loopback pre-kube-vip).
    """

    local_port: int
    remote_bind: str
    remote_port: int

    @classmethod
    def parse(cls, spec: str) -> "ParsedForward":
        parts = spec.split(":")
        if len(parts) == 2:
            local_s, remote_s = parts
            remote_bind = "127.0.0.1"
        elif len(parts) == 3:
            local_s, remote_bind, remote_s = parts
        else:
            raise ValueError(
                f"--port-forward expects <local_port>:<remote_port> or "
                f"<local_port>:<remote_bind>:<remote_port>, got {spec!r}"
            )
        try:
            return cls(
                local_port=int(local_s),
                remote_bind=remote_bind,
                remote_port=int(remote_s),
            )
        except ValueError as exc:
            raise ValueError(
                f"non-integer port in --port-forward {spec!r}: {exc}"
            ) from exc


def _resolve_target(
    topo: ClusterTopology,
    role: str | None,
    name: str | None,
) -> dict[str, str]:
    """Pick exactly one VM from the topology based on --role/--name.

    When both are given, --name wins. When neither is given, the
    first control-plane VM is used (the canonical place to land
    when triaging a cluster).
    """
    if name:
        for n in topo.all_nodes:
            if n["name"] == name:
                return dict(n)
        raise SystemExit(
            f"no node named {name!r} in cluster {topo.name!r}; "
            f"available: {[n['name'] for n in topo.all_nodes]}"
        )
    if role in (None, "control_plane", "control-plane"):
        if not topo.control_plane:
            raise SystemExit(
                f"cluster {topo.name!r} has no control-plane VMs in output.json"
            )
        return dict(topo.control_plane[0])
    if role in ("worker",):
        if not topo.worker:
            raise SystemExit(
                f"cluster {topo.name!r} has no worker VMs in output.json"
            )
        return dict(topo.worker[0])
    raise SystemExit(
        f"unknown --role {role!r}; expected control_plane or worker"
    )


def _build_argv(
    proxy: PveSshProxy,
    target_ip: str,
    command: str | None,
    extra_port_forwards: Sequence[ParsedForward] = (),
) -> list[str]:
    """Build the final ssh argv.

    For interactive shells we want ProxyCommand + a tty. For port
    forwards we add `-L` flags (multiple). For one-off commands we
    append them after `--`.
    """
    base = proxy.ssh_argv(target_ip)
    argv: list[str] = []
    # Strip the trailing `ubuntu@<ip>` token (last) from base so we
    # can splice in `-L` flags BEFORE the destination hop (ssh
    # requires that ordering).
    argv.extend(base[:-1])
    for fwd in extra_port_forwards:
        argv.extend(
            [
                "-L",
                f"{fwd.local_port}:{fwd.remote_bind}:{fwd.remote_port}",
            ]
        )
    argv.extend(
        [
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=4",
        ]
    )
    argv.append(base[-1])  # `ubuntu@<ip>` back on the end
    if command is not None:
        argv.append(command)
    return argv


def _start_port_forward(
    proxy: PveSshProxy,
    target_ip: str,
    fwd: ParsedForward,
) -> "subprocess.Popen[bytes]":
    """Open an `ssh -L` tunnel; return the background Popen.

    Lives for the life of the script; killed by the atexit handler
    we register in main() so Ctrl-C of the foreground ssh leaves
    the tunnel up (intentional -- you can keep your k9s session).
    """
    argv = proxy.ssh_argv(target_ip, command="true")  # placeholder dest
    # Replace the placeholder `true` with `-N -L` flags. ssh puts
    # flags BEFORE the destination; "true" was the destination.
    argv = argv[:-2] + [
        "-N",
        "-L",
        f"{fwd.local_port}:{fwd.remote_bind}:{fwd.remote_port}",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=4",
    ]
    proc = subprocess.Popen(  # noqa: S603 -- forward-only tunnel.
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    return proc


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tools.ssh_proxy",
        description=(
            "SSH into a cluster VM through the PVE jump host. "
            "Optional --port-forward opens a k8s apiserver tunnel."
        ),
    )
    parser.add_argument(
        "--cluster", required=True, help="cluster name (cicd or apps)"
    )
    parser.add_argument(
        "--role",
        choices=("control_plane", "worker"),
        help="node role to land on (default: first control plane)",
    )
    parser.add_argument(
        "--name",
        help=(
            "exact node name (e.g. cicd-cp-1) -- overrides --role. "
            "Run with no --role/--name to land on the first CP."
        ),
    )
    parser.add_argument(
        "--repo-root",
        default=None,
        help=(
            "repo root containing infra/clusters/<name>/output.json. "
            "Defaults to PROXMOX_K8S_REPO, then the current working "
            "directory, then walks up looking for infra/clusters/."
        ),
    )
    parser.add_argument(
        "--port-forward",
        action="append",
        nargs="?",
        const="6443:127.0.0.1:6443",
        default=[],
        metavar="LOCAL[:BIND]:REMOTE",
        help=(
            "open an ssh -L tunnel before exec'ing the foreground ssh. "
            "Pass the flag with no value to use the default k3s apiserver "
            "tunnel (local 6443 -> first CP's loopback 6443). Pass a "
            "value (e.g. 6444:127.0.0.1:6443) for a custom forward. "
            "Repeatable. Bind defaults to 127.0.0.1 (k3s binds the "
            "apiserver on the CP node's loopback pre-kube-vip)."
        ),
    )
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help=(
            "optional command to run non-interactively. Anything after "
            "the first positional is passed verbatim to ssh; use -- to "
            "separate it from the ssh_proxy flags."
        ),
    )
    args = parser.parse_args(argv)
    logger = StructuredLogger("ssh_proxy")
    try:
        repo_root = locate_repo_root(flag_value=args.repo_root)
    except RepoNotFoundError as exc:
        logger.error(
            "ssh_proxy.repo_not_found",
            error=str(exc),
            resolution=(
                "pass --repo-root <path> or set PROXMOX_K8S_REPO; "
                "see the error message for the searched locations"
            ),
        )
        return 1
    cluster_dir = repo_root / "infra" / "clusters" / args.cluster
    output_json = cluster_dir / "output.json"
    if not output_json.exists():
        logger.error(
            "ssh_proxy.no_output",
            error="output.json missing",
            resolution=(
                "run `python scripts/apply_tofu.py <cluster>` to "
                "generate output.json first"
            ),
            cluster=args.cluster,
            expected=str(output_json),
        )
        return 1
    topo = ClusterTopology.from_output_json(output_json)
    target = _resolve_target(topo, args.role, args.name)
    target_ip = target["ip"]
    logger.info(
        "ssh_proxy.connect",
        cluster=args.cluster,
        node=target["name"],
        ip=target_ip,
        port_forwards=list(args.port_forward),
    )
    proxy = PveSshProxy(logger=logger)
    forwards = [ParsedForward.parse(s) for s in args.port_forward]
    bg_procs: list[subprocess.Popen[bytes]] = []
    for fwd in forwards:
        bg = _start_port_forward(proxy, target_ip, fwd)
        bg_procs.append(bg)
        print(
            f"[ssh_proxy] forward: 127.0.0.1:{fwd.local_port} -> "
            f"{target['name']}:{fwd.remote_bind}:{fwd.remote_port} "
            f"(pid={bg.pid})",
            file=sys.stderr,
        )

    # Build the foreground argv. For an interactive shell, command is
    # None. For a one-off, the user passed a command after `--`.
    command: str | None = None
    if args.command:
        # argparse.REMAINDER keeps the leading `--` if the user used
        # it; strip it.
        command_parts = list(args.command)
        if command_parts and command_parts[0] == "--":
            command_parts = command_parts[1:]
        command = " ".join(command_parts) if command_parts else None

    final_argv = _build_argv(proxy, target_ip, command)

    if not bg_procs:
        # Pure interactive (or one-off) shell -- replace the current
        # process with ssh so the operator's tty behaves naturally.
        try:
            os.execvp(final_argv[0], final_argv)
        except OSError as exc:
            logger.error(
                "ssh_proxy.exec_failed",
                error=str(exc),
                resolution="check that `ssh` is on PATH and the jump host is reachable",
                argv=final_argv,
            )
            return 1
        return 0  # unreachable

    # With background port-forwards we stay in the parent so we can
    # clean up on exit. Re-execing would orphan the bg processes.
    try:
        rc = subprocess.call(final_argv)  # noqa: S603
    finally:
        for bg in bg_procs:
            if bg.poll() is None:
                bg.terminate()
                try:
                    bg.wait(timeout=2)
                except Exception:
                    bg.kill()
    return int(rc)


if __name__ == "__main__":
    sys.exit(main())
