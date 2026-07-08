"""K3sInstaller — Python orchestration for installing k3s on the cluster VMs.

Replaces what was previously done in cloud-init runcmd (Phase 1) for the
real per-VM install. Kept pure-Python (no shell scripts in tools/scripts/);
the only shell we execute is the upstream `install.sh` from get.k3s.io,
invoked over SSH.

Key contracts (pinned by tests/test_k3s_installer.py):

  * Versions come from tools/versions.lock.yaml via VersionsLockReader.
  * Idempotent: refuse to re-run when `systemctl is-active k3s` returns
    'active' AND /etc/rancher/k3s/k3s.yaml exists. The upstream
    `install.sh` is itself hash-checked, so re-runs would also be no-ops,
    but a Python-side short-circuit avoids even the network round-trip.
  * Agents join on `https://<vip>:6443`, never a control-plane eth0 IP.
    The VIP is owned by kube-vip's gratuitous ARP; pinning to a CP IP
    would break failover.
  * Control-plane installs with `--tls-san=<vip>` so a kubeconfig
    pulled from the server references a SAN the apiserver's serving
    cert actually carries. Live 2026-07-08 probe: VIP records exist
    in PowerDNS but the apiserver has no listener until kube-vip comes
    up; the first external client (kubectl) gets
    `x509: certificate is not valid for <vip>` without this flag.
  * No token / secret ever logged (M7). StructuredLogger scrubs at the
    boundary; we ALSO pass the join token as an environment variable
    on the SSH command so it doesn't appear in argv / process list.

Idempotency recap (per-VM, per-call):
    - Idempotent if k3s systemd unit is active AND kubeconfig exists.
    - Upstream installer.sh is hash-checked; same env -> `No change
      detected so skipping service start`. We don't rely on that, we
      add our own gate.

Cross-cluster smoke (2026-07-08, kvm.bruj0.net): VIPs pre-existing
in PowerDNS (10.0.0.30 cicd, 10.0.0.40 apps), but unreachable until
this module brings up k3s + until the `helm` phase brings up kube-vip.
Order of operations: cloudinit -> install_k3s -> helm.
"""
from __future__ import annotations

import re
import shlex
import subprocess
from dataclasses import dataclass
from typing import Any, Mapping

from tools.lib.log import StructuredLogger
from tools.lib.pve_ssh import PveSshProxy
from tools.lib.versions import VersionsLockReader


class K3sInstallerError(RuntimeError):
    """Raised on any failure path with a structured message.

    Operator-facing fields are exposed as JSON-ish kwargs so callers
    can parse the reason without re-formatting the string.
    """

    def __init__(self, reason: str, **fields: Any) -> None:
        self.reason = reason
        self.fields = fields
        super().__init__(f"k3s installer: {reason} ({json_dumps(fields)})")

    def to_dict(self) -> dict[str, Any]:
        return {"reason": self.reason, **self.fields}


def json_dumps(obj: Any) -> str:
    import json

    return json.dumps(obj, sort_keys=True, default=str)


@dataclass(frozen=True)
class ServerInstallPlan:
    """The exact recipe to install k3s as a server on one VM.

    `environment` is exported into the remote shell BEFORE `sh -s -`,
    so the upstream installer reads INSTALL_K3S_VERSION etc. without
    them appearing on the SSH command line.

    `exec_flags` is the tail of the upstream call after `sh -s -`, so
    a.k.a. `server ...`. Combined with `environment`, the rendered
    command is:

        env INSTALL_K3S_VERSION=v1.36.2+k3s1 K3S_NODE_NAME=cicd-cp-1 \
            curl -sfL https://get.k3s.io | sh -s - server --flannel-backend=none ...
    """

    node_name: str
    node_ip: str
    vip: str
    environment: dict[str, str]
    exec_flags: list[str]


@dataclass(frozen=True)
class AgentInstallPlan:
    """The exact recipe to install k3s as an agent on one VM.

    Same render as ServerInstallPlan but K3S_URL+K3S_TOKEN are set, and
    the implicit default for `install.sh` (no args) is `server`. We
    always pass `agent` explicitly so there's no ambiguity.
    """

    node_name: str
    node_ip: str
    vip: str
    token: str
    environment: dict[str, str]
    exec_flags: list[str]


