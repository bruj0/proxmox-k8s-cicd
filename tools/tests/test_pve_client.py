"""Tests for tools.lib.pve_client.PveClient.

The PveClient is a thin subprocess wrapper. Tests mock subprocess.run and
verify the constructed command lines + error translation.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tools.lib.log import StructuredLogger
from tools.lib.pve_client import PveClient


def _make_logger(tmp_path: Path) -> StructuredLogger:
    return StructuredLogger("test", log_path=tmp_path / "audit.log")


def _ok(completed: subprocess.CompletedProcess) -> PveClient.RunResult:
    return PveClient.RunResult(returncode=0, stdout="", stderr="")


def test_destroy_vm_invokes_qm_destroy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, list[str]] = {}

    def fake_run(args, *, check, capture_output, text, timeout):  # type: ignore[no-untyped-def]
        captured["args"] = args
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    client = PveClient(_make_logger(tmp_path))
    client.destroy_vm(vmid=900)

    assert captured["args"][0] == "qm"
    assert "destroy" in captured["args"]
    assert "900" in captured["args"]


def test_destroy_vm_continues_on_qm_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Cleanup must be best-effort — non-zero qm returncode must not raise."""

    def fake_run(args, *, check, capture_output, text, timeout):  # type: ignore[no-untyped-def]
        return subprocess.CompletedProcess(
            args=args, returncode=1, stdout="", stderr="VM not found"
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    client = PveClient(_make_logger(tmp_path))
    # Should not raise.
    client.destroy_vm(vmid=999)


def test_get_template_vmid_parses_qm_list(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sample = (
        "      VMID  NAME             STATUS\n"
        "       100  base             stopped\n"
        "       900  talos-v1.10.0    stopped\n"
    )

    def fake_run(args, *, check, capture_output, text, timeout):  # type: ignore[no-untyped-def]
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout=sample, stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    client = PveClient(_make_logger(tmp_path))

    vmid = client.find_template_vmid("talos-v1.10.0")
    assert vmid == 900


def test_find_template_returns_none_when_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sample = "      VMID  NAME\n       100  base\n"

    def fake_run(args, *, check, capture_output, text, timeout):  # type: ignore[no-untyped-def]
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout=sample, stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    client = PveClient(_make_logger(tmp_path))
    assert client.find_template_vmid("missing") is None