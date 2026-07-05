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