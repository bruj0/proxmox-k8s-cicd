"""WP07 acceptance tests for the live-host proxmox-k3s-pipeline skill.

Encodes the Agent Skill acceptance criteria from NFR-010, NFR-011, NFR-012
and the WP07 prompt's Acceptance Criteria list, updated for the
2026-07-07 refactor (Talos v1.10 -> v1.13, Packer -> Python/Image-Factory,
PowerDNS, sync_dns_to_sdn.py):

  NFR-010: SKILL.md has YAML frontmatter with `name` and non-empty `description`.
  NFR-011: skill idempotency (running from clean state vs partial state
    converges to the same end state). We exercise this by asserting the
    skill documents both 'first run' and 'rerun / partial state' paths
    in its body.
  NFR-012: SKILL.md mentions every external library with version pin and
    rationale. We assert a curated list of (library, version) pairs.
  Acceptance: SKILL.md's library-version table calls out the
    context7-auto-research gate.
  Acceptance: all four runbooks exist and contain a copy-pasteable
    command block (bash fenced code).
  Acceptance (2026-07-07): SKILL.md documents the canonical
    Sidero Image Factory + qm importdisk + talosctl --install-image
    flow (NOT Packer), the Bitwarden SSH agent requirement, the
    pre-enrolled-keys Secure-Boot fix, the boot-order flip in
    the template, and the sync_dns_to_sdn.py post-apply fixup.

Side-effect guarantee: tests only read files. No subprocess, no PVE.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

# Project root is two levels up from this test file (tools/tests -> tools -> root).
ROOT = Path(__file__).resolve().parent.parent.parent
SKILL_PATH = ROOT / ".agents" / "skills" / "proxmox-k3s-pipeline" / "SKILL.md"
VERSIONS_PATH = (
    ROOT / ".agents" / "skills" / "proxmox-k3s-pipeline" / "versions.lock.yaml"
)
RUNBOOKS = ROOT / "docs" / "runbooks"
TOOLS_LIB = ROOT / "tools" / "lib"
SCRIPTS = ROOT / "scripts"


# ---------- frontmatter acceptance (NFR-010) ----------


def test_skill_md_exists() -> None:
    """WP07 T001 + NFR-010: SKILL.md exists at the canonical path."""
    assert SKILL_PATH.is_file(), f"SKILL.md missing at {SKILL_PATH}"


def test_skill_md_frontmatter_has_name_and_description() -> None:
    """NFR-010: YAML frontmatter must have `name` and non-empty `description`."""
    text = SKILL_PATH.read_text()
    # Frontmatter is between two --- lines at the top of the file.
    m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert m is not None, "SKILL.md does not start with YAML frontmatter"
    fm = m.group(1)
    assert re.search(r"^name:\s*\S+", fm, re.MULTILINE), (
        "frontmatter missing `name:` field"
    )
    # description must be non-empty (>= 40 chars to filter out placeholder stubs)
    desc_m = re.search(r"^description:\s*(.+)$", fm, re.MULTILINE)
    assert desc_m is not None, "frontmatter missing `description:` field"
    desc = desc_m.group(1).strip()
    assert len(desc) >= 40, (
        f"`description` is too short ({len(desc)} chars); must be a real prose "
        "description of when to load the skill"
    )


# ---------- version-pinned library mention (NFR-012) ----------


def test_skill_md_mentions_every_external_library_with_version() -> None:
    """NFR-012: every external library must appear in SKILL.md with a
    version pin (e.g. `v1.34.x` or `0.111.1`) and a rationale sentence.

    The (library, version-substring, rationale-substring) tuples below
    are the canonical contract; if any are missing, NFR-012 fails.

    Updated 2026-07-07:
      - talosctl 1.10 -> 1.13.x (matches Talos v1.13.5 image)
      - + pan-net/powerdns 1.5.0 (Phase 2 DNS records)
    """
    text = SKILL_PATH.read_text().lower()
    required = [
        ("bpg/proxmox", "0.111.1"),
        ("pan-net/powerdns", "1.5.0"),
        ("strrl/cloudflare-tunnel-ingress-controller", "0.0.23"),
        ("cilium", "1.16"),
        # 2026-07-08 update: sergelogvinov charts moved to OCI in
        # late 2025; the old HTTP paths 404. The skill must use the
        # OCI ref. Chart versions are pinned to what the live host
        # validated via `ghcr.io/v2/.../tags/list`.
        ("oci://ghcr.io/sergelogvinov/charts/proxmox-cloud-controller-manager", "0.2.29"),
        ("oci://ghcr.io/sergelogvinov/charts/proxmox-csi-plugin", "0.5.9"),
        # kube-vip 0.9.9 with the config.address + env.cp_enable
        # values shape (NOT controlPlane.enabled, which never
        # shipped in any released chart).
        ("kube-vip", "0.9.9"),
        # cert-manager 1.20.x.
        ("cert-manager", "1.20"),
        ("talosctl", "1.13"),
        ("k3s", "1.34"),
        ("helm", "3"),  # major version pin
    ]
    missing = [
        (lib, ver) for (lib, ver) in required
        if not (lib.lower() in text and ver.lower() in text)
    ]
    assert not missing, (
        f"NFR-012 violation: SKILL.md must mention each library with a version "
        f"pin. Missing: {missing}"
    )
    # Rationale check: at least one occurrence of "rationale" (case-insensitive)
    # OR an explicit "why this version" sentence must appear. The skill uses
    # the context7-auto-research model, which records rationale per library
    # (see Step 0). Assert at least 5 mentions of "rationale" so we know the
    # skill teaches the agent to surface the rationale, not just the version.
    assert text.count("rationale") >= 5, (
        "NFR-012: SKILL.md must surface library-version rationale (>= 5 "
        "occurrences of the word 'rationale'). The skill must teach the agent "
        "to record WHY each version is pinned, not just WHAT version."
    )


def test_skill_md_excludes_obsolete_hashicorp_proxmox_packer() -> None:
    """2026-07-07 v2 cleanup: the pipeline no longer uses Packer OR
    Sidero Image Factory. The skill must NOT pin hashicorp/proxmox
    Packer plugin OR recommend Image Factory for Phase 1. The skill
    MUST call out the canonical Proxmox+Ubuntu recipe
    (virt-customize + qm + native cloud-init drive) instead."""
    text = SKILL_PATH.read_text().lower()
    # The OLD library table pinned hashicorp/proxmox 1.2.3 (Packer plugin).
    # It should be gone now -- we don't use Packer.
    assert "hashicorp/proxmox 1.2.3" not in text, (
        "SKILL.md still references the Packer plugin hashicorp/proxmox 1.2.3;"
        " Phase 1 was rewritten to use the canonical Proxmox+Ubuntu recipe"
        " in 2026-07-07 -- remove the obsolete pin."
    )
    # The canonical Proxmox+Ubuntu recipe MUST be documented.
    assert "virt-customize" in text, (
        "SKILL.md must document virt-customize (libguestfs-tools) as"
        " the load-bearing step that bakes qemu-guest-agent into the"
        " cloud image BEFORE the VM is created."
    )
    assert "native cloud-init drive" in text or "cloudinit drive" in text or (
        "ide2 data1:cloudinit" in text
    ), (
        "SKILL.md must document Proxmox's native cloud-init drive"
        " (--ide2 data1:cloudinit) as the seed mechanism, NOT a custom"
        " NoCloud seed ISO."
    )
    # The old Image Factory / talosctl --install-image path is gone.
    assert "factory.talos.dev" not in text, (
        "SKILL.md must not reference the Sidero Image Factory anymore;"
        " the v2 cleanup pivoted to vanilla Ubuntu+k3s."
    )
    assert "--install-image" not in text, (
        "SKILL.md must not reference talosctl --install-image anymore;"
        " the v2 cleanup pivoted to vanilla Ubuntu+k3s."
    )


# ---------- context7 gate (Step 0) ----------


def test_skill_md_step_0_instructs_context7_gate() -> None:
    """Acceptance criterion: SKILL.md must instruct the agent to load
    `.agents/skills/context7-auto-research/SKILL.md` before invoking any
    external library."""
    text = SKILL_PATH.read_text()
    # Strip frontmatter
    body = re.sub(r"^---.*?---\n", "", text, count=1, flags=re.DOTALL)
    assert ".agents/skills/context7-auto-research/SKILL.md" in body, (
        "context7-auto-research gate missing from SKILL.md body"
    )
    # Find the line containing the context7 path; assert "before" appears
    # within 20 lines of it.
    lines = body.splitlines()
    for i, line in enumerate(lines):
        if ".agents/skills/context7-auto-research/SKILL.md" in line:
            window = "\n".join(lines[max(0, i - 5): i + 20])
            assert re.search(r"\bbefore\b", window, re.IGNORECASE), (
                f"context7-auto-research gate at line {i} lacks the "
                f"required 'before' qualifier; the skill must require "
                f"context7 research BEFORE invoking any external library."
            )
            return
    pytest.fail("context7-auto-research path not found in SKILL.md body")


# ---------- idempotency (NFR-011) ----------


def test_skill_md_documents_rerun_and_partial_state() -> None:
    """NFR-011: skill must document convergence from partial state and
    rerun idempotency. We accept any of these canonical phrases:
      - "rerun" or "re-run"
      - "partial state" or "already done"
      - "idempotent"
    """
    text = SKILL_PATH.read_text().lower()
    canonical = ["rerun", "re-run", "idempotent", "already done", "partial state"]
    found = [p for p in canonical if p in text]
    assert found, (
        f"NFR-011: SKILL.md must document rerun / partial-state convergence. "
        f"None of {canonical} found."
    )


# ---------- install_k3s sub-phase (2026-07-08) ----------


def test_skill_documents_install_k3s_subphase() -> None:
    """The Phase-4 sub-phase list must include `install_k3s` between
    `cloudinit` and `k3s`, and Step 4a must document the canonical recipe.

    Without `install_k3s`, the pipeline has no Python-side k3s installer
    and falls back to cloud-init runcmd (the old, pre-Pivot recipe).
    """
    text = SKILL_PATH.read_text()
    assert "install_k3s" in text, (
        "SKILL.md must mention the install_k3s sub-phase by exact name"
    )
    # Phase list must order it cloudinit, install_k3s, k3s.
    assert "cloudinit, install_k3s, k3s" in text, (
        "SKILL.md phase list must order: cloudinit, install_k3s, k3s, ..."
    )
    # Step 4a body must describe the installer + the version pin.
    assert "Step 4a" in text, (
        "SKILL.md must include a `Step 4a -- install_k3s sub-phase` section"
    )
    assert "v1.34.9+k3s1" in text, (
        "SKILL.md must pin the k3s install version to v1.34.9+k3s1"
    )
    assert "INSTALL_K3S_VERSION" in text, (
        "SKILL.md must document the INSTALL_K3S_VERSION env var the"
        " upstream installer reads"
    )
    # The mandatory --tls-san=<vip> flag (came out of the VIP verification).
    assert "--tls-san=" in text and "<vip>" in text, (
        "SKILL.md Step 4a must call out --tls-san=<vip> as mandatory for"
        " server installs (see docs/install-k3s-vip-verification.md)"
    )


def test_skill_documents_idempotent_install_k3s() -> None:
    """Step 4a must document the idempotency contract.

    Two gates: upstream install.sh's hash check + the Python wrapper's
    systemctl+kubeconfig short-circuit. Both must be present.
    """
    text = SKILL_PATH.read_text().lower()
    assert "no change detected" in text or "no change detected so skipping" in text, (
        "Step 4a must document the upstream installer's hash-checked"
        " idempotency (the literal 'no change detected so skipping'"
        " message from install.sh)"
    )
    assert "systemctl is-active" in text, (
        "Step 4a must document the Python wrapper's systemctl is-active"
        " short-circuit gate"
    )
    assert "k3s.skip_install" in text, (
        "Step 4a must include the canonical 'k3s.skip_install' log step"
        " that the Python orchestrator emits on a re-run"
    )


# ---------- runbooks (T004-T006 + T008) ----------


def test_runbooks_exist_and_have_copy_paste_blocks() -> None:
    """WP07 T004/T005/T006 + acceptance: each runbook exists and contains
    at least one ```bash fenced code block (copy-pasteable procedure)."""
    required = [
        "cloudflare-fallback.md",
        "scale-workers.md",
        "decommission-cluster.md",
        "rotate-tokens.md",
    ]
    for name in required:
        p = RUNBOOKS / name
        assert p.is_file(), f"runbook missing: {p}"
        text = p.read_text()
        assert "```bash" in text, (
            f"runbook {name} has no ```bash fenced code block; must be "
            f"copy-pasteable per acceptance criteria."
        )


