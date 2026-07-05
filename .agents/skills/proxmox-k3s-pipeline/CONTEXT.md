---
context_name: "Proxmox k3s Pipeline (Agent Skill)"
version: "1"
subsystem: ".agents/skills/proxmox-k3s-pipeline/"
created: "2026-07-05T14:53:00Z"
updated: "2026-07-05T14:53:00Z"
---

# Proxmox k3s Pipeline (Agent Skill)

The Agent Skill that drives the full pipeline (build image -> provision cluster
-> bootstrap orchestration) for spec 001. Loaded by Claude Code, Cursor, and
any other Agent that consumes the agentskills.io open standard.

## Language

**Agent Skill**:
A markdown document under `.agents/skills/<name>/SKILL.md` with YAML
frontmatter (`name`, `description`) that an Agent consumes to drive a
specific operational workflow. The skill is the canonical interface
between an operator and the project's deliverables (scripts, modules,
binaries). One per bounded operational workflow.
_Avoid_: `prompt`, `instruction set`, `playbook` (those are open-ended
or domain-specific terms; this project uses the agentskills.io term
explicitly).
_Subsystems_: SS3 (Agent Skill)
_Files_: `.agents/skills/proxmox-k3s-pipeline/SKILL.md`
_Relates to_: Operator (driven by), Pipeline (orchestrates), SC-001
through SC-006 (verifies)
_History_:
- 2026-07-05 (001-build-a-kubernetes-k3s-cluster-on-proxmo): initial definition.

**Operator**:
The human or AI agent that invokes the skill by typing "bring up both
clusters" or similar natural-language intent. The skill is the bridge
between operator intent and the deterministic deliverable surface
(`make build-image`, `tofu apply`, `python tools/bootstrap_cluster.py`).
_Avoid_: `user`, `caller` (too generic; operator implies the skill
context).
_Subsystems_: SS3
_Files_: `.agents/skills/proxmox-k3s-pipeline/SKILL.md`
_Relates to_: Agent Skill (drives), Pipeline (initiates)
_History_:
- 2026-07-05 (001-build-a-kubernetes-k3s-cluster-on-proxmo): initial definition.

**Pipeline**:
The end-to-end sequence of phases that brings up the two clusters:
Phase 1 (image build) -> Phase 2 (cluster provisioning) -> Phase 3
(talos bootstrap) -> Phase 4 (Helm releases) -> Phase 5 (final
verification). Each phase has a deterministic CLI entry point and a
set of success criteria.
_Avoid_: `workflow`, `runbook` (runbook is a single-phase operator
procedure; pipeline is the end-to-end multi-phase sequence).
_Subsystems_: SS1, SS2, SS3
_Files_: `.agents/skills/proxmox-k3s-pipeline/SKILL.md`,
`Makefile`, `scripts/apply.sh`
_Relates to_: Agent Skill (orchestrated by), Phase (decomposes into),
Operator (initiated by)
_History_:
- 2026-07-05 (001-build-a-kubernetes-k3s-cluster-on-proxmo): initial definition.

**Runbook**:
A copy-pasteable markdown procedure under `docs/runbooks/<topic>.md`
that an operator follows for a single-phase task (cloudflare-fallback,
scale-workers, decommission-cluster, rotate-tokens). Distinguished from
a pipeline by being a single concern, not a multi-phase sequence.
_Avoid_: `tutorial`, `how-to` (too informal; runbook is the operational
term this project uses).
_Subsystems_: SS3
_Files_: `docs/runbooks/cloudflare-fallback.md`,
`docs/runbooks/scale-workers.md`,
`docs/runbooks/decommission-cluster.md`,
`docs/runbooks/rotate-tokens.md`
_Relates to_: Operator (consumed by), Pipeline (subset-of)
_History_:
- 2026-07-05 (001-build-a-kubernetes-k3s-cluster-on-proxmo): initial definition.

**Phase**:
One numbered stage of the pipeline. Each phase has a single CLI entry
point and explicit success criteria that the agent must assert before
proceeding to the next phase.
_Avoid_: `step` (steps live inside a phase; phases live inside the
pipeline).
_Subsystems_: SS1, SS2, SS3
_Files_: `.agents/skills/proxmox-k3s-pipeline/SKILL.md`
_Relates to_: Pipeline (part-of), Runbook (mirrors as a single-phase
procedure)
_History_:
- 2026-07-05 (001-build-a-kubernetes-k3s-cluster-on-proxmo): initial definition.

## Relationships

- A **Pipeline** decomposes into five **Phases** (Phase 1 through Phase 5).
- A **Runbook** is a single-**Phase** procedure consumed directly by the
  **Operator** (no Agent required).
- An **Agent Skill** orchestrates a **Pipeline** on behalf of an **Operator**.

## Flagged Ambiguities

- "skill" was used loosely to mean both an agentskills.io Skill and the
  capability to perform a task — resolved: in this project, "skill" always
  means the agentskills.io SKILL.md artifact; human/agent capability is
  referred to as "the operator" or "the agent".