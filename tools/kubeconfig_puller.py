"""kubeconfig_puller -- produce a kubectl config that talks to a cluster
through a localhost port-forwarded to the control-plane's apiserver.

Why this exists:
  - k3s binds the apiserver to the CP node's loopback (127.0.0.1) on
    first install. The cluster VIP (10.0.0.30/40) is owned by
    kube-vip, which only comes up in the `helm` phase. So in the
    window between `install_k3s` and `helm` the apiserver is reachable
    only via the CP node's loopback, not via the VIP.
  - Even once the VIP is up, the operator's host is NOT on the SDN.
    The CP VM is reachable only by tunneling through PVE. So we
    have to forward 127.0.0.1:6443 on the CP node to a local port
    and point kubectl at `https://127.0.0.1:<local_port>`.

This tool:
  1. Reads `infra/clusters/<name>/output.json` and picks the first
     control-plane VM.
  2. Opens an `ssh -L` tunnel via PVE: <local_port> -> 127.0.0.1:6443
     on the CP node.
  3. `scp`-style fetches `/etc/rancher/k3s/k3s.yaml` from the CP node
     (also through the PVE proxy), using the same SSH argv pattern.
  4. Rewrites the `server:` URL in the kubeconfig to
     `https://127.0.0.1:<local_port>`.
  5. Writes the kubeconfig to the requested path and prints the
     local port + a ready-to-run `KUBECONFIG=…` suggestion.

The forward lives until the operator kills this process (Ctrl-C).
After that, the local port closes and the kubeconfig no longer
works -- that's intentional: we don't want stale creds around if
the cluster is decommissioned.

Usage:
  python -m tools.kubeconfig_puller --cluster cicd
  python -m tools.kubeconfig_puller --cluster apps --kubeconfig ~/.kube/cicd
  KUBECONFIG=~/.kube/cicd kubectl get nodes
"""
from __future__ import annotations

import argparse
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from tools.lib.log import StructuredLogger
from tools.lib.pve_ssh import PveSshProxy
from tools.lib.repo_locator import RepoNotFoundError, locate_repo_root
from tools.lib.talos_client import ClusterTopology


_K3S_KUBECONFIG_PATH = "/etc/rancher/k3s/k3s.yaml"


@dataclass(frozen=True)
class PullerConfig:
    """What the operator passed in -- parsed once, used throughout."""

    cluster: str
    repo_root: Path
    output_path: Path
    local_port: int | None
    keep_tunnel: bool