# ---------- versions.lock.yaml (T000) ----------


def test_versions_lock_documents_agentskills_spec() -> None:
    """WP07 T000: versions.lock.yaml must record the agentskills.io
    spec version, the consumers tested, and the cross_check status."""
    assert VERSIONS_PATH.is_file(), f"versions.lock.yaml missing at {VERSIONS_PATH}"
    text = VERSIONS_PATH.read_text()
    assert "agentskills.io" in text
    assert "claude-code" in text
    assert "cursor" in text
    assert "cross_check" in text


def test_versions_lock_documents_live_host_evidence() -> None:
    """After the 2026-07-06 deploy, versions.lock.yaml must
    record live-host evidence (PVE version, kernel, node name,
    Cloudflare zone/account IDs, permission-group UUIDs) so
    future agents can verify the same environment."""
    assert VERSIONS_PATH.is_file(), f"versions.lock.yaml missing at {VERSIONS_PATH}"
    text = VERSIONS_PATH.read_text()
    assert "live_host_evidence" in text, (
        "versions.lock.yaml must have a live_host_evidence section"
    )
    # Proxmox
    assert "kvm.bruj0.net" in text, (
        "live_host_evidence must record the Proxmox host DNS name"
    )
    assert "BigBertha" in text, (
        "live_host_evidence must record the Proxmox node name"
    )
    # Cloudflare
    assert "15e4cfe0ecfee91903601ae780932ad3" in text, (
        "live_host_evidence must record the Cloudflare zone_id"
    )
    assert "2e9c09b27d2a089c531b12ae0f0e6ff3" in text, (
        "live_host_evidence must record the Cloudflare account_id"
    )
    # Permission-group UUIDs
    assert "c8fed203ed3043cba015a93ad1616f1f" in text
    assert "4755a26eedb94da69e1066d98aa820be" in text
    assert "c07321b023e944ff818fec44d8203567" in text


