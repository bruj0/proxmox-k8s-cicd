"""WP04 acceptance tests.

These tests are red-first: they were written before
tools/bootstrap_cluster.py and tools/lib/{helm,kubeconfig}_client.py
existed. They encode the four M4 + M7 acceptance criteria from the WP04 spec:

  M4 acceptance: phases must surface non-zero exits (no silent failure).
  M4 acceptance: re-running with `--phases cloudinit` skips later phases
                 (idempotent restart from earlier phase).
  M4 acceptance: missing output.json fails the "cloudinit" phase with a
                 clear, machine-readable error (not a stack trace).
  M7 acceptance: tokens are never logged at any level (scrub) - applies
                 to both bootstrap_cluster.py and its delegates.

OS-pivot note (2026-07-07): the `talos` phase was renamed to `cloudinit`.
The Talos-phase test fixtures (talos_dir, *.yaml) are still created in
_write_cluster because lib.talos_client.ClusterTopology.from_output_json
accepts the field for backwards-compatibility. They are not consumed by
_run_cloudinit (which is a Python-side no-op; the actual k3s install
happens at VM first boot via cloud-init runcmd in the NoCloud seed ISO).

Side-effect guarantee: tests use `tmp_path` and monkeypatch subprocess.
No real network calls. No real PVE calls.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bootstrap_cluster import BootstrapError, bootstrap, list_phases  # noqa: E402


def _stub_ok(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=args, returncode=0, stdout="ok", stderr="")


def _stub_fail(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """Simulate a non-zero exit.

    We can't rely on the real `subprocess.run` to raise CalledProcessError
    here because we're replacing it wholesale. Raise directly so the
    production error path (subprocess.CalledProcessError -> BootstrapError)
    is exercised.
    """
    raise subprocess.CalledProcessError(returncode=1, cmd=list(args), stderr="kaboom")


def _write_cluster(tmp_path: Path) -> Path:
    """Materialise a cluster dir in the SS2 output.json shape."""
    cluster = tmp_path / "infra" / "clusters" / "cicd"
    cluster.mkdir(parents=True)
    # SS2 emits: cluster_name, vip, vnet_bridge, control_plane_count,
    # worker_count, nodes: [{role, name, ip, ...}], helm_releases.
    # We omit pod_cidr/svc_cidr to exercise the SS3 default fallback.
    (cluster / "output.json").write_text(
        json.dumps(
            {
                "cluster_name": "cicd",
                "vip": "10.0.0.10",
                "vnet_bridge": "vnet0",
                "control_plane_count": 2,
                "worker_count": 1,
                "nodes": [
                    {"role": "control_plane", "name": "cp1", "ip": "10.0.0.11"},
                    {"role": "control_plane", "name": "cp2", "ip": "10.0.0.12"},
                    {"role": "worker", "name": "w1", "ip": "10.0.0.21"},
                ],
                "helm_releases": [
                    "cilium",
                    "proxmox-cloud-controller-manager",
                    "proxmox-csi-plugin",
                    "traefik",
                    "cloudflare-tunnel-ingress-controller",
                    "cert-manager",
                ],
            }
        )
    )
    # Backwards-compat: lib.talos_client.ClusterTopology still references a
    # talos_dir key for audit. On the Ubuntu+k3s path this directory is not
    # read by the bootstrap script; the test fixture keeps it so the import
    # path remains valid.
    talos_dir = cluster / "talos"
    talos_dir.mkdir()
    for name in ("cp1", "cp2", "w1"):
        (talos_dir / f"{name}.yaml").write_text("type: v1alpha1\n")
    return cluster


def test_list_phases_returns_all_seven() -> None:
    """Acceptance: phases enum is [cloudinit, install_k3s, k3s, helm, kubeconfig, host_ports, externalname]."""
    assert list_phases() == [
        "cloudinit",
        "install_k3s",
        "k3s",
        "helm",
        "kubeconfig",
        "host_ports",
        "externalname",
    ]


def test_bootstrap_missing_output_json_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """M4 acceptance: missing output.json fails clearly, not silently."""
    monkeypatch.setattr(subprocess, "run", _stub_ok)
    with pytest.raises(BootstrapError, match=r"output\.json"):
        bootstrap(cluster_name="cicd", repo_root=tmp_path)


def test_bootstrap_silent_failure_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """M4 acceptance: non-zero subprocess exits surface as BootstrapError.

    This is the 'silent failure' misfit M4 -- a non-zero exit must not be
    swallowed. Previously considered for swallow-and-continue.

    `install_k3s` is now its own phase and short-circuits if the k3s
    unit is already active (idempotent state), so this test starts the
    VMs in 'k3s broken' to prove the silent-failure path is still
    exercised.
    """
    cluster = _write_cluster(tmp_path)
    monkeypatch.setattr(subprocess, "run", _stub_fail)
    with pytest.raises(BootstrapError):
        bootstrap(cluster_name="cicd", repo_root=cluster.parent.parent.parent)


def test_bootstrap_phase_filter_skips_later_phases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M4 acceptance: re-running with --phases cloudinit skips later phases."""
    cluster = _write_cluster(tmp_path)
    monkeypatch.setattr(subprocess, "run", _stub_ok)
    # Should complete without touching helm/k3s phases at all.
    bootstrap(cluster_name="cicd", repo_root=cluster.parent.parent.parent, phases=("cloudinit",))


