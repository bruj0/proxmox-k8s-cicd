---
work_package_id: "WP03"
title: "Cluster Module + Second Instance (apps)"
lane: doing
dependencies:
  - WP02
subsystem: "SS2 (Cluster Provisioning Module)"
misfits_addressed:
  - M3 (proven across instances)
abstract_components:
  - clusters/apps/main.tf
  - clusters/apps/variables.tf
  - clusters/apps/terraform.tfvars.example
  - clusters/apps/output.json (gitignored)
  - docs/cluster-instances.md
agent: spec-bridge-implement
history:
  - timestamp: "2026-07-05T23:45:00+00:00"
    lane: doing
    agent: spec-bridge-implement
    action: started implementation
---

# WP03 — Cluster Module + Second Instance (apps)

## Goal

`clusters/apps/{main,variables}.tf` calling the same `modules/proxmox-k3s-cluster` module with apps-specific values. Proves the module is reusable and that two instances coexist without overlap (M3 satisfied across instances).

## Execution constraints

- Product code and tests: only in `$WORKTREES_DIR/001-build-a-kubernetes-k3s-cluster-on-proxmo-WP03/`
- Do not merge to `$TARGET_BRANCH` until `spec-bridge-merge` after accept

## Subtasks

### T000 — Version compatibility matrix (gate before any other subtask)

Before scaffolding anything, build a per-WP version matrix:

1. **Identify every external dependency this WP will touch.** For WP03: same module dependencies as WP02 (`bpg/proxmox`, `hashicorp/helm`, `hashicorp/local`), but verify that the apps-specific values (different `pod_cidr`, `svc_cidr`, VIP, VMID range) are compatible with the version of Cilium and Cilium's IPAM that this module will render for.
2. **For each dependency, run `context7-auto-research`** (load `.agents/skills/context7-auto-research/SKILL.md` first) to confirm:
   - The **latest stable release** version of each provider (WP02's `versions.lock.yaml` is the input; re-verify nothing newer is available).
   - That the Cilium chart version WP02 targeted is compatible with the apps pod CIDR (`10.44.0.0/16`) and service CIDR (`10.45.0.0/16`) — not just the cicd ones.
3. **Cross-check compatibility** with WP02's matrix: this WP reuses the same module version; the apps-specific CIDRs must not collide with any other cluster's CIDRs (cicd uses 10.42/10.43).
4. **Document the result** in `clusters/apps/versions.lock.yaml` (a thin pointer to WP02's lock file plus the apps-specific CIDR check):
   ```yaml
   inherits_from: "../../modules/proxmox-k3s-cluster/versions.lock.yaml"
   cluster_specific:
     pod_cidr: "10.44.0.0/16"
     svc_cidr: "10.45.0.0/16"
     vip: "10.0.0.40"
     collision_check: "no overlap with cicd (10.42/10.43) or any future cluster"
   ```
5. **The agent must NOT proceed** to T001+ until this file exists and is reviewed.

This subtask is the canonical "T000" step for every WP in this feature. Repeat it in every WP, scoped to that WP's dependencies.

### T001 — `clusters/apps/{main,variables}.tf`

Identical to `clusters/cicd/main.tf` but with these values:

| Variable | cicd | apps |
|---|---|---|
| `cluster_name` | `"cicd"` | `"apps"` |
| `vip` | `"10.0.0.30"` | `"10.0.0.40"` |
| `vmid_start` | `200` | `210` |
| `ip_start` | `"10.0.0.201"` | `"10.0.0.211"` |
| `pod_cidr` | `"10.42.0.0/16"` | `"10.44.0.0/16"` |
| `svc_cidr` | `"10.43.0.0/16"` | `"10.45.0.0/16"` |
| `cf_tunnel_name` | `"cicd"` | `"apps"` |

### T002 — `clusters/apps/terraform.tfvars.example`

Document all variables; pull `image_id`, `cf_api_token`, `proxmox_*` from `infra/tokens/output.json` via `local_file` data source (same as cicd).

### T003 — Guard test for VMID/IP overlap

Author a `tofu test` that asserts:
- The plan for apps does NOT contain any warnings about VMIDs 200-204 (cicd's range) or IPs 10.0.0.201-205 (cicd's range).
- Artificially setting `vmid_start=200` for apps fails plan with a clear error.

### T004 — `tofu validate` + `tofu plan`

```bash
cd clusters/apps
tofu init
tofu validate
tofu plan
```

Document the apply procedure.

### T005 — `docs/cluster-instances.md`

Document how to add a third cluster:
1. Choose a unique `cluster_name`
2. Pick a fresh VIP (not in any existing cluster's range)
3. Pick a fresh VMID range (not overlapping any existing cluster)
4. Pick a fresh IP range
5. Pick fresh pod_cidr and svc_cidr (not overlapping any existing)
6. Choose a fresh `cf_tunnel_name`
7. Author a new `clusters/<name>/` root module calling the shared module

Document the invariant: every cluster instance must be globally unique across these seven values.

## Acceptance Criteria

- [ ] `cd clusters/apps && tofu init && tofu validate` exits 0
- [ ] `tofu plan` exits 0 (against PVE) showing 2 VMs (VMIDs 210, 211) to be created
- [ ] `tofu apply -auto-approve` exits 0; `qm list | grep -E '210|211'` shows 2 VMs
- [ ] `ssh root@10.0.0.1 -p 6022 'pvesh get /cluster/sdn/vnets'` shows ethers reservation for 10.0.0.40 (in addition to cicd's 10.0.0.30)
- [ ] `cat clusters/apps/output.json | jq '.nodes | length'` returns 2
- [ ] `cat clusters/apps/output.json | jq '.nodes[].vmid'` returns 210, 211 (distinct from cicd's 200, 201)
- [ ] Re-running `tofu apply` is a no-op in <30 s
- [ ] `tofu test` passes the overlap guard

## Technical context

Same as WP02. No new dependencies.

## How to run

```bash
cd clusters/apps
tofu init
tofu apply -auto-approve
```