# ---------- 2026-07-07 Phase 1 contracts (Ubuntu+k3s pivot) ----------


def test_skill_documents_ubuntu_cloud_image_source() -> None:
    """Step 1.1 (Ubuntu+k3s, 2026-07-07): SKILL.md must pin the
    Ubuntu cloud image URL and the four components shipped by default.
    The cloud image URL is the single source of truth for the rootfs
    composition; if it changes, the build will produce a different
    image and Phase 2 clones will have different package sets."""
    text = SKILL_PATH.read_text()
    assert (
        "noble-server-cloudimg-amd64.img" in text
        and "cloud-images.ubuntu.com" in text
    ), (
        "SKILL.md must pin the Ubuntu cloud image URL"
        " (noble-server-cloudimg-amd64.img on cloud-images.ubuntu.com)"
        " so future agents cannot drift to a different rootfs composition."
    )
    # The components shipped by the cloud image (apt-installable but
    # already present) and the runtime prerequisites the build verifies.
    for component in [
        "qemu-guest-agent",
        "cloud-init",
        "openssh-server",
    ]:
        assert component in text, (
            f"SKILL.md must mention {component} as part of the canonical"
            f" Ubuntu cloud image composition / build prerequisites."
        )


def test_skill_documents_sidero_schematic_deprecated_marker() -> None:
    """OS pivot (2026-07-07): the canonical pipeline is now Ubuntu+k3s,
    not Talos + Sidero Image Factory. The old Sidero schematic ID
    (ab5430f4...) is kept in versions.yaml for audit but MUST be marked
    deprecated in the live skill so future operators don't re-enable it."""
    text = SKILL_PATH.read_text()
    assert "deprecated" in text.lower() or "no longer" in text.lower(), (
        "SKILL.md must call out the OS pivot from Talos to Ubuntu+k3s"
        " (2026-07-07) so future operators / agents do not re-enable"
        " the Sidero Image Factory schematic."
    )


def test_skill_documents_build_image_py_entry_point() -> None:
    """Step 1.2: SKILL.md must document the tools/build_image.py
    orchestrator as the single Python entry point for Phase 1."""
    text = SKILL_PATH.read_text()
    assert "tools/build_image" in text or "tools.build_image" in text, (
        "SKILL.md must name the Python build orchestrator"
        " (tools/build_image or tools.build_image module path)."
    )
    assert "qm create" in text and "qm template" in text, (
        "SKILL.md must document the qm create + qm template sequence"
        " that the build orchestrator drives."
    )
    assert "talosctl apply-config" in text, (
        "SKILL.md must document talosctl apply-config as the install"
        " trigger (with --install-image)."
    )