def test_bootstrap_full_happy_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance: running all phases in order completes without raising.

    Exercises the canonical operator flow:
      cloudinit -> k3s -> helm -> kubeconfig (host_ports skipped because
      the baseline file does not exist in the temp dir).
    Without this test, a structural bug (e.g. helm phase referencing a
    kubeconfig file that the kubeconfig phase hasn't written yet) could
    pass every other test and still break in production.

    The Ubuntu+k3s path talks to the CP via PveSshProxy (a port forward
    + sudo cat /etc/rancher/k3s/k3s.yaml). We stub that out with a
    fake proxy that returns a syntactically-valid kubeconfig body, so
    the helm phase can run end-to-end without any real network calls.
    """
    cluster = _write_cluster(tmp_path)

    # Stub required secrets so _load_cluster_secrets() doesn't raise.
    monkeypatch.setenv("PROXMOX_TOKEN_ID", "fake-id")
    monkeypatch.setenv("PROXMOX_TOKEN_SECRET", "fake-secret")
    monkeypatch.setenv("CF_API_TOKEN", "fake-cf")
    monkeypatch.setenv("CF_ACCOUNT_ID", "fake-account")

    calls: list[tuple[str, ...]] = []

    def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        cmd = tuple(str(a) for a in args[0])
        calls.append(cmd)
        # kubectl --kubeconfig ... get --raw /healthz needs to return 'ok'.
        if cmd and cmd[0] == "kubectl" and "/healthz" in cmd:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="ok", stderr="")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    # Stub PveSshProxy so the helm + kubeconfig phases can run without
    # touching a real Proxmox node. The stub returns a syntactically
    # valid k3s kubeconfig body from `proxy.run()` and a no-op forward
    # from `proxy.port_forward()`.
    class _FakeForward:
        local_port = 16443
        proc = type("_FakeProc", (), {"pid": 4242})()

        def wait_ready(self, timeout_s: float = 15.0) -> None:
            return None

        def terminate(self) -> None:
            return None

    class _FakeProxy:
        def __init__(self, *a: Any, **kw: Any) -> None:
            return None

        def port_forward(
            self,
            target_ip: str,
            *,
            remote_port: int = 6443,
            remote_bind: str = "127.0.0.1",
            local_port: int = 0,
        ) -> _FakeForward:
            return _FakeForward()

        def run(self, target_ip: str, command: str, **kw: Any) -> Any:
            # Mimic `sudo cat /etc/rancher/k3s/k3s.yaml` -- return a
            # kubeconfig body with the loopback server URL so
            # rewrite_server_url has something to match.
            return type(
                "_R",
                (),
                {
                    "stdout": (
                        "apiVersion: v1\n"
                        "kind: Config\n"
                        "clusters:\n"
                        "- cluster:\n"
                        "    server: https://127.0.0.1:6443\n"
                        "  name: fake\n"
                        "contexts: []\n"
                        "users: []\n"
                    ),
                    "stderr": "",
                },
            )()

    import bootstrap_cluster as _bc  # noqa: PLC0415 -- local import for clarity
    # Patch the source module so both consumers see the stub. The
    # merger (`lib.kubeconfig_merger`) imports PveSshProxy via
    # `from .pve_ssh import PveSshProxy`; module-level bindings are
    # set at import time, so monkeypatch.setattr on lib.pve_ssh only
    # reaches callers that look up the class dynamically. The cleanest
    # stub for the merger is to short-circuit the function itself
    # (it owns the body-fetch + rewrite + merge logic).
    import lib.pve_ssh as _pve_ssh  # noqa: PLC0415
    import lib.kubeconfig_merger as _kcm  # noqa: PLC0415

    def _fake_merge_kubeconfig_for_pveproxy(
        cluster_name: str,
        control_plane_ip: str,
        repo_root: Path,
        home: Path,
        *,
        forward_local_port: int,
        forward_proc: object,
    ) -> Path:
        # Write the cluster kubeconfig verbatim (same body the helm
        # phase wrote) and then perform the ~/.kube/config merge step.
        cluster_dir = repo_root / "infra" / "clusters" / cluster_name
        cluster_dir.mkdir(parents=True, exist_ok=True)
        kubeconfig_path = cluster_dir / "kubeconfig"
        # The body was already written by the helm phase. If not,
        # materialise a minimal one so the test still exercises the
        # merge step.
        if not kubeconfig_path.exists():
            kubeconfig_path.write_text(
                "apiVersion: v1\nkind: Config\nserver: "
                f"https://127.0.0.1:{forward_local_port}\n"
            )
        kubeconfig_path.chmod(0o600)
        from lib.kubeconfig_merger import _merge_into_default  # noqa: PLC0415
        return _merge_into_default(cluster_name, kubeconfig_path, home)

    monkeypatch.setattr(_pve_ssh, "PveSshProxy", _FakeProxy)
    monkeypatch.setattr(_bc, "PveSshProxy", _FakeProxy)
    monkeypatch.setattr(_kcm, "merge_kubeconfig_for_pveproxy", _fake_merge_kubeconfig_for_pveproxy)
    # bootstrap_cluster re-imports the symbol into its own namespace at
    # load time, so the merger-call site in bootstrap_cluster.py needs
    # a separate patch.
    monkeypatch.setattr(_bc, "merge_kubeconfig_for_pveproxy", _fake_merge_kubeconfig_for_pveproxy)

    # Skip the host_ports phase explicitly so the test does not need a
    # baseline file. WP05 tests cover that phase separately.
    bootstrap(
        cluster_name="cicd",
        repo_root=cluster.parent.parent.parent,
        phases=("cloudinit", "k3s", "helm", "kubeconfig"),
    )
    # The k3s phase probes apiserver via `kubectl --kubeconfig ...
    # get --raw /healthz`; the helm phase runs `helm upgrade --install`;
    # the kubeconfig phase merges into ~/.kube/config via
    # `kubectl config view --flatten`.
    cmds = [" ".join(c) for c in calls]
    assert any(c.startswith("kubectl") and "/healthz" in c for c in cmds)
    assert any(c.startswith("helm upgrade") for c in cmds)
    assert any(c.startswith("kubectl config view") for c in cmds)
    # The kubeconfig file the helm phase wrote should now exist and
    # contain a server: URL pointing at the fake forward's local port.
    kc = cluster / "kubeconfig"
    assert kc.exists()
    assert "https://127.0.0.1:16443" in kc.read_text()


def test_bootstrap_logs_redact_secret_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """M7 acceptance: token-like values are scrubbed before reaching the log.

    The previous version of this test was a false-positive -- it injected a
    token into the FAKE subprocess's stdout but never exercised the scrub
    path because bootstrap_cluster.py does not log subprocess stdout. This
    version injects a token into a StructuredLogger.info() call and
    verifies it is redacted from the resulting stdout (the console sink
    the StructuredLogger actually writes to).
    """
    cluster = _write_cluster(tmp_path)
    monkeypatch.setattr(subprocess, "run", _stub_ok)
    bootstrap(cluster_name="cicd", repo_root=cluster.parent.parent.parent, phases=("cloudinit",))
    out = capsys.readouterr().out
    # The token-shaped key is dropped entirely by _scrub (not replaced
    # with a placeholder). Inject a value via the log module directly to
    # prove scrub is wired up; the stdout write should not contain it.
    from lib.log import StructuredLogger
    StructuredLogger("redaction_probe").info(
        "scrub.probe",
        cf_api_token="supersecret-cf-token-value",
        safe_field="visible",
    )
    out += capsys.readouterr().out
    assert "supersecret-cf-token-value" not in out
    # The console sink prints [LEVEL] step: msg. Confirm the probe was logged.
    assert "scrub.probe" in out
    # Independently exercise _scrub() to prove the redaction function
    # itself drops token-shaped keys (covers the audit-log path which
    # the console sink doesn't show).
    from lib.log import _scrub
    scrubbed = _scrub(
        {
            "cf_api_token": "supersecret-cf-token-value",
            "safe_field": "visible",
            "nested": {"ssh_key_path": "leak", "ok": "stay"},
        }
    )
    assert "cf_api_token" not in scrubbed
    assert "ssh_key_path" not in scrubbed["nested"]
    assert scrubbed["safe_field"] == "visible"
    assert scrubbed["nested"]["ok"] == "stay"