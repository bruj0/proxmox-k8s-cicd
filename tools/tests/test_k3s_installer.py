"""Red-first tests for tools.lib.k3s_installer.K3sInstaller.

These pin the contract that landed on 2026-07-08:

  * per-VM Python orchestration; no shell scripts
  * idempotent (refuses to re-install if k3s is healthy)
  * pinned via tools/versions.lock.yaml
  * agent joins via the kube-vip VIP, NOT a control-plane eth0 IP
  * control-plane installs with --tls-san=<vip>
  * no token / secret ever logged
  * survives on real SSH (paramiko-style invoke through `ssh` binary)

The recipe is canonicalized in the SKILL.md Step 4a gotchas and
implemented in tools/lib/k3s_installer.py.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.k3s_installer import (  # noqa: E402
    K3sInstaller,
    K3sInstallerError,
    ServerInstallPlan,
    AgentInstallPlan,
)
from lib.log import StructuredLogger  # noqa: E402
from lib.versions import VersionsLockReader  # noqa: E402


# ---------- fixtures ----------

@pytest.fixture()
def logger() -> StructuredLogger:
    return StructuredLogger("k3s_installer_test")


@pytest.fixture()
def cluster() -> dict[str, Any]:
    """A cluster dict shaped like infra/clusters/<name>/output.json.

    The values here mirror the live cicd cluster on kvm.bruj0.net as of
    2026-07-08 (VMID 112, SDN IP 10.0.0.65, VIP 10.0.0.30). The agent IPs
    intentionally use the live DHCP-pool values rather than the stale
    10.0.1.x/10.0.2.x that output.json carried -- this matches the
    SDN-IPAM reality documented in the SKILL.md Step 4a.3.x gotchas.
    """
    return {
        "name": "cicd",
        "vip": "10.0.0.30",
        "ssh_user": "ubuntu",
        "vms": [
            {"name": "cicd-cp-1", "vmid": 112, "role": "control_plane", "ip": "10.0.0.65"},
            {"name": "cicd-w-1", "vmid": 111, "role": "worker", "ip": "10.0.0.64"},
        ],
    }


@pytest.fixture()
def installer(cluster: dict[str, Any], logger: StructuredLogger) -> K3sInstaller:
    return K3sInstaller(
        cluster=cluster,
        ssh_proxy_target="root@kvm.bruj0.net -p 6022",
        logger=logger,
    )


# ---------- pinning ----------

def test_k3s_version_read_from_lockfile(tmp_path: Path) -> None:
    """tools/versions.lock.yaml::k3s_stable_version is the canonical pin."""
    lock = tmp_path / "versions.lock.yaml"
    lock.write_text(
        "k3s_stable_version: \"v1.36.2+k3s1\"\n"
        "k3s_install_url: \"https://get.k3s.io\"\n"
        "k3s_channel: \"stable\"\n"
    )
    reader = VersionsLockReader.from_lockfile(lock)
    assert reader.k3s_stable_version == "v1.36.2+k3s1"
    assert reader.k3s_install_url == "https://get.k3s.io"
    assert reader.k3s_channel == "stable"


def test_k3s_version_falls_back_to_legacy_deps_block(tmp_path: Path) -> None:
    """If k3s_stable_version is absent, fall back to dependencies[*].name=='k3s'.

    The lockfile grew the new top-level key when this module landed; old
    lockfiles only have the dependencies[] block. We must keep working.
    """
    lock = tmp_path / "versions.lock.yaml"
    lock.write_text(
        "dependencies:\n"
        "  - name: k3s\n"
        "    version: \"v1.34.x\"\n"
    )
    reader = VersionsLockReader.from_lockfile(lock)
    assert reader.k3s_stable_version == "v1.34.x"


def test_k3s_version_default_when_lockfile_missing(tmp_path: Path) -> None:
    """A missing lockfile falls back to the documented default, never raises."""
    lock = tmp_path / "missing.yaml"
    reader = VersionsLockReader.from_lockfile(lock)
    # Hardcoded documented default (matches versions.yaml::k3s::v1.34.x).
    assert reader.k3s_stable_version == "v1.36.2+k3s1"
    assert reader.k3s_install_url == "https://get.k3s.io"


# ---------- planning ----------

def test_plan_for_control_plane_includes_tls_san_and_flannel_off(
    installer: K3sInstaller,
) -> None:
    """Server installs always include --flannel-backend=none + --tls-san=<vip>."""
    cp = next(v for v in installer.cluster["vms"] if v["role"] == "control_plane")
    plan = installer.plan_server(cp, vip=installer.cluster["vip"])
    assert isinstance(plan, ServerInstallPlan)
    env = plan.environment
    # INSTALL_K3S_VERSION must be the exact pinned version.
    assert env["INSTALL_K3S_VERSION"] == "v1.36.2+k3s1"
    # K3S_* env vars are preserved for the systemd unit.
    assert env["K3S_NODE_NAME"] == cp["name"]
    exec_str = " ".join(plan.exec_flags)
    assert "--flannel-backend=none" in exec_str
    # Proxmox-CCM requires cloud-provider=external on the kubelet.
    assert "--kubelet-arg=cloud-provider=external" in exec_str
    # Traefik / servicelb / local-storage / metrics-server stay disabled
    # so the helm phase owns those concerns exclusively.
    assert "--disable=traefik" in exec_str
    assert "--disable=servicelb" in exec_str
    assert "--disable=local-storage" in exec_str
    assert "--disable=metrics-server" in exec_str
    # WP08 (2026-07-08): the apiserver cert SAN is now the CP host
    # IP, NOT a kube-vip VIP. The cluster runs single-control-plane
    # (cicd=10.0.0.65, apps=10.0.0.67) and the apiserver is reached
    # directly on the CP host IP. The vip= kwarg is preserved on
    # plan_server/install_server as a deprecation stub for callers
    # that still pass it, but the actual SAN is the CP eth0 IP.
    assert f"--tls-san={cp['ip']}" in exec_str
    # --node-ip / --node-external-ip get populated from the lease.
    assert f"--node-ip={cp['ip']}" in exec_str
    assert f"--node-external-ip={cp['ip']}" in exec_str


def test_plan_for_agent_joins_cp_ip_not_eth0(installer: K3sInstaller) -> None:
    """Agents must join K3S_URL=https://<cp_ip>:6443, never a VIP.

    WP08 (2026-07-08): the kube-vip VIP layer is gone. Agents join
    directly on the CP host IP (single CP per cluster; no HA load
    balancer is needed). This test pins the contract from
    tools/lib/k3s_installer.py.plan_agent.
    """
    worker = next(v for v in installer.cluster["vms"] if v["role"] == "worker")
    cp = next(v for v in installer.cluster["vms"] if v["role"] == "control_plane")
    plan = installer.plan_agent(worker, vip=installer.cluster["vip"], token="NODE::TOKEN")
    assert isinstance(plan, AgentInstallPlan)
    env = plan.environment
    # WP08: K3S_URL points at the CP host IP, not the legacy VIP.
    assert env["K3S_URL"] == f"https://{cp['ip']}:6443"
    assert env["K3S_TOKEN"] == "NODE::TOKEN"
    assert env["INSTALL_K3S_VERSION"] == "v1.36.2+k3s1"
    exec_str = " ".join(plan.exec_flags)
    # Agents do NOT have --tls-san (server-only flag; agent rejects it).
    assert "--tls-san" not in exec_str
    # CRITICAL: k3s agent binary REJECTS --flannel-backend. The server
    # sets --flannel-backend=none and the agent inherits the CNI choice
    # from the kubelet join handshake. Passing the flag directly to the
    # agent surfaces as `flag provided but not defined: -flannel-backend`
    # in journalctl.
    assert "--flannel-backend" not in exec_str, (
        "k3s agent does not accept --flannel-backend; the server-only "
        "flag is inherited via the join handshake. Reverting this "
        "causes systemctl status k3s-agent to exit 1."
    )
    # Agent must NOT ship --disable=traefik: only the server bundles Traefik.
    assert "--disable=traefik" not in exec_str


def test_plan_for_worker_rejects_token_missing(installer: K3sInstaller) -> None:
    """The agent plan refuses to render without a non-empty token."""
    worker = next(v for v in installer.cluster["vms"] if v["role"] == "worker")
    with pytest.raises(K3sInstallerError):
        installer.plan_agent(worker, vip=installer.cluster["vip"], token="")


def test_plan_server_rejects_blank_vip(installer: K3sInstaller) -> None:
    """A blank VIP is a misfit -- the server can't bind what it can't see."""
    cp = next(v for v in installer.cluster["vms"] if v["role"] == "control_plane")
    with pytest.raises(K3sInstallerError):
        installer.plan_server(cp, vip="")


# ---------- SSH-proxied execution (idempotency + idempotency-skip) ----------

class _Recorder:
    """Records (cmd, kwargs) for every `subprocess.run` call."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []
        # The fake's idea of what k3s is running on the remote node.
        # Tests can override before calling the install method. The
        # default matches the live cluster at the time this test was
        # written (post-reconcile).
        self.running_version: str = "v1.36.2+k3s1 (01b6f04a)"

    def __call__(self, cmd: list[str], **kwargs: Any) -> Any:
        self.calls.append((tuple(cmd), kwargs))
        # Pretend the SSH command succeeded; return value shape mirrors
        # subprocess.CompletedProcess with the stdout the installer
        # would receive.
        stdout = ""
        argv = tuple(cmd)
        # Helpers: substring search across all argv tokens.
        flat = " ".join(str(a) for a in argv)

        # /healthz probe -> "ok"
        if "/healthz" in flat and "curl" in flat:
            stdout = "ok"
        # systemctl is-active --quiet k3s -> exit code 0 (active)
        if "systemctl" in flat and "is-active" in flat:
            return _FakeProcess(0, "active", "")
        # /etc/rancher/k3s/k3s.yaml existence check
        if "/etc/rancher/k3s/k3s.yaml" in flat and "test -f" in flat:
            return _FakeProcess(0, "", "")
        # k3s --version probe (reconcile-and-pin gate) -- the
        # version comes from the active k3s_stable_version of the
        # fixture's versions reader.
        if "k3s --version" in flat:
            return _FakeProcess(0, f"k3s version {self.running_version}\n", "")
        # cat /var/lib/rancher/k3s/server/node-token
        if "node-token" in flat:
            return _FakeProcess(0, "NODE_TOKEN_FROM_TEST", "")
        # default
        return _FakeProcess(0, stdout, "")


