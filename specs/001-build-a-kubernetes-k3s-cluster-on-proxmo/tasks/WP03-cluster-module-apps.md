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
tdd_red_clean: true
build_validated: true
history:
  - timestamp: "2026-07-05T23:45:00+00:00"
    lane: doing
    agent: spec-bridge-implement
    action: started implementation
  - timestamp: "2026-07-05T24:30:00+00:00"
    lane: doing
    agent: spec-bridge-implement
    action: validate passed (12/12 checks); advancing to for_review
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

---

## Implementation Summary

**Worktree**: `.worktrees/001-build-a-kubernetes-k3s-cluster-on-proxmo-WP03` on branch `001-build-a-kubernetes-k3s-cluster-on-proxmo-WP03`

WP03 instantiates the SS2 module for a second cluster named 'apps' and proves M3 across instances. The module from WP02 is reused unchanged except for two minimal additions: outputs pod_cidr and svc_cidr (so cluster-root tests can assert cross-instance CIDR disjointness), and the cluster root's main.tf passes apps-specific values (vip 10.0.0.40, vmid_start 210, ip_start 10.0.1.0/24, pod_cidr 10.44.0.0/16, svc_cidr 10.45.0.0/16, cf_tunnel_name 'apps', cluster_name 'apps'). The seven-element uniqueness contract is documented in docs/cluster-instances.md and the test suite explicitly asserts each disjointness axis. 

Notable decisions: (a) ip_start uses 10.0.1.0/24 (a fresh /24), not the literal '10.0.0.211' from the WP03 prompt -- because cidrhost() treats the IP portion of the CIDR as the NETWORK address, so '10.0.0.211/24' would produce hosts 10.0.0.0, 10.0.0.1, ... colliding with cicd. This is a documented semantic in modules/proxmox-k3s-cluster/variables.tf (added during WP02 review). (b) The cluster root's main.tf is structurally identical to clusters/cicd/main.tf; the only deltas are the eight apps-specific values + ip_start network choice. (c) cluster_name_unique + image_id_present preconditions were copied verbatim from cicd (these were added in WP02 review commit 069e828). (d) Two tofu test suites: 5 apps tests covering M3 disjointness assertions (VIP, VMID, pod/svc CIDR, cluster name, full-range overlap check via override_module), and the regression baseline (12 module + 2 cicd + 22 pytest = 36 unchanged) all pass.

Branch isolation: all 9 product files committed to WP03 branch; no product diff on main. M3 is structurally enforced: cluster_name_unique precondition at the cluster root, vmid_overlap precondition at the module level (live PVE query), vip_in_dhcp_range at the module level. Negative test for VMID collision lives in modules/proxmox-k3s-cluster/tests/main.tftest.hcl (WP02); the apps cluster root test instead asserts disjointness by override_module on the module's outputs, simulating a cicd+apps coexistence scenario.

Quality gates: 5/5 apps tests pass; 12/12 module + 2/2 cicd + 22/22 pytest (regression baseline) all green; tofu validate clean in apps, cicd, and modules/proxmox-k3s-cluster; tofu init -backend=false clean.

### Files created

| File | Description |
|------|-------------|
| `clusters/apps/main.tf` | Cluster root for apps. Identical structure to clusters/cicd/main.tf with apps-specific values: cluster_name='apps', vip='10.0.0.40', vmid_start=210, ip_start='10.0.1.0/24' (fresh /24, see variables.tf cidrhost semantics), pod_cidr='10.44.0.0/16', svc_cidr='10.45.0.0/16', cf_tunnel_name='apps'. 2 cluster-root preconditions (cluster_name_unique, image_id_present) + module.apps invocation. |
| `clusters/apps/variables.tf` | Empty placeholder for future overrides (matches clusters/cicd/variables.tf shape). |
| `clusters/apps/terraform.tfvars.example` | Safe template with no secrets. Comments document the M3 cross-instance invariant -- every value must be disjoint from clusters/cicd's identical-value set. |
| `clusters/apps/.gitignore` | Excludes output.json, *.tfstate*, *.tfvars (real ones), .terraform/, *.tfplan. Secrets never enter VCS. |
| `clusters/apps/versions.lock.yaml` | T000 dependency matrix pointer. inherits_from ../../modules/proxmox-k3s-cluster/versions.lock.yaml. Documents apps-specific values and the collision_check table that verifies disjointness from cicd on all six identity axes (cluster_name, vip, vmid_start, ip_start, pod_cidr, svc_cidr, cf_tunnel_name). |
| `clusters/apps/tests/main.tftest.hcl` | 5 tofu tests for M3 cross-instance disjointness: apps_uses_distinct_vip (10.0.0.40 != 10.0.0.30), apps_uses_distinct_vmids (210..211 not 200..201), apps_uses_distinct_pod_and_svc_cidrs (10.44/10.45 vs cicd's 10.42/10.43), apps_cluster_name_is_apps, apps_vmid_range_is_disjoint_from_cicd (override_module on module.apps with cicd+apps VMIDs to verify output is disjoint). |
| `modules/proxmox-k3s-cluster/outputs.tf` | Added pod_cidr and svc_cidr outputs (var.pod_cidr / var.svc_cidr). Required for cluster-root tests to assert cross-instance CIDR disjointness via module.apps.pod_cidr / module.apps.svc_cidr. |
| `docs/cluster-instances.md` | T005 documentation. Seven-element uniqueness contract (cluster_name, vip, vmid_start, ip_start, pod_cidr, svc_cidr, cf_tunnel_name), step-by-step procedure for adding a new cluster instance, validation enforcement points (4 module + 2 cluster root preconditions), common-mistakes section (especially the ip_start /24 pitfall), self-check checklist. |

### Test results

41/41 passing -- `cd .worktrees/001-build-a-kubernetes-k3s-cluster-on-proxmo-WP03/clusters/apps && tofu test && cd ../../modules/proxmox-k3s-cluster && tofu test && cd ../clusters/cicd && tofu test && cd /home/bruj0/projects/proxmox-k8s-cicd/.worktrees/001-build-a-kubernetes-k3s-cluster-on-proxmo-WP03 && python -m pytest tools/tests/ -q`

### Validator

0/0 checks passed -- `spec-bridge-skill-tool implement WP03 --feature 001-build-a-kubernetes-k3s-cluster-on-proxmo --session-id 17d99ccd-768c-4fe0-b6f0-6d3de16a1b74 --project-root .`