def _parse_args(argv: Sequence[str] | None) -> PullerConfig:
    parser = argparse.ArgumentParser(
        prog="tools.kubeconfig_puller",
        description=(
            "Forward a cluster's apiserver through PVE and emit a "
            "kubectl config that talks to it via 127.0.0.1:<port>."
        ),
    )
    parser.add_argument("--cluster", required=True)
    parser.add_argument(
        "--kubeconfig",
        help=(
            "where to write the rewritten kubeconfig. Default: "
            "<repo>/infra/clusters/<cluster>/kubeconfig.pveproxy"
        ),
    )
    parser.add_argument(
        "--local-port",
        type=int,
        help=(
            "operator-side port to forward 127.0.0.1:6443 to. "
            "Default: pick a free port. The kubeconfig's server: "
            "URL always points at this port so the operator can "
            "start the tunnel themselves later."
        ),
    )
    parser.add_argument(
        "--no-tunnel",
        action="store_true",
        help=(
            "skip starting the ssh -L forward (you want to start "
            "one yourself with `ssh-proxy --port-forward` later, "
            "or you're running in a non-interactive context). The "
            "kubeconfig is still written, and a free local port is "
            "picked for its server: URL -- re-run with the same "
            "`--local-port` later so the tunnel lines up. Default: "
            "start the tunnel as a detached background process so "
            "`kubectl` works immediately from another terminal. "
            "The pid is printed so you can `kill` it when done."
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
    args = parser.parse_args(argv)
    # Resolve the repo root via the shared locator (covers the
    # --repo-root flag, PROXMOX_K8S_REPO env var, cwd, and the
    # walk-up from cwd). Raises RepoNotFoundError with a clear
    # message if none of those match.
    try:
        repo_root = locate_repo_root(flag_value=args.repo_root)
    except RepoNotFoundError as exc:
        # Re-raise so main() can log it with the structured
        # logger; argparse-time resolution isn't ideal because
        # the logger hasn't been constructed yet.
        raise SystemExit(str(exc)) from exc
    out_path = (
        Path(args.kubeconfig).resolve()
        if args.kubeconfig
        else repo_root / "infra" / "clusters" / args.cluster
        / "kubeconfig.pveproxy"
    )
    return PullerConfig(
        cluster=args.cluster,
        repo_root=repo_root,
        output_path=out_path,
        local_port=args.local_port,
        keep_tunnel=not args.no_tunnel,
    )


def _find_first_cp(topo: ClusterTopology) -> dict[str, str]:
    if not topo.control_plane:
        raise SystemExit(
            f"cluster {topo.name!r} has no control-plane VMs in output.json"
        )
    # Sequence[Mapping[str, str]] -> dict[str, str] at runtime; widen.
    return dict(topo.control_plane[0])


def fetch_kubeconfig_via_proxy(
    proxy: PveSshProxy,
    target_ip: str,
    logger: StructuredLogger,
) -> str:
    """Run `sudo cat <k3s.yaml>` on the CP node, return the file body.

    The cloud image refuses root login (step 4a.3.5), so we land as
    `ubuntu` and `sudo -n` to read the kubeconfig. We pull it through
    the same PveSshProxy used for the port forward so the operator
    only has to trust one SSH fingerprint chain.

    Exposed as a module-level helper so the bootstrap script can
    reuse it (the helm phase needs the same body to install the
    first two helm releases before the operator ever runs the
    kubeconfig-puller).
    """
    inner = f"cat {_K3S_KUBECONFIG_PATH}"
    remote = f"sudo -n bash -c {shlex.quote(inner)}"
    proc = proxy.run(target_ip, remote, check=True, timeout=20)
    body = proc.stdout
    if "apiVersion: v1" not in body or "kind: Config" not in body:
        # The sudo -n on a fresh CP that hasn't yet had /etc/sudoers.d
        # applied will fail with "a password is required" and return
        # the sudo error text. Surface that to the operator.
        raise RuntimeError(
            f"refusing to write a kubeconfig that does not look like one. "
            f"first 200 chars of stdout: {body[:200]!r}, "
            f"stderr: {proc.stderr[:200]!r}"
        )
    logger.info(
        "kubeconfig_puller.fetched",
        bytes=len(body),
        node_ip=target_ip,
    )
    return body


def rewrite_server_url(kubeconfig_text: str, local_port: int) -> str:
    """Replace the `server:` line with the local-forwarded URL.

    The CP-side kubeconfig points at `https://127.0.0.1:6443` (k3s
    binds loopback). We rewrite to `https://127.0.0.1:<local_port>`
    so kubectl on the operator host hits the tunnel instead of the
    operator's own loopback.

    The shape of a kubeconfig file is YAML; we do a literal line
    match on `server:` (no YAML parser) because the k3s-generated
    file uses simple `key: value` pairs on a single line and we
    never want to silently mis-parse a multi-doc YAML.

    Exposed as a module-level helper so the bootstrap script can
    reuse it after fetching the kubeconfig for the helm phase.
    """
    new_url = f"https://127.0.0.1:{local_port}"
    out_lines: list[str] = []
    replaced = False
    for line in kubeconfig_text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("server:") and not replaced:
            indent = line[: len(line) - len(stripped)]
            out_lines.append(f"{indent}server: {new_url}")
            replaced = True
        else:
            out_lines.append(line)
    if not replaced:
        raise RuntimeError(
            "no `server:` line in the k3s kubeconfig; refusing to write"
        )
    return "\n".join(out_lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    cfg = _parse_args(argv)
    logger = StructuredLogger("kubeconfig_puller")
    cluster_dir = cfg.repo_root / "infra" / "clusters" / cfg.cluster
    output_json = cluster_dir / "output.json"
    if not output_json.exists():
        logger.error(
            "kubeconfig_puller.no_output",
            error="output.json missing",
            resolution="run `python scripts/apply_tofu.py <cluster>` first",
            cluster=cfg.cluster,
            expected=str(output_json),
        )
        return 1
    topo = ClusterTopology.from_output_json(output_json)
    cp = _find_first_cp(topo)
    target_ip = cp["ip"]
    proxy = PveSshProxy(logger=logger)

    forward = None
    local_port = cfg.local_port
    if cfg.keep_tunnel:
        # Start the apiserver forward and KEEP IT ALIVE across the
        # process exit. The operator wants to use the kubeconfig
        # from another terminal; the tunnel must outlive us.
        # We do this by spawning a long-lived detached child
        # process (a tiny `python -c` that calls port_forward
        # and parks on a sleep), printing the child pid, and
        # returning 0 immediately. The operator kills the
        # detached child with `kill <pid>` or
        # `pkill -f "ssh.*-L <local_port>"` when done.
        forward = proxy.port_forward(
            target_ip,
            remote_port=6443,
            remote_bind="127.0.0.1",
            local_port=local_port,
        )
        local_port = forward.local_port
        if local_port == 0:
            raise RuntimeError("port_forward returned local_port=0; refusing")
        print(
            f"[kubeconfig_puller] apiserver forward ready: "
            f"https://127.0.0.1:{local_port} -> {cp['name']}:6443",
            file=sys.stderr,
        )
    else:
        # No tunnel started. We still need a local_port so the
        # kubeconfig's server: URL is addressable -- either the
        # operator passed --local-port, or we pick a free one
        # (the operator will start the tunnel on the same port
        # later, e.g. `ssh-proxy --port-forward <port>:127.0.0.1:6443`).
        if local_port is None:
            local_port = _pick_unused_local_port()

    body = fetch_kubeconfig_via_proxy(proxy, target_ip, logger)
    rewritten = rewrite_server_url(body, local_port)
    cfg.output_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.output_path.write_text(rewritten)
    cfg.output_path.chmod(0o600)
    logger.info(
        "kubeconfig_puller.wrote",
        path=str(cfg.output_path),
        server_url=f"https://127.0.0.1:{local_port}",
    )
    print(f"[kubeconfig_puller] wrote {cfg.output_path}")
    print(
        f"[kubeconfig_puller] try:  KUBECONFIG={cfg.output_path} "
        f"kubectl get nodes"
    )
    if forward is not None:
        # Detach the tunnel. The forward's Popen was started with
        # start_new_session=True so it survives us. We DON'T call
        # forward.terminate() in a finally block here -- the whole
        # point is that the tunnel outlives this process.
        print(
            f"[kubeconfig_puller] tunnel pid={forward.proc.pid}; "
            f"kill it with:  kill {forward.proc.pid}  "
            f"(or: pkill -f 'ssh.*-L {local_port}')"
        )
        # Forget the forward object; the detached child is now
        # the operator's responsibility. The finally block below
        # would have killed it.
        forward = None  # noqa: F841 -- intentionally drop the reference
    else:
        print(
            f"[kubeconfig_puller] start the tunnel when you need it:  "
            f"ssh-proxy --cluster {cfg.cluster} "
            f"--port-forward {local_port}:127.0.0.1:6443"
        )
    return 0


def _pick_unused_local_port() -> int:
    """Pick a free TCP port on the operator host.

    Used by `main()` when the operator didn't pass --local-port
    and also didn't ask for the tunnel; the kubeconfig's server:
    URL still needs a valid port to point at so a later
    `ssh-proxy --port-forward <port>:...` lines up.
    """
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


if __name__ == "__main__":
    sys.exit(main())