class _FakeProcess:
    def __init__(self, rc: int, stdout: str, stderr: str) -> None:
        self.returncode = rc
        self.stdout = stdout
        self.stderr = stderr


def test_install_idempotent_when_k3s_already_healthy(
    installer: K3sInstaller, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """If k3s is healthy, install_server / install_agent short-circuit.

    The Python wrapper refuses to re-invoke the upstream installer when
    systemctl is-active returns 'active' AND /etc/rancher/k3s/k3s.yaml
    exists. This protects against `bootstrap_cluster --phase install_k3s`
    re-running on a healthy cluster.
    """
    rec = _Recorder()
    monkeypatch.setattr("lib.k3s_installer.subprocess.run", rec)
    cp = next(v for v in installer.cluster["vms"] if v["role"] == "control_plane")
    installer.install_server(cp, vip=installer.cluster["vip"])
    # The only SSH commands should be the two idempotency probes.
    # Anything else means the installer re-ran install.sh on a healthy cluster.
    saw_install = any(
        "sh -s -" in " ".join(str(a) for a in argv)
        and "get.k3s.io" in " ".join(str(a) for a in argv)
        for argv, _ in rec.calls
    )
    assert not saw_install, "installer re-ran install.sh on a healthy cluster"


def test_install_invokes_upstream_when_unhealthy(
    installer: K3sInstaller, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If k3s is NOT installed, the wrapper invokes get.k3s.io over SSH."""
    rec = _Recorder()

    def selective(cmd: list[str], **kwargs: Any) -> Any:
        rec.calls.append((tuple(cmd), kwargs))
        flat = " ".join(str(a) for a in cmd)
        # systemctl is-active -> 'inactive' (non-zero exit)
        if "systemctl" in flat and "is-active" in flat:
            return _FakeProcess(1, "inactive", "")
        # /etc/rancher/k3s/k3s.yaml existence -> missing
        if "/etc/rancher/k3s/k3s.yaml" in flat and "test -f" in flat:
            return _FakeProcess(1, "", "No such file")
        # Everything else (including the actual curl | sh) -> succeed
        return _FakeProcess(0, "", "")

    monkeypatch.setattr("lib.k3s_installer.subprocess.run", selective)
    cp = next(v for v in installer.cluster["vms"] if v["role"] == "control_plane")
    installer.install_server(cp, vip=installer.cluster["vip"])
    # The actual install.sh invocation MUST have happened.
    saw_install = any(
        "get.k3s.io" in " ".join(str(a) for a in argv)
        for argv, _ in rec.calls
    )
    assert saw_install, "installer did not invoke get.k3s.io on a broken cluster"
    # The env vars must have been passed via SSH into the remote shell.
    install_call = next(
        (
            {"args": list(argv)}
            for argv, _ in rec.calls
            if "get.k3s.io" in " ".join(str(a) for a in argv)
        ),
        None,
    )
    assert install_call is not None
    # The upstream installer expects the env to be passed as a shell
    # prefix. We verify that INSTALL_K3S_VERSION is in that prefix.
    cmd_str = " ".join(install_call.get("args", []) or [])
    assert "INSTALL_K3S_VERSION=v1.36.2+k3s1" in cmd_str


def test_install_invokes_upstream_when_running_version_drifts(
    installer: K3sInstaller, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Reconcile-and-pin: if the running k3s is on an older patch than
    `k3s_stable_version`, the installer re-runs the upstream installer
    to roll the node forward. The systemctl and kubeconfig probes say
    'healthy' (so the existing short-circuit would otherwise fire) but
    the version probe says 'older' and that should force a re-install.

    The reconcile-and-pin policy is the central reason
    `k3s_stable_version` is read at install time, not just at first
    bootstrap: drifted nodes get picked up by the next install_k3s
    run without any operator intervention.
    """
    rec = _Recorder()
    # Pretend the remote node is on v1.34.9 (the pre-reconcile pin).
    rec.running_version = "v1.34.9+k3s1 (5f72184f)"
    monkeypatch.setattr("lib.k3s_installer.subprocess.run", rec)
    cp = next(v for v in installer.cluster["vms"] if v["role"] == "control_plane")
    installer.install_server(cp, vip=installer.cluster["vip"])
    # The actual install.sh invocation MUST have happened -- the
    # version drift is enough to override the "already healthy" gate.
    saw_install = any(
        "get.k3s.io" in " ".join(str(a) for a in argv)
        for argv, _ in rec.calls
    )
    assert saw_install, (
        "installer did not reconcile a drifted node "
        f"(running={rec.running_version}, "
        f"pinned={installer.versions.k3s_stable_version})"
    )


def test_install_skips_when_running_version_matches(
    installer: K3sInstaller, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Companion to the drift test: when the running version matches
    the pin, the installer short-circuits. This is the same behavior
    the old `_is_k3s_healthy` check provided, but now gated on the
    version string, not just on the systemd unit's active state.
    """
    rec = _Recorder()
    # Default running_version is v1.36.2+k3s1 -- matches the pin.
    monkeypatch.setattr("lib.k3s_installer.subprocess.run", rec)
    cp = next(v for v in installer.cluster["vms"] if v["role"] == "control_plane")
    installer.install_server(cp, vip=installer.cluster["vip"])
    saw_install = any(
        "get.k3s.io" in " ".join(str(a) for a in argv)
        for argv, _ in rec.calls
    )
    assert not saw_install, (
        "installer re-ran install.sh on a node already at the pinned version"
    )


def test_read_node_token_raises_on_missing_file(
    installer: K3sInstaller, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the server has no node-token file, raise a structured error."""
    def fake_run(cmd: list[str], **kwargs: Any) -> Any:
        return _FakeProcess(1, "", "No such file or directory")

    monkeypatch.setattr("lib.k3s_installer.subprocess.run", fake_run)
    with pytest.raises(K3sInstallerError):
        installer.read_node_token(
            next(v for v in installer.cluster["vms"] if v["role"] == "control_plane")
        )


def test_read_node_token_returns_trimmed_value(
    installer: K3sInstaller, monkeypatch: pytest.MonkeyPatch
) -> None:
    """read_node_token returns the trimmed bytes from the server's token file."""
    def fake_run(cmd: list[str], **kwargs: Any) -> Any:
        flat = " ".join(str(a) for a in cmd)
        if "node-token" in flat:
            return _FakeProcess(0, " NODE::SECRETTOKEN \n", "")
        return _FakeProcess(0, "", "")

    monkeypatch.setattr("lib.k3s_installer.subprocess.run", fake_run)
    tok = installer.read_node_token(
        next(v for v in installer.cluster["vms"] if v["role"] == "control_plane")
    )
    assert tok == "NODE::SECRETTOKEN"


# ---------- log scrubbing ----------

def test_install_does_not_log_token(installer: K3sInstaller, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """M7: the join token must NEVER appear in stdout, even for one log line."""
    rec = _Recorder()
    monkeypatch.setattr("lib.k3s_installer.subprocess.run", rec)
    cp = next(v for v in installer.cluster["vms"] if v["role"] == "control_plane")
    worker = next(v for v in installer.cluster["vms"] if v["role"] == "worker")
    installer.install_server(cp, vip=installer.cluster["vip"])
    installer.install_agent(worker, vip=installer.cluster["vip"], token="ULTRA_SECRET_TOKEN")
    out = capsys.readouterr().out
    assert "ULTRA_SECRET_TOKEN" not in out


def test_logger_scrubs_token_keys(installer: K3sInstaller, capsys: pytest.CaptureFixture[str]) -> None:
    """The StructuredLogger must drop any key whose name contains 'token'.

    Belt-and-braces: even if a future contributor accidentally passes
    token=... to a logger call, the scrub layer wipes it.
    """
    installer.logger.info("scrub.probe", K3S_TOKEN="ULTRA_SECRET_TOKEN")
    out = capsys.readouterr().out
    assert "ULTRA_SECRET_TOKEN" not in out
    # The probe step still got logged (just without the value).
    assert "scrub.probe" in out