def test_skill_documents_pre_enrolled_keys_secure_boot_fix() -> None:
    """Step 1.5.1: SKILL.md must warn that pre-enrolled-keys=1
    breaks Talos v1.13.5 Secure Boot. This was the load-bearing
    Phase-1 gotcha of 2026-07-07; without this, builds hang in
    the OVMF Boot Manager with `Access Denied`."""
    text = SKILL_PATH.read_text()
    assert "pre-enrolled-keys" in text, (
        "SKILL.md must mention the pre-enrolled-keys Secure Boot"
        " interaction with Talos v1.13.5."
    )
    # The fix must be clearly stated: drop pre-enrolled-keys=1
    assert (
        "drop" in text.lower() and "pre-enrolled-keys" in text
    ) or (
        "remove" in text.lower() and "pre-enrolled-keys" in text
    ) or (
        "no `pre-enrolled-keys=1`" in text
        or "NO `pre-enrolled-keys=1`" in text
    ), (
        "SKILL.md must state the fix: drop/remove `pre-enrolled-keys=1`"
        " from the create_template_shell call."
    )


def test_skill_documents_template_boot_order_flip() -> None:
    """Step 1: SKILL.md must document that the template's boot order
    MUST be set to scsi0 (so clones boot from disk, NOT from the
    cloud-init drive or any leftover ISO). The boot order is set
    at `qm create` time in the canonical Proxmox+Ubuntu recipe."""
    text = SKILL_PATH.read_text()
    assert "boot order" in text.lower() or "boot: order" in text, (
        "SKILL.md must explain the template boot order requirement."
    )
    assert "order=scsi0" in text, (
        "SKILL.md must show the corrected boot order: order=scsi0."
    )
    # The recovery path / the qm command that sets the order
    assert "qm set" in text or "qm create" in text, (
        "SKILL.md must reference the qm command that pins"
        " --boot order=scsi0 (either at qm create or via qm set)."
    )


def test_skill_documents_graceful_then_force_stop_fallback() -> None:
    """Step 1.5.5 (Ubuntu+k3s, 2026-07-07): SKILL.md must explain that the
    build uses stop_vm (graceful, 10s) -> wait_for_vm_stopped (30s) ->
    stop_vm_forcible (qm stop, 10s) as a fail-safe when cloud-init /
    qemu-guest-agent / systemd graceful shutdown hangs."""
    text = SKILL_PATH.read_text()
    assert "stop_vm_forcible" in text or "qm stop" in text, (
        "SKILL.md must document the force-stop fallback (stop_vm_forcible"
        " or qm stop) used when graceful shutdown hangs."
    )


def test_skill_documents_initramfs_e2fsck_fix() -> None:
    """v2 cleanup: the canonical Proxmox+Ubuntu recipe handles the
    EXT4 journal corruption risk by running `update-initramfs -u`
    INSIDE the image via `virt-customize --run-command` (not via the
    chroot-on-mounted-disk dance the first Ubuntu build used). The
    skill must document that path."""
    text = SKILL_PATH.read_text()
    # The new build path: virt-customize --run-command 'update-initramfs -u'
    assert "virt-customize" in text and "update-initramfs" in text, (
        "SKILL.md must document the virt-customize path that runs"
        " update-initramfs -u inside the image BEFORE the VM is created."
    )
    # The historical rationale (e2fsck / fsck) is still useful context.
    assert "e2fsck" in text or "fsck" in text, (
        "SKILL.md must still mention e2fsck/fsck as the rationale for"
        " the initramfs regeneration step (carry-over from the first"
        " Ubuntu build's debug history)."
    )


# ---------- 2026-07-07 Phase 2 contracts ----------


def test_skill_documents_bitwarden_ssh_agent_requirement() -> None:
    """Step 0a.9: SKILL.md must explain that PVE SSH access REQUIRES
    SSH_AUTH_SOCK=/home/bruj0/.bitwarden-ssh-agent.sock -- the standard
    OpenSSH agent's key is rejected by PVE."""
    text = SKILL_PATH.read_text()
    assert "/home/bruj0/.bitwarden-ssh-agent.sock" in text, (
        "SKILL.md must name the Bitwarden SSH agent socket path"
        " (/home/bruj0/.bitwarden-ssh-agent.sock) explicitly."
    )
    assert "Bitwarden" in text, (
        "SKILL.md must call out that the SSH agent is Bitwarden's,"
        " not the default OpenSSH agent."
    )
    # The recovery when SSH_AUTH_SOCK is unset
    assert "Permission denied" in text, (
        "SKILL.md must show the failure mode (Permission denied (publickey))"
        " that occurs without SSH_AUTH_SOCK set."
    )


def test_skill_documents_k3s_cluster_role_19_privs() -> None:
    """Step 2.2.3: The k3s-cluster role must extend from the spec
    T005 12-priv set to 19 entries for VM lifecycle / SDN writes."""
    text = SKILL_PATH.read_text()
    assert (
        "Sys.Audit" in text and "VM.Audit" in text and "VM.Clone" in text
    ) and "VM.Config.HWType" in text, (
        "SKILL.md must enumerate the added VM-lifecycle privs"
    )
    assert "19" in text and (
        "12" in text and ("spec" in text.lower() or "T005" in text)
    ), (
        "SKILL.md must compare the new 19-priv total against the"
        " original spec T005 12-priv count."
    )


