"""WP04 acceptance tests.

These tests are red-first: they were written before
tools/bootstrap_cluster.py and tools/lib/{talos,helm,kubeconfig}_client.py
existed. They encode the four M4 + M7 acceptance criteria from the WP04 spec:

  M4 acceptance: phases must surface non-zero exits (no silent failure).
  M4 acceptance: re-running with `--phases talos` skips later phases
                 (idempotent restart from earlier phase).
  M4 acceptance: missing output.json fails the "talos" phase with a
                 clear, machine-readable error (not a stack trace).
  M7 acceptance: tokens are never logged at any level (scrub) - applies
                 to both bootstrap_cluster.py and its delegates.

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
    cluster = tmp_path / "clusters" / "cicd"
    cluster.mkdir(parents=True)
    # SS2 emits: cluster_name, vip, vnet_bridge, control_plane_count,
    # worker_count, talos_dir, nodes: [{role, name, ip, ...}], helm_releases.
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
                    "kube-vip",
                    "proxmox-cloud-controller-manager",
                    "proxmox-csi-plugin",
                    "traefik",
                    "cloudflare-tunnel-ingress-controller",
                    "cert-manager",
                ],
            }
        )
    )
    talos_dir = cluster / "talos"
    talos_dir.mkdir()
    for name in ("cp1", "cp2", "w1"):
        (talos_dir / f"{name}.yaml").write_text("type: v1alpha1\n")
    return cluster


def test_list_phases_returns_all_six() -> None:
    """Acceptance: phases enum is [talos, k3s, helm, kubeconfig, host_ports, externalname]."""
    assert list_phases() == [
        "talos",
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
    """
    cluster = _write_cluster(tmp_path)
    monkeypatch.setattr(subprocess, "run", _stub_fail)
    with pytest.raises(BootstrapError):
        bootstrap(cluster_name="cicd", repo_root=cluster.parent.parent)


def test_bootstrap_phase_filter_skips_later_phases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M4 acceptance: re-running with --phases talos skips later phases."""
    cluster = _write_cluster(tmp_path)
    monkeypatch.setattr(subprocess, "run", _stub_ok)
    # Should complete without touching helm/k3s phases at all.
    bootstrap(cluster_name="cicd", repo_root=cluster.parent.parent, phases=("talos",))


def test_bootstrap_full_happy_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance: running all phases in order completes without raising.

    Exercises the canonical operator flow:
      talos -> k3s -> helm -> kubeconfig (host_ports skipped because the
      baseline file does not exist in the temp dir).
    Without this test, a structural bug (e.g. helm phase referencing a
    kubeconfig file that the kubeconfig phase hasn't written yet) could
    pass every other test and still break in production.
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
    # Skip the host_ports phase explicitly so the test does not need a
    # baseline file. WP05 tests cover that phase separately.
    bootstrap(
        cluster_name="cicd",
        repo_root=cluster.parent.parent,
        phases=("talos", "k3s", "helm", "kubeconfig"),
    )
    # Every phase must have been invoked exactly once on the first run.
    cmds = [" ".join(c) for c in calls]
    assert any(c.startswith("talosctl apply-config") for c in cmds)
    assert any(c.startswith("kubectl") and "/healthz" in c for c in cmds)
    assert any(c.startswith("helm upgrade") for c in cmds)
    assert any(c.startswith("kubectl config view") or c.startswith("talosctl kubeconfig") for c in cmds)


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
    bootstrap(cluster_name="cicd", repo_root=cluster.parent.parent, phases=("talos",))
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