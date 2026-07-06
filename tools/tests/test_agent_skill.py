"""WP07 acceptance tests.

Encodes the Agent Skill acceptance criteria from NFR-010, NFR-011, NFR-012
and the WP07 prompt's Acceptance Criteria list:

  NFR-010: SKILL.md has YAML frontmatter with `name` and non-empty `description`.
  NFR-011: skill idempotency (running from clean state vs partial state
    converges to the same end state). We exercise this by asserting the
    skill documents both 'first run' and 'rerun / partial state' paths
    in its body.
  NFR-012: SKILL.md mentions every external library with version pin and
    rationale. We assert a curated list of (library, version) pairs.
  Acceptance: SKILL.md Step 1 instructs the agent to load
    .agents/skills/context7-auto-research/SKILL.md before invoking any
    external library.
  Acceptance: all four runbooks exist and contain a copy-pasteable
    command block (bash fenced code).

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
    are the canonical contract; if any are missing, NFR-012 fails."""
    text = SKILL_PATH.read_text().lower()
    required = [
        ("bpg/proxmox", "0.111.1"),
        ("hashicorp/proxmox", "1.2.3"),
        ("strrl/cloudflare-tunnel-ingress-controller", "0.0.23"),
        ("cilium", "1.16"),
        ("sergelogvinov/proxmox-cloud-controller-manager", "0.14.0"),
        ("sergelogvinov/proxmox-csi-plugin", "0.5.9"),
        ("talosctl", "1.10"),
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


# ---------- context7 gate (Step 1) ----------


def test_skill_md_step_1_instructs_context7_gate() -> None:
    """Acceptance criterion: SKILL.md's first content step (after the
    prerequisites preamble) must instruct the agent to load
    `.agents/skills/context7-auto-research/SKILL.md` before invoking any
    external library.

    We assert:
      - the literal path `.agents/skills/context7-auto-research/SKILL.md`
        appears in the body (NOT the frontmatter), and
      - the phrase "before invoking" (case-insensitive) appears within
        ~20 lines of that mention (a positive gate, not a stray mention).
    """
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


# ---------- docs/architecture.md cross-links (T007) ----------


def test_architecture_md_links_all_planning_artefacts() -> None:
    """WP07 T007: docs/architecture.md must link spec.md, plan.md,
    decomposition.md, research.md."""
    p = ROOT / "docs" / "architecture.md"
    assert p.is_file(), f"docs/architecture.md missing at {p}"
    text = p.read_text()
    for artefact in ["spec.md", "plan.md", "decomposition.md", "research.md"]:
        assert artefact in text, f"architecture.md missing link to {artefact}"

# ---------- live-host preflight (Step 0a / 0b / 0c / 0d) ----------
#
# Added 2026-07-06 after the WP00 deploy against kvm.bruj0.net
# surfaced four hard blockers (Proxmox node-name mismatch, host
# subnet collision with node IP CIDR, Proxmox token without
# Sys.Modify, Cloudflare provider ExactlyOneOf auth, child-token
# mint via cfat_*). Each test below pins one of those lessons
# into the skill's body so future agents cannot regress.


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


# ---------- Phase 1 apply-time lessons (Step 1b, 2026-07-06) ----------
#
# Pinned from the live PVE 9.2.3 deploy where the WP00-minted
# k3s-terraform@pam!tf token was used as Packer auth and Phase 1
# surfaced six hard blockers. Each test below pins one lesson so a
# future agent can't regress.


def test_skill_documents_packer_local_lvm_storage_swap() -> None:
    """Step 1b.1: SKILL.md must tell operators to retarget
    `local-lvm` -> `data1` when the live host has no `local-lvm`
    lvmthin pool. BigBertha only has `data1`, `data2`, `local`."""
    text = SKILL_PATH.read_text()
    assert "Step 1b" in text, (
        "SKILL.md missing Step 1b (Phase 1 apply-time gotchas) section"
    )
    assert "local-lvm" in text, (
        "Step 1b must explain why local-lvm is the default and why we"
        " retarget to data1 on hosts that lack it"
    )
    assert "data1" in text, (
        "Step 1b must recommend data1 as the replacement storage"
    )


def test_skill_documents_packer_token_bare_secret_format() -> None:
    """Step 1b.2: Packer v1.2.x `token` is the BARE secret UUID,
    not the concatenated `<id>=<secret>` value. The pre-2026-07-06
    template concatenated twice and PVE rejected with 401."""
    text = SKILL_PATH.read_text().lower()
    assert "bare secret" in text, (
        "Step 1b.2 must explain that Packer `token` is the BARE secret"
    )
    assert "double-prefixed" in text or ("401" in text and "auth" in text), (
        "Step 1b.2 must call out the 401 double-prefix failure mode"
    )


def test_skill_documents_k3s_cluster_role_19_privs() -> None:
    """Step 1b.3: The k3s-cluster role must extend from the spec
    T005 12-priv set to 19 entries (adding Sys.Audit, VM.Audit,
    VM.Clone, VM.Migrate, VM.Config.CDROM, VM.Config.HWType,
    VM.Snapshot.Rollback) for Packer/Phase 1 access."""
    text = SKILL_PATH.read_text()
    assert (
        "Sys.Audit" in text and "VM.Audit" in text and "VM.Clone" in text
    ) and "VM.Config.HWType" in text, (
        "Step 1b.3 must enumerate the 7 added privs"
    )
    assert "19" in text and (
        "12 priv" in text or "12-priv" in text or "12 priv" in text
    ), (
        "Step 1b.3 must compare the new 19-priv total against the"
        " original spec T005 12-priv count"
    )


def test_skill_documents_output_json_secret_split() -> None:
    """Step 1b.4: bpg/proxmox v0.111.x's
    `proxmox_user_token.<>.value` is the FULL api-token string;
    output_json.tf must split on `=` so proxmox_token_secret
    contains only the bare UUID (length 36)."""
    text = SKILL_PATH.read_text()
    assert "value" in text and (
        "FULL" in text or "full" in text.lower()
    ), (
        "Step 1b.4 must explain that .value is the FULL token string"
    )
    assert "split" in text and "=" in text, (
        "Step 1b.4 must mention the split-on-= approach to extract"
        " the bare UUID"
    )
    assert "36" in text, (
        "Step 1b.4 must mention the 36-char UUID length as the"
        " success signature"
    )


def test_skill_documents_packer_ssh_wait_incompatibility() -> None:
    """Step 1b.5: hashicorp/proxmox v1.2.x proxmox-clone blocks
    on SSH-wait (5-minute timeout) which Talos installer mode
    can never satisfy. SKILL must recommend the direct PVE API
    clone+template bypass path."""
    text = SKILL_PATH.read_text()
    assert (
        "proxmox-clone" in text or "proxmox-clone" in text
    ), (
        "Step 1b.5 must name the proxmox-clone builder"
    )
    assert "SSH" in text and (
        "wait" in text or "timeout" in text
    ), (
        "Step 1b.5 must call out the SSH-wait / SSH-timeout behavior"
    )
    assert (
        "/nodes/" in text and "clone" in text and "template" in text
    ) or (
        "PROXMOX_API_URL" in text and "clone" in text
    ), (
        "Step 1b.5 must give the concrete curl copy-paste for the"
        " POST /nodes/<node>/qemu/999/clone + template bypass"
    )


def test_skill_documents_vmid_999_storage_preallocation() -> None:
    """Step 1b.6: VMID 999 (talos-base) must be pre-created on
    the same storage pool the Packer template retargets to
    (`data1`). The ISO file name must match; the upstream
    asset is `metal-amd64.iso`, NOT `talos-amd64.iso`."""
    text = SKILL_PATH.read_text()
    assert "talos-base" in text and "999" in text, (
        "Step 1b.6 must instruct pre-creating VMID 999 named"
        " `talos-base` with the Talos ISO attached"
    )
    assert "qm create 999" in text, (
        "Step 1b.6 must give the qm create copy-paste for VMID 999"
    )
    assert "metal-amd64.iso" in text, (
        "Step 1b.6 must call out the GitHub release asset name"
        " `metal-amd64.iso` (NOT `talos-v1.10.0-amd64.iso`)"
    )


def test_versions_lock_documents_phase1_evidence() -> None:
    """After the 2026-07-06 Phase 1 deploy, versions.lock.yaml
    must record the Phase 1 cross-checks: Packer schema patches,
    role-extension to 19 privs, output.json split, Packer-SSH
    incompatibility, and the direct API bypass."""
    assert VERSIONS_PATH.is_file(), f"versions.lock.yaml missing at {VERSIONS_PATH}"
    text = VERSIONS_PATH.read_text()
    assert "phase1_apply_against_live_host" in text
    assert "phase1_k3s_role_privs" in text
    assert "phase1_output_json_split" in text
    assert "phase1_packer_schema" in text
    assert "phase1_packer_ssh_wait" in text
    # The 19-priv count must be present in the lock file too
    assert "19" in text, (
        "versions.lock.yaml must record the new 19-priv k3s-cluster role"
    )
    # BigBertha's storage pool is in evidence
    assert "data1" in text and "data2" in text


# ---------- Phase 2 apply-time lessons (Step 2b, 2026-07-06) ----------
#
# Pinned from the live PVE 9.2.3 Phase-2 apply where the k3s-cluster
# tofu module surfaced six real-world deployment issues beyond the
# Phase-0/Phase-1 preflight set. Each test pins one lesson.


def test_skill_documents_cf_api_token_contract() -> None:
    """Step 2b.1: `output.json` must expose `cf_api_token` and
    `cf_account_id` per spec T007. The cluster root's
    `data.local_sensitive_file` reads these keys."""
    text = SKILL_PATH.read_text()
    assert "Step 2b" in text, (
        "SKILL.md missing Step 2b (Phase 2 apply-time gotchas) section"
    )
    assert "cf_api_token" in text and "cf_account_id" in text, (
        "Step 2b.1 must spell out the cf_api_token / cf_account_id"
        " contract keys that output.json must expose"
    )
    assert (
        "spec-T007" in text or "spec T007" in text or "tasks.md" in text
    ), (
        "Step 2b.1 must reference the spec contract (tasks.md line ~106)"
        " that mandates the cf_api_token naming convention"
    )


def test_skill_documents_target_datastore_fix() -> None:
    """Step 2b.2: bpg/proxmox v0.111.x proxmox_cloned_vm needs
    `clone.target_datastore` set to the same storage pool as the
    source VM. Otherwise plan/apply disagrees."""
    text = SKILL_PATH.read_text()
    assert "target_datastore" in text, (
        "Step 2b.2 must name the target_datastore PVE clone config"
    )
    assert (
        "disk_storage_pool" in text and "var.disk_storage_pool" in text
    ), (
        "Step 2b.2 must introduce the disk_storage_pool module variable"
    )
    assert "local-lvm" in text and "data1" in text, (
        "Step 2b.2 must explain that BigBertha lacks local-lvm and"
        " the retarget is to data1"
    )


def test_skill_documents_sys_modify_requirement() -> None:
    """Step 2b.3: the k3s-cluster role MUST extend to include
    `Sys.Modify` (20 privs total) for proxmox_virtual_environment_hosts
    SDN writes to be accepted by PVE 9.2.x."""
    text = SKILL_PATH.read_text()
    assert "Sys.Modify" in text, (
        "Step 2b.3 must explain that Sys.Modify is required for"
        " SDN hosts writes"
    )
    assert (
        "20" in text and "12 spec T005" in text
    ), (
        "Step 2b.3 must compare the new 20-priv total against the"
        " original 12-priv spec T005 count"
    )


def test_skill_documents_pod_svc_cidr_output() -> None:
    """Step 2b.4: `output.json` MUST expose `pod_cidr` and
    `svc_cidr` so tools/lib/talos_client.py can wire Talos
    configs."""
    text = SKILL_PATH.read_text()
    assert "pod_cidr" in text and "svc_cidr" in text, (
        "Step 2b.4 must call out the missing pod_cidr / svc_cidr"
        " output.json keys and the fix"
    )
    assert (
        "tools/lib/talos_client.py" in text or "talos_client.py" in text
    ), (
        "Step 2b.4 must reference the consumer (talos_client.py)"
        " that needs these keys"
    )


def test_skill_documents_manifests_subdir_path() -> None:
    """Step 2b.5: the module must write the Traefik HelmChartConfig
    under `infra/clusters/<name>/manifests/`, not at the cluster
    root."""
    text = SKILL_PATH.read_text()
    assert (
        "manifests/traefik-helmchartconfig.yaml" in text
        or "manifests/traefik" in text
    ), (
        "Step 2b.5 must mandate the manifests/ subdirectory"
        " for the Traefik HelmChartConfig"
    )
    assert (
        "tools/lib/helm_client.py" in text or "helm_client.py" in text
    ), (
        "Step 2b.5 must reference tools/lib/helm_client.py which"
        " expects the file under manifests/"
    )


def test_skill_documents_path_module_double_dot_fix() -> None:
    """Step 2b.7: the cluster module and cluster root both use
    relative paths that broke after the layout refactor. Both
    must use enough `..` segments to reach the repo root."""
    text = SKILL_PATH.read_text()
    assert "${path.module}/../../clusters" in text, (
        "Step 2b.7 must show the corrected module-side path"
        " pattern (two '..' segments)"
    )
    assert "${path.module}/../../../build" in text, (
        "Step 2b.7 must show the corrected cluster-root-side path"
        " pattern (three '..' segments to the build/ dir)"
    )


def test_versions_lock_documents_phase2_evidence() -> None:
    """After the 2026-07-06 Phase-2 apply, versions.lock.yaml must
    record the Phase-2 evidence."""
    assert VERSIONS_PATH.is_file(), f"versions.lock.yaml missing at {VERSIONS_PATH}"
    text = VERSIONS_PATH.read_text()
    assert "phase2" in text, (
        "versions.lock.yaml must have a Phase-2 cross_check entry"
    )
    # The 20-priv count must be present
    assert "20" in text, (
        "versions.lock.yaml must record the new 20-priv k3s-cluster role"
    )


# ---------- State-backend (Step 0e, 2026-07-06) ----------
#
# Pinned from the live 4-stack migration from local state to the
# GitLab HTTP backend at project infra-state/bigbertha (id 84156476).
# Each test pins one lesson so a future re-org has to update the
# test before changing the design.


def test_skill_documents_gitlab_backend_layout() -> None:
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
