"""Tests for tools.build_image — the main CLI.

TDD red-phase coverage of the three misfits this WP addresses:

  M1 (Packer race)       — second invocation while first holds the lock
                           must exit non-zero with a structured error.
  M8 (compatibility)     — unknown Talos version must exit non-zero with
                           a structured error before any PVE call.
  M4 (silent failure)    — Packer returning non-zero must (a) emit
                           structured error, (b) destroy the half-baked VM,
                           (c) leave build/image-id.txt unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.build_image import BuildImage
from tools.lib.log import StructuredLogger


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VERSIONS_YAML = """\
talos:
  v1.10.0:
    pve_kernel_min: "6.8"
    k3s_max: v1.34.x
    cilium_max: 1.16.x
    notes: known-good on BigBertha
"""


def _write_versions(path: Path, content: str = VERSIONS_YAML) -> Path:
    path.write_text(content)
    return path


def _build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **kwargs):
    """Construct a BuildImage rooted in tmp_path with sensible defaults."""
    audit_path = tmp_path / "audit.log"
    versions = _write_versions(tmp_path / "versions.yaml")
    logger = StructuredLogger("test", log_path=audit_path)
    defaults = dict(
        talos_version="v1.10.0",
        pve_endpoint="https://10.0.0.1:8006/api2/json",
        pve_node="bigbertha",
        pve_token_id="terraform@pve!k3s",
        pve_token_secret="prod-token-secret",
        build_dir=tmp_path / "build",
        versions_yaml=versions,
        logger=logger,
        verbose=False,
        dry_run=False,
    )
    defaults.update(kwargs)
    return BuildImage(**defaults), audit_path


# ---------------------------------------------------------------------------
# M8 — Compatibility: unknown Talos version exits non-zero with structured error
# ---------------------------------------------------------------------------

def test_unknown_talos_version_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bi, audit = _build(tmp_path, monkeypatch, talos_version="v9.9.9")

    packer_called = False

    def fail_if_called(self):  # type: ignore[no-untyped-def]
        nonlocal packer_called
        packer_called = True
        raise RuntimeError("Packer should not be called when version_check fails")

    monkeypatch.setattr("tools.build_image.BuildImage._run_packer", fail_if_called)

    rc = bi.run()
    assert rc != 0
    assert not packer_called, "Packer was called even though version_check failed"

    # Audit log must contain a version_check entry with the error and the
    # resolution suggestion. This is M4 (no silent failures).
    parsed = [json.loads(line) for line in audit.read_text().strip().split("\n")]
    version_check = [p for p in parsed if p.get("step") == "version_check"]
    assert len(version_check) == 1
    assert "v9.9.9" in version_check[0]["error"]
    assert "resolution" in version_check[0]


def test_known_talos_version_proceeds(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bi, _ = _build(tmp_path, monkeypatch)

    packer_called = False

    def fake_run_packer(self):  # type: ignore[no-untyped-def]
        nonlocal packer_called
        packer_called = True
        # Simulate successful build by writing image-id.txt.
        (self.build_dir / "image-id.txt").write_text("900\n")
        return 0

    monkeypatch.setattr("tools.build_image.BuildImage._run_packer", fake_run_packer)
    rc = bi.run()
    assert rc == 0
    assert packer_called


# ---------------------------------------------------------------------------
# Idempotency: image-id.txt already contains 900 → skip Packer
# ---------------------------------------------------------------------------

def test_idempotent_skip_when_image_id_file_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bi, audit = _build(tmp_path, monkeypatch)
    bi.build_dir.mkdir(parents=True, exist_ok=True)
    (bi.build_dir / "image-id.txt").write_text("900\n")

    packer_called = False

    def fail_if_called(self, *a, **kw):  # type: ignore[no-untyped-def]
        nonlocal packer_called
        packer_called = True
        raise RuntimeError("Packer should not be called")

    monkeypatch.setattr("tools.build_image.BuildImage._run_packer", fail_if_called)
    rc = bi.run()
    assert rc == 0
    assert not packer_called
    parsed = [json.loads(line) for line in audit.read_text().strip().split("\n")]
    skip = [p for p in parsed if p.get("step") == "idempotent_skip"]
    assert len(skip) == 1


# ---------------------------------------------------------------------------
# M4 — Silent failures: Packer returns non-zero → emit error + cleanup VM
# ---------------------------------------------------------------------------

def test_packer_failure_emits_structured_error_and_destroys_vm(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from tools.build_image import _PackerFailed

    bi, audit = _build(tmp_path, monkeypatch)

    destroyed: list[int] = []

    def fake_run_packer_fail(self):  # type: ignore[no-untyped-def]
        raise _PackerFailed("Packer exit code 1")

    def fake_destroy(self, vmid: int) -> None:
        destroyed.append(vmid)

    monkeypatch.setattr("tools.build_image.BuildImage._run_packer", fake_run_packer_fail)
    monkeypatch.setattr("tools.build_image.BuildImage._destroy_vm", fake_destroy)

    rc = bi.run()
    assert rc != 0
    assert destroyed == [900], f"VM 900 must be destroyed on Packer failure, got {destroyed}"

    parsed = [json.loads(line) for line in audit.read_text().strip().split("\n")]
    packer_steps = [p for p in parsed if p.get("step") == "packer_failed"]
    assert len(packer_steps) == 1
    assert "resolution" in packer_steps[0]
    cleanup_steps = [p for p in parsed if p.get("step") == "cleanup_destroy_vm"]
    assert any(900 == c.get("vmid") for c in cleanup_steps)

    # image-id.txt must NOT exist (the build never completed).
    assert not (bi.build_dir / "image-id.txt").exists()


def test_secrets_never_logged_even_on_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from tools.build_image import _PackerFailed

    bi, audit = _build(
        tmp_path,
        monkeypatch,
        pve_token_secret="DEFINITELY-LEAK-SENTINEL-VALUE",
    )

    def fake_run_packer_fail(self):  # type: ignore[no-untyped-def]
        raise _PackerFailed("boom")

    monkeypatch.setattr(
        "tools.build_image.BuildImage._run_packer", fake_run_packer_fail
    )
    monkeypatch.setattr(
        "tools.build_image.BuildImage._destroy_vm", lambda self, vmid: None
    )

    rc = bi.run()
    assert rc != 0

    content = audit.read_text()
    assert "DEFINITELY-LEAK-SENTINEL-VALUE" not in content


# ---------------------------------------------------------------------------
# M1 — Packer race: lock file prevents concurrent builds
# ---------------------------------------------------------------------------

def test_concurrent_run_is_blocked_by_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bi, audit = _build(tmp_path, monkeypatch)
    bi.build_dir.mkdir(parents=True, exist_ok=True)

    # Pretend another process holds the lock.
    lock_path = bi.build_dir / ".build.lock"
    lock_path.write_text("9999")  # fake PID

    rc = bi.run()
    assert rc != 0

    parsed = [json.loads(line) for line in audit.read_text().strip().split("\n")]
    race_steps = [p for p in parsed if p.get("step") == "lock_held"]
    assert len(race_steps) == 1
    assert "resolution" in race_steps[0]


def test_lock_is_acquired_and_released(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bi, _ = _build(tmp_path, monkeypatch)
    bi.build_dir.mkdir(parents=True, exist_ok=True)

    acquired_within: list[Path | None] = []
    released_within: list[Path | None] = []

    def fake_run_packer(self):  # type: ignore[no-untyped-def]
        lock = self.build_dir / ".build.lock"
        acquired_within.append(None if not lock.exists() else lock)
        (self.build_dir / "image-id.txt").write_text("900\n")
        released_within.append(None if lock.exists() else lock)
        return 0

    monkeypatch.setattr(
        "tools.build_image.BuildImage._run_packer", fake_run_packer
    )
    rc = bi.run()
    assert rc == 0
    # During the run, the lock file existed. After the run, it's gone.
    assert acquired_within == [bi.build_dir / ".build.lock"], acquired_within
    assert released_within == [None], released_within


# ---------------------------------------------------------------------------
# Dry run: must NOT touch PVE / Packer / image-id.txt; must log dry_run step
# ---------------------------------------------------------------------------

def test_dry_run_does_not_invoke_packer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bi, audit = _build(tmp_path, monkeypatch, dry_run=True)

    packer_called = False

    def fail(self, *a, **kw):  # type: ignore[no-untyped-def]
        nonlocal packer_called
        packer_called = True
        raise RuntimeError("must not be called in --dry-run")

    monkeypatch.setattr("tools.build_image.BuildImage._run_packer", fail)
    rc = bi.run()
    assert rc == 0
    assert not packer_called
    assert not (bi.build_dir / "image-id.txt").exists()
    parsed = [json.loads(line) for line in audit.read_text().strip().split("\n")]
    assert any(p.get("step") == "dry_run" for p in parsed)