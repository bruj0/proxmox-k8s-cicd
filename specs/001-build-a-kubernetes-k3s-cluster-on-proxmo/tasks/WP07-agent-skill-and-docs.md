---
work_package_id: "WP07"
title: "Agent Skill + Runbooks + Final Verification"
lane: "doing"
dependencies:
  - WP06
subsystem: "SS3 (Agent Skill)"
misfits_addressed:
  - M7 (codified via skill)
  - SC-001 through SC-006 (verified)
abstract_components:
  - .agents/skills/proxmox-k3s-pipeline/SKILL.md
  - docs/runbooks/cloudflare-fallback.md
  - docs/runbooks/scale-workers.md
  - docs/runbooks/decommission-cluster.md
  - docs/runbooks/rotate-tokens.md
  - docs/architecture.md
tdd_red_clean: true
agent: "spec-bridge-implement"
history:
  - timestamp: "2026-07-05T14:52:55+00:00"
    lane: doing
    agent: spec-bridge-implement
    action: started implementation
---

# WP07 — Agent Skill + Runbooks + Final Verification

## Goal

Author `.agents/skills/proxmox-k3s-pipeline/SKILL.md` (the Agent Skill that drives the whole pipeline), three operator runbooks, and `docs/architecture.md`. Run final SC-001 through SC-006 verifications.

## Execution constraints

- Product code and tests: only in `$WORKTREES_DIR/001-build-a-kubernetes-k3s-cluster-on-proxmo-WP07/`
- Do not merge to `$TARGET_BRANCH` until `spec-bridge-merge` after accept

## Subtasks

### T000 — Version compatibility matrix (gate before any other subtask)

Before scaffolding anything, build a per-WP version matrix:

1. **Identify every external dependency this WP will touch.** For WP07: `agentskills.io` open standard (SKILL.md frontmatter schema), the Agent Skill consumer (Claude Code, Cursor — both must be supported), `context7-auto-research` skill (the prerequisite gate from FR-025).
2. **For each dependency, run `context7-auto-research`** (load `.agents/skills/context7-auto-research/SKILL.md` first) to confirm:
   - The **latest stable release** of the `agentskills.io` spec.
   - That the SKILL.md schema this WP writes is the version both Claude Code and Cursor currently consume (spec drift across consumers is a real risk; document the consumer version range tested).
   - The latest version of the `context7-auto-research` skill that this WP's SKILL.md will reference.
3. **Cross-check compatibility**:
   - The SKILL.md frontmatter schema matches both consumers' parsers.
   - The 5-phase pipeline instructions are consistent with the actual CLI tools (tofu, build_image.py, bootstrap_cluster.py) and Helm release names that WP00-WP06 produced.
4. **Document the result** in `.agents/skills/proxmox-k3s-pipeline/versions.lock.yaml`:
   ```yaml
   dependencies:
     - name: agentskills.io
       version: "v1 (current stable)"
       consumers_tested: ["claude-code", "cursor"]
     - name: context7-auto-research
       version: "latest"
   cross_check:
     skill_schema_both_consumers: "compatible"
     pipeline_instructions_match_actual_cli: "verified"
   ```
5. **The agent must NOT proceed** to T001+ until this file exists and is reviewed.

This subtask is the canonical "T000" step for every WP in this feature. Repeat it in every WP, scoped to that WP's dependencies.

### T001 — `.agents/skills/proxmox-k3s-pipeline/SKILL.md` (per `agentskills.io`)

```yaml
---
name: proxmox-k3s-pipeline
description: Bring up two k3s clusters (cicd + apps) on a single Proxmox host using OpenTofu, Packer, and an Agent-driven bootstrap. Use when the user says "bring up both clusters", "deploy the pipeline", "run spec 001", or similar. Outputs a fully bootstrapped cluster pair with public HTTPS via Cloudflare Tunnel (no host open ports) and apps->cicd cross-cluster Service consumption via ExternalName.
---

# Proxmox k3s Pipeline

## When to load this skill
Load when the operator asks to bring up, scale, or troubleshoot the k3s clusters provisioned by spec 001.

## Step 0 — Load prerequisites
Before invoking any external library, load `.agents/skills/context7-auto-research/SKILL.md` and run context7 for:
- `bpg/proxmox` v0.111.1 (OpenTofu provider)
- `hashicorp/proxmox` v1.2.3 (Packer plugin)
- `STRRL/cloudflare-tunnel-ingress-controller` v0.0.23 Helm chart
- `cilium` 1.16.x Helm chart
- `sergelogvinov/proxmox-cloud-controller-manager` v0.14.0
- `sergelogvinov/proxmox-csi-plugin` v0.19.1 (chart 0.5.9)
- `talosctl` (Talos 1.10.x)
- `k3s` v1.34.x
- `helm` 3.x

Document findings in the operator's reply before invoking any of them.

## Step 1 — Phase 1: Build the VM image
...
```

### T002 — Step 1 of the skill: instruct the agent to load context7-auto-research

The skill's Step 1 (just above) is the explicit gate. The body includes:

> Before invoking `bpg/proxmox`, `hashicorp/proxmox`, `STRRL/cloudflare-tunnel-ingress-controller`, `helm`, `talosctl`, or `kubernetes`, load `.agents/skills/context7-auto-research/SKILL.md` and run `context7-auto-research` for each library. Do NOT rely on training data for library APIs.

### T003 — Steps 2-5 of the skill (Phase 1-5)

For each phase, the skill instructs the agent on:
- The exact CLI to run (e.g. `make build-image` for Phase 1)
- The operator prompts for missing configuration (with redaction for secrets)
- The success criteria (e.g. `qm list | grep 900` for Phase 1)
- The failure handling (halt + structured error + wait for operator decision)

### T004 — `docs/runbooks/cloudflare-fallback.md`

Four steps:

1. Flip the variable:
   ```bash
   cd clusters/cicd
   tofu apply -var="cf_publish_traefik_publicly=true"
   ```
2. Re-render Traefik HelmChartConfig (Tofu does this automatically as part of the variable flip).
3. Add DNAT rules on BigBertha:
   ```bash
   ssh root@10.0.0.1 -p 6022 nft add rule ip nat prerouting tcp dport 443 dnat to 10.0.0.30:443
   ssh root@10.0.0.1 -p 6022 nft add rule ip nat prerouting tcp dport 80  dnat to 10.0.0.30:80
   ```
4. Update Cloudflare DNS: change the `*.example.com` CNAMEs (managed by the controller) to A records pointing at `151.80.34.63`.

Rollback: reverse all four steps.

### T005 — `docs/runbooks/scale-workers.md`

Two directions:

Scale up:
1. Edit `clusters/cicd/terraform.tfvars` to set `workers.count = 3` (or higher)
2. Run `cd clusters/cicd && tofu apply -auto-approve`
3. Wait ~5 minutes per new worker; `kubectl --context cicd get nodes` shows them Ready

Scale down:
1. Edit `workers.count` back down
2. Run `tofu apply -auto-approve`
3. The module cordons + drains + `qm destroy`s the surplus VMs in order of highest VMID first
4. PDB-aware eviction: `kubectl --context cicd get pdb -A` confirms any PDBs are respected
5. Minimum 60-second grace period before destroy

### T006 — `docs/runbooks/decommission-cluster.md`

```bash
cd clusters/cicd   # or apps
tofu destroy -auto-approve
```

The module:
- Removes the cluster's VMs from PVE
- Removes the cluster's VIP reservation from dnsmasq ethers
- Removes the cluster's context from `~/.kube/config` (via the bootstrap script's inverse)

For a full PVE cleanup: `pvesh delete /access/...` for any orphaned resources.

### T007 — `docs/architecture.md`

Author a top-level architecture document that links:
- spec.md (the requirements)
- plan.md (the implementation plan)
- decomposition.md (the misfit analysis)
- research.md (the technical research)
- the cluster modules and their outputs

Includes a high-level topology diagram (same as research.md §1) and a cross-cluster wiring section.

### T008 — Final SC verifications

Document and (where PVE is accessible) run:

- SC-001: clean-room end-to-end in ≤60 minutes
- SC-002: PVC + Deployment succeeds on both clusters
- SC-003: Ingress of class `cloudflare-tunnel` resolves via Cloudflare
- SC-004: `nft list chain ip nat prerouting` shows zero new DNAT rules
- SC-005: re-run idempotency (tofu + bootstrap)
- SC-006: `tofu destroy` cleanup

### T009 — NFR verifications

- NFR-013: resource budget ≤ 16 vCPU + 24 GiB for default shape
- NFR-014: each new worker Ready in <5 min

### T010 — NFR-010/011/012 verifications

- NFR-010: SKILL.md has YAML frontmatter with `name` and `description` (test)
- NFR-011: skill idempotency (running from clean state vs. partial state converge to the same end state)
- NFR-012: skill mentions every external library with version pin and rationale (test)

## Acceptance Criteria

- [ ] `.agents/skills/proxmox-k3s-pipeline/SKILL.md` exists, has YAML frontmatter with `name: proxmox-k3s-pipeline` and a non-empty `description`
- [ ] SKILL.md mentions every external library (bpg/proxmox, hashicorp/proxmox, STRRL controller, helm, talosctl, kubernetes) with version pin and rationale
- [ ] SKILL.md Step 1 instructs the agent to load context7-auto-research before invoking any external library
- [ ] All three runbooks exist and document the procedures in copy-pasteable form
- [ ] `docs/architecture.md` exists with links to all four planning artefacts
- [ ] An agent that loads the skill can drive the whole pipeline end-to-end
- [ ] SC-001 through SC-006 all verified (or their verification procedure documented if PVE is unavailable)
- [ ] NFR-013, NFR-014, NFR-010, NFR-011, NFR-012 all verified

## Technical context

- **Agent Skill format**: `agentskills.io` open standard (YAML frontmatter, markdown body)
- **Operators**: claude-code, cursor (skill must be loadable by both)
- **Runbooks**: plain Markdown in `docs/runbooks/`

## How to run

```bash
# Operator-facing:
cat .agents/skills/proxmox-k3s-pipeline/SKILL.md
# Or just type "bring up both clusters" to any agent that has the skill loaded.
```