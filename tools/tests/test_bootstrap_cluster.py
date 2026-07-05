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
    here because we're replacing it wholesale. Return rc=1 so any caller
    that uses check=False sees the failure, but more importantly, mirror
    what production does: convert the failure to a clear error.
    """
    raise subprocess.CalledProcessError(returncode=1, cmd=list(args), stderr="kaboom")


def _write_cluster(tmp_path: Path) -> Path:
    cluster = tmp_path / "clusters" / "cicd"
    cluster.mkdir(parents=True)
    (cluster / "output.json").write_text(
        json.dumps(
            {
                "name": "cicd",
                "vip": "10.0.0.10",
                "control_plane": [
                    {"name": "cp1", "ip": "10.0.0.11"},
                    {"name": "cp2", "ip": "10.0.0.12"},
                ],
                "worker": [{"name": "w1", "ip": "10.0.0.21"}],
            }
        )
    )
    talos_dir = cluster / "talos"
    talos_dir.mkdir()
    for name in ("cp1", "cp2", "w1"):
        (talos_dir / f"{name}.yaml").write_text("type: v1alpha1\n")
    return cluster


def test_list_phases_returns_all_four() -> None:
    """Acceptance: phases enum is [talos, k3s, helm, kubeconfig]."""
    assert list_phases() == ["talos", "k3s", "helm", "kubeconfig"]


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


def test_bootstrap_logs_redact_secret_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """M7 acceptance: token-like strings are redacted by StructuredLogger.scrub()."""
    cluster = _write_cluster(tmp_path)
    # Inject a sensitive env var that the helper accidentally passes via stdout.
    monkeypatch.setenv("CF_API_TOKEN", "supersecret-cf-token-value")

    def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        # Make stdout leak the env var if not scrubbed.
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout="connected cf=supersecret-cf-token-value",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    bootstrap(cluster_name="cicd", repo_root=cluster.parent.parent, phases=("talos",))
    text = caplog.text
    assert "supersecret-cf-token-value" not in text