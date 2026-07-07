"""Tests for tools.lib.pve_client.PveClient.

The PveClient is a thin subprocess wrapper. Tests mock subprocess.Popen
(the actual entry point used by PveClient._run) and verify the
constructed command lines + error translation.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from tools.lib.log import StructuredLogger
from tools.lib.pve_client import PveClient


def _make_logger(tmp_path: Path) -> StructuredLogger:
    return StructuredLogger("test", log_path=tmp_path / "audit.log")


def _ok(completed: subprocess.CompletedProcess) -> PveClient.RunResult:
    return PveClient.RunResult(returncode=0, stdout="", stderr="")


class _FakePopen:
    """Drop-in replacement for subprocess.Popen that completes synchronously.

    Mirrors the real Popen's `.communicate()` and `.returncode` surface so
    PveClient._run's timeout / non-zero exit code paths still exercise
    the same control flow.
    """

    def __init__(
        self,
        args: list[str],
        *,
        stdin: Any = None,
        stdout: Any = None,
        stderr: Any = None,
        text: bool = False,
        **kwargs: Any,
    ) -> None:
        self.args = args
        self.stdout_value = ""
        self.stderr_value = ""
        self.returncode = 0
        self._timed_out = False

    def communicate(self, timeout: float = None):  # type: ignore[no-untyped-def]
        if self._timed_out:
            raise subprocess.TimeoutExpired(self.args, timeout or 0)
        return self.stdout_value, self.stderr_value

    def kill(self) -> None:
        pass

    def set_outputs(self, stdout: str, stderr: str, returncode: int) -> "_FakePopen":
        self.stdout_value = stdout
        self.stderr_value = stderr
        self.returncode = returncode
        return self

    def set_timeout(self) -> "_FakePopen":
        self._timed_out = True
        return self


def test_destroy_vm_invokes_qm_destroy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, list[str]] = {}

    def fake_popen(args, **kwargs):  # type: ignore[no-untyped-def]
        captured["args"] = list(args)
        return _FakePopen(args).set_outputs("", "", 0)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    client = PveClient(_make_logger(tmp_path))
    client.destroy_vm(vmid=900)

    assert captured["args"][0] == "qm"
    assert "destroy" in captured["args"]
    assert "900" in captured["args"]


def test_destroy_vm_continues_on_qm_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Cleanup must be best-effort — non-zero qm returncode must not raise."""

    def fake_popen(args, **kwargs):  # type: ignore[no-untyped-def]
        return _FakePopen(args).set_outputs("", "VM not found", 1)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
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

    def fake_popen(args, **kwargs):  # type: ignore[no-untyped-def]
        return _FakePopen(args).set_outputs(sample, "", 0)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    client = PveClient(_make_logger(tmp_path))

    vmid = client.find_template_vmid("talos-v1.10.0")
    assert vmid == 900


def test_find_template_returns_none_when_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sample = "      VMID  NAME\n       100  base\n"

    def fake_popen(args, **kwargs):  # type: ignore[no-untyped-def]
        return _FakePopen(args).set_outputs(sample, "", 0)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    client = PveClient(_make_logger(tmp_path))
    assert client.find_template_vmid("missing") is None


def test_run_captures_partial_stderr_on_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """On timeout, _run must capture whatever stderr was buffered so
    the operator can see *why* the call hung (e.g. PVE lock contention
    produces 'trying to acquire lock...' messages before we kill it)."""

    class _TimeoutThenPartialFake(_FakePopen):
        def __init__(self) -> None:
            super().__init__(["qm", "shutdown", "900"])
            self._calls = 0

        def communicate(self, timeout: float = None):  # type: ignore[no-untyped-def]
            self._calls += 1
            if self._calls == 1:
                raise subprocess.TimeoutExpired(self.args, timeout or 0)
            # Second call (after kill) — return partial stderr
            return "", "trying to acquire lock..."

    fake = _TimeoutThenPartialFake()
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: fake)

    from tools.lib.pve_client import PveError

    client = PveClient(_make_logger(tmp_path))
    with pytest.raises(PveError) as exc_info:
        client._run(["qm", "shutdown", "900"], timeout=0.01, allow_failure=False)
    # PveError must carry the partial stderr in its message
    assert "TIMEOUT" in exc_info.value.stderr
    assert "trying to acquire lock" in exc_info.value.stderr


def test_run_returns_result_on_timeout_when_allow_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """allow_failure=True on a timed-out call returns the partial result
    rather than raising — used by best-effort cleanup paths."""

    class _TimeoutThenPartialFake(_FakePopen):
        def __init__(self) -> None:
            super().__init__(["qm", "shutdown", "900"])
            self._calls = 0

        def communicate(self, timeout: float = None):  # type: ignore[no-untyped-def]
            self._calls += 1
            if self._calls == 1:
                raise subprocess.TimeoutExpired(self.args, timeout or 0)
            return "", "lock wait timeout"

    fake = _TimeoutThenPartialFake()
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: fake)

    client = PveClient(_make_logger(tmp_path))
    result = client._run(
        ["qm", "shutdown", "900"], timeout=0.01, allow_failure=True
    )
    assert result.stderr == "lock wait timeout"