def test_skill_documents_sdn_ipam_dhcp_allocation() -> None:
    """Step 2.2.7: SKILL.md must explain that PVE's SDN IPAM allocates
    IPs from the DHCP pool (10.0.0.50-200), NOT from var.ip_start, and
    that the post-apply sync_dns_to_sdn.py is required to fix the
    PowerDNS records."""
    text = SKILL_PATH.read_text()
    assert "DHCP" in text, (
        "SKILL.md must explain that the SDN DHCP pool is what assigns"
        " real VM IPs."
    )
    assert "sync_dns_to_sdn" in text, (
        "SKILL.md must name the sync_dns_to_sdn.py post-apply fixup."
    )
    assert "scripts/sync_dns_to_sdn.py" in text or (
        ROOT / "scripts" / "sync_dns_to_sdn.py"
    ).is_file(), (
        "SKILL.md must reference the scripts/sync_dns_to_sdn.py file"
        " (either by literal path in the body or by file presence)."
    )
    # The wrong-IP record table — must show a cidrhost-rendered IP
    # (10.0.1.0, 10.0.1.1, 10.0.2.0, 10.0.2.1) and an SDN-DHCP-allocated
    # IP (10.0.0.50-200) in the same table.
    assert "10.0.1.0" in text, (
        "SKILL.md must show the wrong-IP record (cidrhost-rendered 10.0.1.0)"
    )
    import re as _re
    sdn_dhcp_ips = _re.findall(r"10\.0\.0\.(?:[5-9][0-9]|1[0-9]{2})", text)
    assert sdn_dhcp_ips, (
        "SKILL.md must show concrete SDN-DHCP-allocated IPs"
        " (10.0.0.50-200) in the wrong-vs-actual table."
    )


def test_skill_documents_powerdns_lxc_101() -> None:
    """Step 2.3: SKILL.md must name the PowerDNS LXC container (101)
    and its API endpoint (10.0.0.3:8081) so future agents know where
    the records actually live (NOT on PVE itself)."""
    text = SKILL_PATH.read_text()
    assert "LXC 101" in text or "lxc 101" in text.lower() or (
        "pct exec 101" in text
    ), (
        "SKILL.md must name PowerDNS LXC container 101 as the record store."
    )
    assert "10.0.0.3:8081" in text, (
        "SKILL.md must show the PowerDNS API address 10.0.0.3:8081."
    )


def test_skill_documents_dns_tunnel_local_port() -> None:
    """Step 2.3: SKILL.md must document the SSH tunnel the
    sync_dns_to_sdn.py and apply_tofu.py scripts open to reach
    PowerDNS (10.0.0.3:8081 is not directly reachable from the
    operator host because vnet0 is private to PVE)."""
    text = SKILL_PATH.read_text()
    assert "127.0.0.1:18081" in text or (
        "127.0.0.1:8081" in text and "tunnel" in text.lower()
    ), (
        "SKILL.md must document the SSH tunnel local-port"
        " (127.0.0.1:18081 for sync_dns_to_sdn.py,"
        " 127.0.0.1:8081 for apply_tofu.py) used to reach PowerDNS."
    )


def test_skill_documents_apply_tofu_py_runner() -> None:
    """Step 0d / 2.1: SKILL.md must name scripts/apply_tofu.py as the
    single entry point for `tofu apply` against the tokens and cluster
    roots. The legacy scripts/apply.sh is gone (replaced 2026-07-07)."""
    text = SKILL_PATH.read_text()
    assert "scripts/apply_tofu.py" in text, (
        "SKILL.md must name scripts/apply_tofu.py as the unified"
        " apply entry point."
    )
    assert "apply.sh" not in text, (
        "SKILL.md must NOT reference the legacy scripts/apply.sh"
        " (replaced by apply_tofu.py in 2026-07-07)."
    )


def test_skill_documents_state_backend_layout() -> None:
    """Step 0e.2: SKILL.md must name the 4 state names and the
    project id, and document why the module does not carry state."""
    text = SKILL_PATH.read_text()
    assert "Step 0e" in text, "SKILL.md missing Step 0e (state backend) section"
    assert "infra-tokens" in text and "cluster-cicd" in text and "cluster-apps" in text, (
        "Step 0e.2 must list the three stateful stack names"
    )
    assert "84156476" in text, (
        "Step 0e.2 must document the project id (84156476)"
    )
    assert "proxmox-k3s-cluster-module" in text, (
        "Step 0e.2 must reserve the module's state name even though"
        " the module does not actually carry state"
    )


def test_skill_documents_gitlab_pat_requirement() -> None:
    """Step 0e.3: SKILL.md must spell out the api-scope PAT
    requirement and warn against the OAuth token."""
    text = SKILL_PATH.read_text()
    assert "api" in text and "scope" in text, (
        "Step 0e.3 must mention the api scope requirement"
    )
    assert "OAuth" in text or "oauth" in text, (
        "Step 0e.3 must warn that the glab cached OAuth token is"
        " not suitable (it expires daily)"
    )
    assert "GITLAB_ACCESS_TOKEN" in text, (
        "Step 0e.3 must show how to export GITLAB_ACCESS_TOKEN"
        " from the .env file"
    )


def test_skill_documents_gitlab_backend_helper() -> None:
    """Step 0e.4: SKILL.md must document the
    scripts/gitlab_backend.sh helper and its three sub-commands."""
    text = SKILL_PATH.read_text()
    assert "scripts/gitlab_backend.sh" in text, (
        "Step 0e.4 must name the helper script"
    )
    assert "init" in text and "show" in text, (
        "Step 0e.4 must list the init + show sub-commands"
    )
    assert "-force-copy" in text and "-input=false" in text, (
        "Step 0e.4 must call out the -force-copy + -input=false"
        " flags the helper passes to make migration non-interactive"
    )


def test_skill_documents_force_unlock_recipe() -> None:
    """Step 0e.5: SKILL.md must show the curl-based force-unlock
    recipe for stuck HTTP-backend locks."""
    text = SKILL_PATH.read_text()
    assert "force-unlock" in text or "force_unlock" in text or "DELETE" in text, (
        "Step 0e.5 must show a force-unlock recipe (curl DELETE)"
    )
    # The exact URL pattern
    assert "/terraform/state/" in text, (
        "Step 0e.5 must show the GitLab state-lock API path"
    )