# Shared flag set for the control-plane node. Matched to
# versions.yaml::k3s::v1.34.x::install_args_default.control_plane, with
# the VIP verification note's mandatory additions:
#   --tls-san=<vip>             -- apiserver cert must carry the VIP
#   --node-ip / --node-external-ip -- populate from the SDN DHCP lease
_SERVER_BASE_FLAGS: tuple[str, ...] = (
    "--flannel-backend=none",
    "--disable=traefik",
    "--disable=servicelb",
    "--disable=local-storage",
    "--disable=metrics-server",
    "--kubelet-arg=cloud-provider=external",
)


# Agent-side flags. Per the official k3s documentation, the
# `--flannel-backend` flag is server-ONLY; the agent inherits the
# server's CNI decision via the kubelet join handshake. Agents on
# a `--flannel-backend=none` server must NOT pass --flannel-backend
# at all (k3s agent binary rejects it: "flag provided but not
# defined: -flannel-backend"). Same for --disable={traefik,...}
# which are server-only.
#
# What agents DO need: --node-ip / --node-external-ip. Anything else
# is kubelet config we may add later.
_AGENT_BASE_FLAGS: tuple[str, ...] = ()


@dataclass
class K3sInstaller:
    """Per-cluster Python orchestrator for the install_k3s sub-phase.

    Constructed once per cluster by `bootstrap_cluster._run_install_k3s`.
    All public methods are idempotent on a per-VM basis (a re-run sees
    the systemd unit already active and skips).
    """

    cluster: Mapping[str, Any]
    ssh_proxy_target: str
    logger: StructuredLogger
    versions: VersionsLockReader | None = None
    proxy: PveSshProxy | None = None

    def __post_init__(self) -> None:
        # VersionsLockReader is read-only and cacheable; default to a
        # reader backed by documented defaults. Tests can either (a)
        # inject their own `versions` argument, or (b) leave it None
        # and we'll fall back to defaults. We deliberately do NOT touch
        # the filesystem here so tests don't depend on the real repo
        # lockfile.
        if self.versions is None:
            object.__setattr__(
                self,
                "versions",
                VersionsLockReader(logger=self.logger),
            )
        if self.proxy is None:
            # ssh_user on the cluster dict lets per-cluster tofu
            # overrides pin a different in-VM user (we currently use
            # the cloud-image default `ubuntu` everywhere).
            ssh_user = str(self.cluster.get("ssh_user", "ubuntu"))
            object.__setattr__(
                self,
                "proxy",
                PveSshProxy(
                    jump_host=self.ssh_proxy_target,
                    ssh_user=ssh_user,
                    logger=self.logger,
                ),
            )

    @property
    def _versions(self) -> VersionsLockReader:
        # Convenience accessor for the always-non-None invariant.
        assert self.versions is not None
        return self.versions

    @property
    def _proxy(self) -> PveSshProxy:
        # Convenience accessor for the always-non-None invariant.
        assert self.proxy is not None
        return self.proxy

    # ---------- planning ----------

    def plan_server(self, vm: Mapping[str, Any], *, vip: str) -> ServerInstallPlan:
        """Render the server install plan for one control-plane VM."""
        if not vip:
            raise K3sInstallerError(
                "blank_vip",
                node=vm.get("name"),
                resolution="pass infra/clusters/<name>/output.json::vip through",
            )
        node_ip = str(vm["ip"])
        node_name = str(vm["name"])
        # --tls-san=<vip> is REQUIRED (came out of the VIP verification).
        # Filled at install time, never hard-coded.
        flags = [
            *_SERVER_BASE_FLAGS,
            f"--node-ip={node_ip}",
            f"--node-external-ip={node_ip}",
            f"--tls-san={vip}",
        ]
        env = {
            "INSTALL_K3S_VERSION": self._versions.k3s_stable_version,
            "INSTALL_K3S_CHANNEL": self._versions.k3s_channel,
            "K3S_NODE_NAME": node_name,
        }
        return ServerInstallPlan(
            node_name=node_name,
            node_ip=node_ip,
            vip=vip,
            environment=env,
            exec_flags=["server", *flags],
        )

    def plan_agent(
        self,
        vm: Mapping[str, Any],
        *,
        vip: str,
        token: str,
    ) -> AgentInstallPlan:
        """Render the agent install plan for one worker VM."""
        if not vip:
            raise K3sInstallerError(
                "blank_vip",
                node=vm.get("name"),
                resolution="pass infra/clusters/<name>/output.json::vip through",
            )
        if not token:
            raise K3sInstallerError(
                "blank_token",
                node=vm.get("name"),
                resolution=(
                    "fetch the token via read_node_token() from the "
                    "control-plane VM before installing agents"
                ),
            )
        node_ip = str(vm["ip"])
        node_name = str(vm["name"])
        env = {
            "INSTALL_K3S_VERSION": self._versions.k3s_stable_version,
            "INSTALL_K3S_CHANNEL": self._versions.k3s_channel,
            "K3S_NODE_NAME": node_name,
            "K3S_URL": f"https://{vip}:6443",
            # K3S_TOKEN is never logged; the StructuredLogger scrubs at the
            # boundary, but we ALSO avoid putting it on the command line --
            # we pass it via the remote shell's environment.
            "K3S_TOKEN": token,
        }
        flags = [
            *_AGENT_BASE_FLAGS,
            f"--node-ip={node_ip}",
            f"--node-external-ip={node_ip}",
        ]
        return AgentInstallPlan(
            node_name=node_name,
            node_ip=node_ip,
            vip=vip,
            token=token,
            environment=env,
            exec_flags=["agent", *flags],
        )

    # ---------- execution ----------

    def install_server(self, vm: Mapping[str, Any], *, vip: str) -> None:
        """Idempotently install k3s as a server on the given control-plane VM."""
        plan = self.plan_server(vm, vip=vip)
        if self._is_k3s_healthy(vm):
            self.logger.info(
                "k3s.skip_install",
                node=plan.node_name,
                reason="already healthy (systemctl is-active + kubeconfig present)",
            )
            return
        self.logger.info(
            "k3s.install_server",
            node=plan.node_name,
            vip=plan.vip,
            version=self._versions.k3s_stable_version,
        )
        self._run_upstream_install(vm, plan)

    def install_agent(
        self,
        vm: Mapping[str, Any],
        *,
        vip: str,
        token: str,
    ) -> None:
        """Idempotently install k3s as an agent on the given worker VM."""
        plan = self.plan_agent(vm, vip=vip, token=token)
        if self._is_k3s_agent_healthy(vm):
            self.logger.info(
                "k3s.skip_install",
                node=plan.node_name,
                reason="already healthy (k3s-agent unit active)",
            )
            return
        self.logger.info(
            "k3s.install_agent",
            node=plan.node_name,
            vip=plan.vip,
            version=self._versions.k3s_stable_version,
            # token deliberately not passed -> scrubbed by StructuredLogger.
        )
        self._run_upstream_install(vm, plan)

    def read_node_token(self, server_vm: Mapping[str, Any]) -> str:
        """Fetch the join token from the control-plane VM.

        Returns the trimmed token. On any failure path raises
        K3sInstallerError so callers see a structured message instead
        of a raw stderr blob.
        """
        ip = str(server_vm["ip"])
        # Tunnel through the PVE jump host into the VM's SDN IP and run
        # as root via sudo -n (the cluster VMs refuse root login).
        inner = "cat /var/lib/rancher/k3s/server/node-token 2>/dev/null"
        remote = f"sudo -n bash -c {shlex.quote(inner)}"
        cmd = self._proxy.ssh_argv(ip, command=remote)
        proc = subprocess.run(  # noqa: S603 -- single, documented shell call.
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        if proc.returncode != 0:
            raise K3sInstallerError(
                "node_token_unreadable",
                server=server_vm.get("name"),
                returncode=proc.returncode,
                stderr=proc.stderr[:200],
            )
        token = proc.stdout.strip()
        if not token:
            raise K3sInstallerError(
                "node_token_empty",
                server=server_vm.get("name"),
                resolution=(
                    "install_server may not have completed; rerun the "
                    "install_k3s phase and inspect /var/log/k3s.log "
                    "on the server"
                ),
            )
        return token

    # ---------- internals ----------

    def _is_k3s_healthy(self, vm: Mapping[str, Any]) -> bool:
        """Probe + kubeconfig existence check before re-installing.

        Returns False (forcing a re-install) if EITHER:
          - the k3s systemd unit is not active, OR
          - /etc/rancher/k3s/k3s.yaml is missing, OR
          - the running k3s version does NOT match the pinned
            `k3s_stable_version` (reconcile-and-pin policy -- the
            installer re-runs the upstream installer.sh to roll
            forward drifted nodes without disturbing healthy ones).
        """
        ip = str(vm["ip"])
        if self._ssh_returncode(
            ip, "systemctl is-active --quiet k3s"
        ) != 0:
            return False
        if self._ssh_returncode(ip, "test -f /etc/rancher/k3s/k3s.yaml") != 0:
            return False
        return self._running_k3s_version_matches(vm)

    def _is_k3s_agent_healthy(self, vm: Mapping[str, Any]) -> bool:
        """Like `_is_k3s_healthy` but for agents (unit is `k3s-agent`)."""
        ip = str(vm["ip"])
        if self._ssh_returncode(ip, "systemctl is-active --quiet k3s-agent") != 0:
            return False
        return self._running_k3s_version_matches(vm)

    def _running_k3s_version_matches(self, vm: Mapping[str, Any]) -> bool:
        """Return True iff the on-host `k3s --version` matches the pin.

        Reconcile-and-pin policy: the installer always targets
        `k3s_stable_version` from the lockfile. A drifted node
        (running an older patch) is treated as "not healthy" so the
        upstream installer is re-invoked to roll it forward. The
        upstream installer.sh does the actual download + replace;
        our short-circuit just gates the call.

        Returns True on any SSH error so a probe failure does not
        accidentally trigger a re-install on a node we can't read.
        """
        ip = str(vm["ip"])
        try:
            proc_str = self._ssh_capture(
                ip, "k3s --version 2>/dev/null | head -n1"
            )
        except Exception:  # pragma: no cover -- defensive
            return True
        # proc_str is "k3s version v1.34.9+k3s1 (5f72184f)"
        m = re.search(r"v\d+\.\d+\.\d+\+k3s\d+", proc_str)
        if m is None:
            return True
        running = m.group(0)
        pinned = self._versions.k3s_stable_version
        if running != pinned:
            self.logger.info(
                "k3s.version_drift",
                node=str(vm.get("name")),
                running=running,
                pinned=pinned,
                resolution=(
                    "re-running upstream installer to reconcile the pin"
                ),
            )
        return running == pinned

    def _ssh_capture(self, ip: str, command: str) -> str:
        """Run `command` over SSH as root; return stdout, never raise.

        Symmetric with `_ssh_returncode` (both wrap a single
        `sudo -n bash -c <command>` ssh call) but returns the
        captured stdout. Used by probes that need to parse a version
        string. Returns "" on any error so callers can pattern-match
        safely.
        """
        try:
            remote = f"sudo -n bash -c {shlex.quote(command)}"
            proc = subprocess.run(  # noqa: S603 -- documented shell call.
                self._proxy.ssh_argv(ip, command=remote),
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
            if proc.returncode != 0:
                return ""
            return proc.stdout
        except (subprocess.CalledProcessError, OSError):
            return ""

    def _ssh_returncode(self, ip: str, command: str) -> int:
        """Run `command` over SSH as root; return the exit code, never raise.

        The remote user is `ubuntu` (cluster VMs use the cloud-image
        default; root login is rejected). We `sudo -n bash -c <command>`
        to escalate to root for the call. The whole command is
        single-quoted on argv so the operators in the inner shell
        don't get tokenized by the outer ssh client.

        Used for idempotency gates where a non-zero result is just a
        'not installed yet' signal, not an error worth surfacing.
        Returns -1 if the SSH invocation itself exploded (e.g. agent
        down, network unreachable). Callers should treat -1 the same
        as a non-zero rc.
        """
        try:
            remote = f"sudo -n bash -c {shlex.quote(command)}"
            proc = subprocess.run(  # noqa: S603 -- documented shell call.
                self._proxy.ssh_argv(ip, command=remote),
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
            return proc.returncode
        except (subprocess.CalledProcessError, OSError):
            return -1

    def _run_upstream_install(
        self,
        vm: Mapping[str, Any],
        plan: ServerInstallPlan | AgentInstallPlan,
    ) -> None:
        """Invoke the upstream `install.sh` over SSH on the given VM.

        Renders to (single shell run as root via `sudo -n bash -c`):
            sudo -n bash -c '
              export K3S_TOKEN=...
              export INSTALL_K3S_VERSION=v1.36.2+k3s1
              ...
              curl -sfL https://get.k3s.io | sh -s - <exec-flags...>
            '

        Notes:
          - The env vars are placed INSIDE the sudo'd bash (via
            `export`), not on the outer shell. `sudo -n` strips the
            caller's environment by default (env_reset is on; env_keep
            does not include K3S_*/INSTALL_K3S_*), so a bare env prefix
            on the outer shell would be silently lost.
          - The token / secret env vars are exported, not argv. They
            never appear in `ps auxe` output on the operator host.
          - The upstream install script is itself idempotent (hash check);
            our Python-side short-circuit just saves a network round-trip.
          - The remote user is `ubuntu` (cluster VMs use the cloud-image
            default; root login is rejected). `sudo -n` requires no
            password -- the cluster root tofu module sets `NOPASSWD` for
            the operator's ssh-key user.
        """
        ip = str(vm["ip"])
        # Whitelist env keys (alphanumeric + underscore) to defend against
        # accidental injection. Only the VALUE is shlex-quoted; the KEY
        # is left bare so bash parses it as an assignment.
        export_lines = []
        for k, v in plan.environment.items():
            if not k.replace("_", "").isalnum():
                raise K3sInstallerError(
                    "unsafe_env_key",
                    node=vm.get("name"),
                    key=k,
                    resolution=(
                        "env keys must be alphanumeric + underscore; "
                        "check the install plan"
                    ),
                )
            export_lines.append(f"export {k}={shlex.quote(v)}")
        export_block = "\n".join(export_lines) + "\n"
        # Build the inner shell payload, then wrap in sudo -n bash -c.
        # We use a literal shell command: `export K=...; export T=...;
        # curl -sfL https://get.k3s.io | sh -s - <flags>`. Putting the
        # `export` statements INSIDE the sudo'd bash is necessary because
        # `sudo -n` strips the caller's env by default (env_reset is on;
        # env_keep does NOT include K3S_*/INSTALL_K3S_*). The bare env
        # prefix on the outer shell would be silently lost.
        install_cmd = (
            f"curl -sfL {self._versions.k3s_install_url} | "
            "sh -s - " + " ".join(shlex.quote(f) for f in plan.exec_flags)
        )
        inner = export_block + install_cmd
        remote = f"sudo -n bash -c {shlex.quote(inner)}"
        cmd = self._proxy.ssh_argv(ip, command=remote)
        try:
            proc = subprocess.run(  # noqa: S603
                cmd,
                check=False,
                capture_output=True,
                text=True,
                timeout=240,  # 4 min -- enough for slow dnsmasq + 100 MB download
            )
        except (subprocess.CalledProcessError, OSError) as exc:
            raise K3sInstallerError(
                "ssh_invocation_failed",
                node=vm.get("name"),
                install_url=self._versions.k3s_install_url,
                detail=str(exc),
            ) from exc
        if proc.returncode != 0:
            raise K3sInstallerError(
                "install_sh_failed",
                node=vm.get("name"),
                install_url=self._versions.k3s_install_url,
                version=self._versions.k3s_stable_version,
                returncode=proc.returncode,
                stdout_tail=proc.stdout[-200:],
                stderr_tail=proc.stderr[-400:],
            )
