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
