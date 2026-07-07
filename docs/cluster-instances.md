# Cluster Instances — How to Add a New Cluster

This document describes how to add a third (fourth, fifth, ...) cluster instance
to the SS2 Cluster Provisioning Module. Every cluster instance must satisfy the
**seven-element uniqueness contract** below; violating any one of them violates
M3 ("two clusters race for the same control-plane endpoint").

## Background

The `infra/modules/proxmox-k3s-cluster` module is designed to be instantiated once per
cluster. Each instantiation creates:

- N Proxmox VMs (control-plane + workers) cloned from the Ubuntu+k3s image template (VMID 900)
- A kube-vip VIP reserved in the vnet0 dnsmasq ethers file
- Per-VM output.json with the cluster's identity, written as `0600`

Two cluster instances must coexist on the same Proxmox host without colliding.
This document enforces the contract that makes coexistence safe.

## The Seven-Element Uniqueness Contract

Every cluster instance must pick **disjoint values** for these seven elements:

| # | Element | Disjointness rule | Reason |
|---|---|---|---|
| 1 | `cluster_name` | Unique across all instances | Identifies the cluster in k3s certs, dnsmasq, output.json. The cluster root has a `terraform_data.cluster_name_unique` precondition that fires if any sibling's `output.json` already uses this name. |
| 2 | `vip` | Unique across all instances | kube-vip binds the active control-plane to this IP. Two clusters with the same VIP would race for the kube-apiserver endpoint. |
| 3 | `vmid_start` | Range `[vmid_start .. vmid_start + total - 1]` must not overlap any other cluster's range; recommended 4-gate buffer between ranges | Proxmox VMIDs are unique across the host. The module's `terraform_data.vmid_overlap` precondition checks against the live PVE VM list. |
| 4 | `ip_start` | Distinct `/24` from any other cluster's `ip_start` | cidrhost treats the IP portion of the CIDR as the network address, so `10.0.0.0/24` yields `10.0.0.0`, `10.0.0.1`, ... Two clusters with the same /24 will produce identical per-node IPs at the same host positions, causing L3 collisions. **Pick a fresh /24 — not just a fresh host number.** |
| 5 | `pod_cidr` | Unique across all instances | Cilium IPAM allocates pods from this range per cluster. Two clusters with the same pod_cidr would have pod IP collisions. |
| 6 | `svc_cidr` | Unique across all instances | kube-proxy/Cilium service virtual IPs come from this range. |
| 7 | `cf_tunnel_name` | Unique across all instances (when cf_tunnel is enabled) | The Cloudflare Tunnel resource is created per-cluster; sharing a name would merge tunnel state across clusters. |

## Procedure

### 1. Pick the seven values

Pick a fresh `cluster_name` (e.g., `prod`, `staging`, `data`). Then pick the
other six values that are disjoint from every existing instance. Use
`infra/clusters/cicd/versions.lock.yaml` and `infra/clusters/apps/versions.lock.yaml` as
references for what's already taken.

### 2. Author the cluster root

Create `infra/clusters/<cluster_name>/` with:

- `main.tf` — calls `module "../../modules/proxmox-k3s-cluster"` with all 13
  inputs, reading `build/image-id.txt` and `infra/tokens/output.json` via data
  sources (same shape as `infra/clusters/cicd/main.tf`).
- `variables.tf` — empty placeholder for future overrides.
- `terraform.tfvars.example` — safe template, no secrets.
- `.gitignore` — excludes `output.json`, `*.tfstate*`, `talos/`, etc.
- `tests/main.tftest.hcl` — at least:
  - `assert module.<name>.cluster_name == "<cluster_name>"`
  - asserts that the seven values are disjoint from every sibling instance.
- `versions.lock.yaml` — pointer to `../../modules/proxmox-k3s-cluster/versions.lock.yaml`
  plus the cluster-specific values and the collision_check table.

### 3. Verify

```bash
cd infra/clusters/<cluster_name>
tofu init -backend=false
tofu validate              # exits 0
tofu test                  # all assertions pass (incl. M3 disjointness)
```

### 4. Apply

```bash
# Operator-side: with live PVE credentials in env
cd infra/clusters/<cluster_name>
tofu apply -auto-approve
```

This provisions the N VMs, reserves the VIP in dnsmasq, and writes the cluster's
`output.json`. The bootstrap agent (SS3 / `tools/bootstrap_cluster.py`)
consumes `output.json` to drive the `cloudinit, k3s, helm, kubeconfig,
host_ports, externalname` sub-phases.

## Common Mistakes

- **Same /24 for ip_start.** If you pick `ip_start = "10.0.0.0/24"` for both
  cicd and apps, both clusters' per-node IPs will be `10.0.0.0` and `10.0.0.1`
  — L3 collision. Pick a fresh /24 (e.g., `10.0.1.0/24`).
- **vmid_start too close.** The 4-gate buffer (e.g., cicd uses 200..201, apps
  uses 210..211) gives PVE time to garbage-collect VMID allocation state
  between plans. Don't crowd ranges together.
- **Reusing cf_tunnel_name.** Even when `cf_publish_traefik_publicly=false`
  (default), the `cf_tunnel_name` is reserved. Picking a fresh name now makes
  the cloudflare fallback trivially switchable later.

## Validation at Plan Time

The cluster root and the module cooperate to enforce uniqueness:

- **Cluster root**: `terraform_data.cluster_name_unique` reads sibling
  `output.json` files and asserts no other cluster has the same name.
- **Module**: `terraform_data.vmid_overlap` reads the live PVE VM list and
  asserts the VMID range does not overlap any populated VM.
- **Module**: `terraform_data.vip_in_dhcp_range` asserts the VIP does not
  collide with the per-node IP range (M5 — DHCP safety).
- **Module**: `terraform_data.validate_control_plane_count` asserts count is 1
  or 3 (FR-030).
- **Module**: `terraform_data.validate_image_id` asserts image_id is non-empty.

These preconditions fail plan at the first collision; no live state is
touched.

## Self-Check Checklist

Before applying a new cluster, verify against every existing instance:

- [ ] `cluster_name` differs from every sibling.
- [ ] `vip` differs from every sibling's VIP.
- [ ] `vmid_start..vmid_start+total-1` is disjoint from every sibling's range
      (with at least a 4-VMID buffer).
- [ ] `ip_start` /24 differs from every sibling's /24.
- [ ] `pod_cidr` differs from every sibling.
- [ ] `svc_cidr` differs from every sibling.
- [ ] `cf_tunnel_name` differs from every sibling's tunnel name.

If any item is missing, the cluster will collide at the layer named.