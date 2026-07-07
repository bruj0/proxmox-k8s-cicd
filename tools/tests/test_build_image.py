"""Tests for tools.build_image — Ubuntu+k3s golden image builder.

Cross-checks the public contract of BuildImage without spinning up a
real PVE (which would require hardware). The runtime phases are
covered end-to-end by running the CLI against a live host in the
acceptance runbook.

Pinpoints:
  - TEMPLATE_VMID is the canonical 900 slot
  - dataclass construction accepts the full Ubuntu field set
  - main() rejects missing required args with exit code 2
  - _first_pubkey_line drops multi-line Bitwarden exports so the
    cloud-init schema isn't broken by trailing comments/keys
  - BuildImage exposes the canonical Proxmox+Ubuntu recipe
    constants (DISK_STORAGE, BRIDGE, MEMORY_MB, DISK_SIZE_GB)
    as module-level constants the cluster OpenTofu module can
    read indirectly via build/image-id.txt + versions.lock.yaml.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tools.build_image import (
    DISK_SIZE_GB,
    DISK_STORAGE,
    MEMORY_MB,
    TEMPLATE_VMID,
    BuildImage,
    _first_pubkey_line,
    main,
)


def _defaults(tmp_path: Path, **kwargs):
    audit_path = tmp_path / "audit.log"
    defaults = dict(
        pve_endpoint="https://10.0.0.1:8006/api2/json",
        pve_node="BigBertha",
        pve_token_id="k3s-terraform@pam!tf",
        pve_token_secret="00000000-0000-0000-0000-000000000000",
        pve_ssh_host="kvm.bruj0.net",
        pve_ssh_port=6022,
        ssh_pubkey_path=tmp_path / "id_rsa.pub",
        ubuntu_image_version="noble",
        k3s_channel="stable",
        build_dir=tmp_path / "build",
        versions_yaml=tmp_path / "versions.yaml",
        log_dir=tmp_path / "logs",
        audit_log=audit_path,
    )
    defaults.update(kwargs)
    (tmp_path / "id_rsa.pub").write_text("ssh-ed25519 AAAA test@host\n")
    return defaults


def test_template_vmid_is_900() -> None:
    """VMID 900 is the canonical Ubuntu+k3s template slot.

    Set to 900 on 2026-07-07 to match the operator skill's recipe
    and avoid the stuck LV on VMID 950 (the cluster root reads the
    live value from build/image-id.txt via the SS1->SS2 contract,
    so any future VMID bump just requires updating build/image-id.txt
    — not the cluster root HCL).
    """
    assert TEMPLATE_VMID == 900


def test_canonical_recipe_constants() -> None:
    """The Proxmox+Ubuntu recipe constants are pinned in the build
    module so the cluster OpenTofu module can re-derive its
    expected values from versions.lock.yaml without drift."""
    assert DISK_STORAGE == "data1"
    assert MEMORY_MB == 4096
    assert DISK_SIZE_GB == 32


def test_build_image_construction_accepts_full_field_set(
    tmp_path: Path,
) -> None:
    """Dataclass accepts the Ubuntu-era fields with no surprises."""
    bi = BuildImage(**_defaults(tmp_path))
    assert bi.ubuntu_image_version == "noble"
    assert bi.k3s_channel == "stable"
    assert bi.pve_ssh_port == 6022
    assert bi.ssh_pubkey_path.exists()


def test_main_rejects_missing_required_args(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys,
) -> None:
    """Without --pve-endpoint, --pve-token-id, --pve-token-secret the
    CLI must exit non-zero with a structured stderr error before any
    PVE side-effects."""
    monkeypatch.setattr(sys, "argv", ["build_image"])
    monkeypatch.setenv("PVE_ENDPOINT", "")
    monkeypatch.setenv("PVE_TOKEN_ID", "")
    monkeypatch.setenv("PVE_TOKEN_SECRET", "")
    monkeypatch.setenv("PVE_HOST", "")
    monkeypatch.setenv("PVE_SSH_PORT", "")
    rc = main()
    captured = capsys.readouterr()
    assert rc == 2
    assert "--pve-endpoint" in captured.err


def test_first_pubkey_line_strips_bitwarden_multi_key_export(
    tmp_path: Path,
) -> None:
    """Bitwarden exports can include multiple `ssh-add -L` keys.

    The canonical Proxmox+Ubuntu recipe uses ``qm set --sshkeys``
    with a *file path* on the PVE host — so we need to write
    exactly one key per file. ``_first_pubkey_line`` is the
    single-line-of-defense helper that the build's
    ``_configure_cloudinit_drive`` calls before scp'ing the key
    to /tmp/build-image-seed-ssh.pub on PVE.

    Regression pin: if a future refactor reverts to feeding the
    raw multi-line file, the PVE ``qm set`` command will store a
    malformed ``--sshkeys`` value and clones will fail to
    authorize SSH key auth. This test catches that.
    """
    pub = tmp_path / "id_rsa.pub"
    pub.write_text(
        "# bitwarden export header\n"
        "ssh-ed25519 AAAA1111111111111111111111111111111111111111111 "
        "kvm@bruj0-primary\n"
        "ssh-ed25519 AAAA2222222222222222222222222222222222222222222 "
        "kvm@bruj0-secondary\n"
    )
    first = _first_pubkey_line(pub)
    assert first.startswith("ssh-ed25519 ")
    assert "kvm@bruj0-primary" in first
    assert "secondary" not in first, (
        "multi-line Bitwarden export was not truncated to a single"
        " key — this would break PVE's --sshkeys storage and"
        " clones would fail SSH key auth"
    )

    # And a single-line file should round-trip cleanly.
    pub.write_text("ssh-ed25519 AAAA onlyone key@host\n")
    assert _first_pubkey_line(pub) == "ssh-ed25519 AAAA onlyone key@host"


def test_first_pubkey_line_rejects_all_comments(
    tmp_path: Path,
) -> None:
    """A pubkey file containing only comments must raise — the
    caller (the build) should never silently pass an empty key
    to PVE's --sshkeys."""
    pub = tmp_path / "id_rsa.pub"
    pub.write_text("# nothing useful here\n# also nothing\n")
    with pytest.raises(ValueError, match="no non-comment pubkey"):
        _first_pubkey_line(pub)