def test_skill_documents_module_no_backend() -> None:
    """Step 0e.6: SKILL.md must explain that the module does NOT
    carry a `backend "http" {}` block, and why."""
    text = SKILL_PATH.read_text()
    assert "module" in text.lower() and "backend" in text.lower(), (
        "Step 0e.6 must explain the module-vs-backend relationship"
    )
    # The comment in the module's versions.tf must explain this
    module_versions_text = (ROOT / "infra/modules/proxmox-k3s-cluster/versions.tf").read_text()
    assert 'backend "http"' not in module_versions_text, (
        "infra/modules/proxmox-k3s-cluster/versions.tf must NOT"
        " declare a `backend \"http\"` block (modules never"
        " carry state)"
    )
    # The module versions.tf must mention why no backend block
    assert "ignored" in module_versions_text or "silently" in module_versions_text or "warning" in module_versions_text, (
        "infra/modules/proxmox-k3s-cluster/versions.tf must"
        " document why no backend block is declared"
    )


def test_skill_documents_path_drift_expectation() -> None:
    """Step 0e.7: SKILL.md must warn about the tokens_output_path
    drift between mount paths."""
    text = SKILL_PATH.read_text()
    assert "tokens_output_path" in text, (
        "Step 0e.7 must call out the tokens_output_path drift"
    )
    assert "/mnt/data/Projects" in text or "mount path" in text, (
        "Step 0e.7 must show that the drift is between two"
        " paths to the same physical directory"
    )


def test_skill_documents_refresh_only_post_migration() -> None:
    """Step 0e.8: SKILL.md must set the expectation that post-migration
    plan is `0 to add, N to change, 0 to destroy` (no resource
    destruction)."""
    text = SKILL_PATH.read_text()
    assert "0 to add" in text and "0 to destroy" in text, (
        "Step 0e.8 must show the expected post-migration plan shape"
        " (`0 to add, N to change, 0 to destroy`)"
    )


def test_gitlab_backend_helper_script_exists() -> None:
    """The gitlab_backend.sh helper must exist and be executable."""
    helper = ROOT / "scripts/gitlab_backend.sh"
    assert helper.is_file(), f"helper missing at {helper}"
    import stat
    mode = helper.stat().st_mode
    assert mode & stat.S_IXUSR, "scripts/gitlab_backend.sh must be executable"


def test_three_root_stacks_have_backend_block() -> None:
    """infra/tokens, infra/clusters/cicd, infra/clusters/apps must
    each declare a `backend \"http\" {}` block in their
    `terraform { ... }`."""
    for path in [
        "infra/tokens/versions.tf",
        "infra/clusters/cicd/main.tf",
        "infra/clusters/apps/main.tf",
    ]:
        text = (ROOT / path).read_text()
        assert 'backend "http"' in text, (
            f"{path} must declare a `backend \"http\"` block to"
            f" route state to the GitLab HTTP backend"
        )


def test_module_has_no_backend_block() -> None:
    """infra/modules/proxmox-k3s-cluster must NOT declare a
    `backend \"http\" {}` block (modules never carry state and the
    block would trigger a warning at every init)."""
    text = (ROOT / "infra/modules/proxmox-k3s-cluster/versions.tf").read_text()
    assert 'backend "http"' not in text, (
        "infra/modules/proxmox-k3s-cluster/versions.tf must NOT"
        " declare a `backend \"http\"` block"
    )


# ---------- WP00 preflight (Step 0a / 0b / 0c / 0d) ----------


def test_skill_documents_pve_node_discovery() -> None:
    """Step 0a.2: SKILL.md must instruct the agent to probe the
    Proxmox node name via `ssh hostname` BEFORE applying
    cluster tofu modules. Otherwise apply fails because the
    module's `node_name` defaults to `proxmox-host`."""
    text = SKILL_PATH.read_text()
    assert "Step 0a" in text, (
        "SKILL.md missing Step 0a (preflight discovery) section"
    )
    assert "pve_node" in text, (
        "SKILL.md must discuss the pve_node variable (cluster module takes"
        " the host's actual Proxmox node name; live hosts have arbitrary"
        " names like 'BigBertha')"
    )
    assert "hostname" in text, (
        "SKILL.md Step 0a.2 must instruct probing the live host's hostname"
    )


def test_skill_documents_subnet_collision_risk() -> None:
    """Step 0a.3: SKILL.md must warn about per-cluster ip_start
    colliding with the host's own management IP. The cluster
    module's `vip_in_dhcp_range` precondition only checks
    VIP-vs-node-IP; it does NOT check host-vs-node-IP."""
    text = SKILL_PATH.read_text()
    assert "ip_start" in text and "10.0.0.1" in text, (
        "SKILL.md must show concrete subnet-collision example (e.g. host"
        " on 10.0.0.1/8 vs cluster nodes in 10.0.0.0/24)"
    )
    assert "host" in text.lower() and (
        "interface" in text.lower() or "addr" in text.lower()
    ), (
        "SKILL.md must instruct probing the host's network interfaces"
        " (ip -4 -o addr show) BEFORE applying cluster tofu modules"
    )


def test_skill_documents_cloudflare_global_api_key_requirement() -> None:
    """Step 0a.6 / 0b.1: SKILL.md must explain that cfat_*
    scoped tokens cannot mint child tokens (POST /user/tokens
    needs user-level auth), and that the Cloudflare provider
    v5 requires ExactlyOneOf(api_key, api_token)."""
    text = SKILL_PATH.read_text().lower()
    assert "cfo_at" not in text, "no obfuscation"
    assert "cfat_" in text, (
        "SKILL.md must mention cfat_ scoped admin tokens explicitly"
    )
    assert "global_api_key" in text, (
        "SKILL.md must require CLOUDFLARE_GLOBAL_API_KEY for WP00"
    )
    # ExactlyOneOf is the schema-enforced constraint;
    # case-insensitive search accepts Either "ExactlyOneOf" or the
    # prose form ("exactly one of") that Step 0b.1 uses.
    assert (
        "exactlyoneof" in text or "exactly one of" in text
    ), (
        "SKILL.md must explain the Cloudflare provider's ExactlyOneOf"
        " (api_key vs api_token) constraint so future agents don't pass"
        " both and trigger a schema violation"
    )


def test_skill_documents_proxmox_sys_modify_requirement() -> None:
    """Step 0b.5: SKILL.md must warn that PVEAdmin does NOT
    include Sys.Modify; only Administrator role has it. WP00
    needs Sys.Modify to create roles."""
    text = SKILL_PATH.read_text()
    assert "Sys.Modify" in text, (
        "SKILL.md must mention Sys.Modify as a required privilege for WP00"
    )
    assert "PVEAdmin" in text, (
        "SKILL.md must warn that PVEAdmin does NOT include Sys.Modify"
    )
    assert "Administrator" in text, (
        "SKILL.md must mention the Administrator role as the path that"
        " has Sys.Modify"
    )


def test_skill_documents_opentofu_chdir_flag() -> None:
    """Step 0b.6: SKILL.md must warn that OpenTofu uses
    -chdir=DIR, NOT Terraform's -C. Makefile recipes that
    iterate modules can silently fail otherwise."""
    text = SKILL_PATH.read_text()
    assert "-chdir=" in text, (
        "SKILL.md must show the OpenTofu -chdir=DIR flag explicitly"
    )
    assert "Terraform `-C`" in text or "Terraform -C" in text, (
        "SKILL.md must explicitly say '-C is NOT supported' so future"
        " agents don't write Makefile recipes that use -C"
    )


def test_skill_documents_stale_tf_var_trap() -> None:
    """Step 0a.7: SKILL.md must warn that stale TF_VAR_*
    env vars from a prior terminal session silently override
    .env re-sourcing. Must instruct `unset $(env | grep ^TF_VAR_ | cut -d= -f1)`."""
    text = SKILL_PATH.read_text()
    assert "unset" in text and "TF_VAR_" in text, (
        "SKILL.md must show how to clear stale TF_VAR_* env vars before"
        " re-sourcing .env"
    )


def test_skill_documents_imported_token_no_secret_trap() -> None:
    """Step 0a.8: SKILL.md must warn that tofu-imported PVE
    tokens have a null secret because PVE doesn't return the
    secret for existing tokens. The fix is to delete and
    re-apply."""
    text = SKILL_PATH.read_text()
    assert "tofu import" in text and "value = null" in text or (
        "value" in text and "imported" in text.lower()
    ), (
        "SKILL.md must warn about the imported-token-no-secret trap"
    )
    assert "state rm" in text, (
        "SKILL.md must instruct using 'tofu state rm' to drop the imported"
        " token from state before re-applying"
    )


def test_skill_documents_cloudflare_resource_key_format() -> None:
    """Step 0b.2: SKILL.md must show the exact Cloudflare
    resource key format that the API accepts:
      - zone-scoped:    com.cloudflare.api.account.zone.<zone_id>
      - account-scoped: com.cloudflare.api.account.<account_id>
    NOT account.id (which is the API response field, not a resource key)."""
    text = SKILL_PATH.read_text()
    assert "com.cloudflare.api.account.zone.<zone_id>" in text or (
        "com.cloudflare.api.account.zone." in text
    ), "SKILL.md must show zone-scoped resource key format"
    assert "com.cloudflare.api.account.<account_id>" in text or (
        "com.cloudflare.api.account." in text
    ), "SKILL.md must show account-scoped resource key format"
    assert "account.id" in text, (
        "SKILL.md must call out 'account.id' as the wrong key (it's the"
        " API response field, not a resource key)"
    )


def test_skill_documents_permission_group_uuids() -> None:
    """Step 0b.3: SKILL.md must record the three Cloudflare
    permission-group UUIDs we hardcode as fallback (cfat_* tokens
    can't enumerate them via the provider). Stable since 2024."""
    text = SKILL_PATH.read_text()
    assert "c8fed203ed3043cba015a93ad1616f1f" in text, (
        "SKILL.md must record the Zone Read permission-group UUID"
    )
    assert "4755a26eedb94da69e1066d98aa820be" in text, (
        "SKILL.md must record the DNS Write permission-group UUID"
    )
    assert "c07321b023e944ff818fec44d8203567" in text, (
        "SKILL.md must record the Cloudflare Tunnel Write permission-group UUID"
    )


def test_skill_documents_wp00_phase() -> None:
    """Step 0d: SKILL.md must explicitly document WP00 / SS0
    (token provisioning) as a numbered phase before Phase 1."""
    text = SKILL_PATH.read_text()
    assert "WP00" in text, "SKILL.md must mention WP00"
    assert (
        "Phase 0" in text or "SS0" in text
    ), "SKILL.md must reference SS0/Phase 0 (token provisioning)"
    assert "output.json" in text, (
        "SKILL.md must reference infra/tokens/output.json (the SS0 contract)"
    )


# ---------- Phase 2 apply-time lessons (Step 2b, 2026-07-07) ----------


def test_skill_documents_cf_api_token_contract() -> None:
    """Step 2.2.1: `output.json` must expose `cf_api_token` and
    `cf_account_id` per spec T007. The cluster root's
    `data.local_sensitive_file` reads these keys."""
    text = SKILL_PATH.read_text()
    assert "cf_api_token" in text and "cf_account_id" in text, (
        "Step 2.2.1 must spell out the cf_api_token / cf_account_id"
        " contract keys that output.json must expose"
    )


def test_skill_documents_target_datastore_fix() -> None:
    """Step 2.2.2: bpg/proxmox v0.111.x proxmox_cloned_vm needs
    `clone.target_datastore` set to the same storage pool as the
    source VM. Otherwise plan/apply disagrees."""
    text = SKILL_PATH.read_text()
    assert "target_datastore" in text, (
        "Step 2.2.2 must name the target_datastore PVE clone config"
    )
    assert (
        "disk_storage_pool" in text and "var.disk_storage_pool" in text
    ), (
        "Step 2.2.2 must introduce the disk_storage_pool module variable"
    )
    assert "local-lvm" in text and "data1" in text, (
        "Step 2.2.2 must explain that BigBertha lacks local-lvm and"
        " the retarget is to data1"
    )


def test_skill_documents_pod_svc_cidr_output() -> None:
    """Step 2.2.5 (Ubuntu+k3s, 2026-07-07): `output.json` MUST expose
    `pod_cidr` and `svc_cidr` so tools/bootstrap_cluster.py can wire
    the per-VM k3s install flags (`--cluster-cidr` for the control
    plane, Cilium's clusterPoolIPv4PodCIDR for the CNI)."""
    text = SKILL_PATH.read_text()
    assert "pod_cidr" in text and "svc_cidr" in text, (
        "Step 2.2.5 must call out the missing pod_cidr / svc_cidr"
        " output.json keys and the fix"
    )
    assert (
        "tools/bootstrap_cluster.py" in text or "bootstrap_cluster.py" in text
    ), (
        "Step 2.2.5 must reference the consumer (bootstrap_cluster.py)"
        " that needs these keys"
    )


def test_skill_documents_manifests_subdir_path() -> None:
    """Step 2.2.6: the module must write the Traefik HelmChartConfig
    under `infra/clusters/<name>/manifests/`, not at the cluster
    root."""
    text = SKILL_PATH.read_text()
    assert (
        "manifests/traefik-helmchartconfig.yaml" in text
        or "manifests/traefik" in text
    ), (
        "Step 2.2.6 must mandate the manifests/ subdirectory"
        " for the Traefik HelmChartConfig"
    )
    assert (
        "tools/lib/helm_client.py" in text or "helm_client.py" in text
    ), (
        "Step 2.2.6 must reference tools/lib/helm_client.py which"
        " expects the file under manifests/"
    )


# ---------- Architecture cross-links (T007) ----------


def test_architecture_md_links_all_planning_artefacts() -> None:
    """WP07 T007: docs/architecture.md must link spec.md, plan.md,
    decomposition.md, research.md."""
    p = ROOT / "docs" / "architecture.md"
    assert p.is_file(), f"docs/architecture.md missing at {p}"
    text = p.read_text()
    for artefact in ["spec.md", "plan.md", "decomposition.md", "research.md"]:
        assert artefact in text, f"architecture.md missing link to {artefact}"


# ---------- Source-file pin: build_image pre-enrolled-keys fix ----------


def test_build_image_omits_pre_enrolled_keys_in_create_template() -> None:
    """Cross-check: the PveClient.create_template_shell must NOT
    include `pre-enrolled-keys=1` in the efidisk0 args (Talos v1.13.5
    UKI signature is not in the OVMF pre-enrolled DB). The skill
    documents this in Step 1.5.1; this test pins the source-side
    fix."""
    text = (TOOLS_LIB / "pve_client.py").read_text()
    # Find the create_template_shell method body
    m = re.search(
        r"def create_template_shell.*?(?=\n    def |\nclass )",
        text, re.DOTALL,
    )
    assert m is not None, (
        "PveClient.create_template_shell not found in pve_client.py"
    )
    body = m.group(0)
    # Strip line comments and docstrings so the rationale comment
    # doesn't trigger a false positive. We only want to assert that
    # the live `efidisk0` / `--efidisk0` arg list does NOT include
    # `pre-enrolled-keys=1`.
    code_lines = []
    for line in body.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        # Drop inline comments
        if "#" in line:
            in_str = False
            for i, ch in enumerate(line):
                if ch in ('"', "'"):
                    in_str = not in_str
                elif ch == "#" and not in_str:
                    line = line[:i]
                    break
        code_lines.append(line)
    code_only = "\n".join(code_lines)
    assert "pre-enrolled-keys" not in code_only, (
        "PveClient.create_template_shell must NOT pass pre-enrolled-keys=1"
        " (Talos v1.13.5 Secure Boot fix). See SKILL Step 1.5.1."
    )


def test_build_image_flips_boot_order_before_template() -> None:
    """Cross-check: build_image.py must set the template's boot order
    to scsi0 BEFORE calling qm template 900. Otherwise clones boot
    the ISO forever."""
    text = (ROOT / "tools" / "build_image" / "__init__.py").read_text()
    # Find the boot-order flip and the template_vm call
    assert "order=scsi0" in text, (
        "tools/build_image/__init__.py must set --boot order=scsi0"
        " before converting VM 900 to a template (SKILL Step 1.5.2)."
    )
    # The set_vm_config call must appear before template_vm
    scsi0_idx = text.find("order=scsi0")
    template_idx = text.find("self.pve.template_vm(TEMPLATE_VMID)")
    assert 0 < scsi0_idx < template_idx, (
        f"boot order=scsi0 (idx {scsi0_idx}) must be set BEFORE"
        f" template_vm (idx {template_idx}) in tools/build_image/__init__.py"
    )


def test_sync_dns_to_sdn_script_exists() -> None:
    """The DNS sync post-apply fixup must exist (SKILL Step 2.3)."""
    p = SCRIPTS / "sync_dns_to_sdn.py"
    assert p.is_file(), (
        f"scripts/sync_dns_to_sdn.py missing at {p}"
    )
    text = p.read_text()
    assert "network-get-interfaces" in text, (
        "sync_dns_to_sdn.py must call qm agent network-get-interfaces"
    )
    assert "intranet.local" in text, (
        "sync_dns_to_sdn.py must target the intranet.local. forward zone"
    )
    assert "10.in-addr.arpa" in text, (
        "sync_dns_to_sdn.py must target the 10.in-addr.arpa. reverse zone"
    )
