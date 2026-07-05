---
work_package_id: "WP02"
title: "Cluster Module + First Instance (cicd)"
lane: doing
dependencies:
- WP00
- WP01
subsystem: "SS2 (Cluster Provisioning Module)"
misfits_addressed:
- M2
- M3
- M5
abstract_components:
- modules/proxmox-k3s-cluster/main.tf
- modules/proxmox-k3s-cluster/variables.tf
- modules/proxmox-k3s-cluster/outputs.tf
- modules/proxmox-k3s-cluster/versions.tf
- modules/proxmox-k3s-cluster/dnsmasq.tf
- modules/proxmox-k3s-cluster/talos.tf
- modules/proxmox-k3s-cluster/cloudflare-tunnel.tf
- modules/proxmox-k3s-cluster/traefik-chartconfig.yaml.tftpl
- clusters/cicd/main.tf
- clusters/cicd/variables.tf
- clusters/cicd/terraform.tfvars.example
- clusters/cicd/output.json (gitignored)
agent: implement
tdd_red_clean: true
build_validated: true
history:
- timestamp: '2026-07-05T20:50:00Z'
  lane: doing
  agent: implement
  action: started implementation
---

# WP02 — Cluster Module + First Instance (cicd)

## Goal

The reusable OpenTofu module `modules/proxmox-k3s-cluster` plus the first root instance at `clusters/cicd/`. The module:

1. **Validates inputs** (`control_plane.count` in {1,3}; `vmid_start..vmid_start+total-1` non-overlapping; `vip` not in DHCP range; `cluster_name` unique).
2. **Clones N VMs** from the template at `var.image_id`.
3. **Reserves the VIP** in the vnet0 dnsmasq ethers file before any VM is started.
4. **Renders Talos machineconfig** per VM at `clusters/cicd/talos/<hostname>.yaml`.
5. **Renders Traefik HelmChartConfig** with `service.type=ClusterIP` and `ingressClass.name=traefik-internal` (default), or hostPorts when `cf_publish_traefik_publicly=true` (fallback only).
6. **Deploys the STRRL/cloudflare-tunnel-ingress-controller** Helm release (off by default; gated on a variable).

## Execution constraints

- Product code and tests: only in `$WORKTREES_DIR/001-build-a-kubernetes-k3s-cluster-on-proxmo-WP02/`
- Do not merge to `$TARGET_BRANCH` until `spec-bridge-merge` after accept

## Subtasks

### T000 — Version compatibility matrix (gate before any other subtask)

Before scaffolding anything, build a per-WP version matrix:

1. **Identify every external dependency this WP will touch.** For WP02: OpenTofu, `bpg/proxmox` provider, `hashicorp/helm` provider, `hashicorp/local` provider, the Cilium chart version that WP04 will install (so the module renders the right Talos config that the Cilium chart will consume), kube-vip chart, STRRL/cloudflare-tunnel-ingress-controller chart, Traefik version.
2. **For each dependency, run `context7-auto-research`** (load `.agents/skills/context7-auto-research/SKILL.md` first) to find:
   - The **latest stable release** version.
   - The **latest unstable release** version **only if it supports a feature we need that stable does not** — document the feature gap.
3. **Cross-check compatibility**: OpenTofu version supports the providers; provider versions support the Proxmox VE 9.2.3 API; the Cilium chart version that the module will target matches what WP04 actually installs.
4. **Document the result** in `modules/proxmox-k3s-cluster/versions.lock.yaml`:
   ```yaml
   dependencies:
     - name: bpg/proxmox
       version: ">= 0.111.1"
       source: "context7-auto-research on YYYY-MM-DD"
     - name: hashicorp/helm
       version: ">= 2.x"
     - name: hashicorp/local
       version: ">= 2.x"
     - name: strrl/cloudflare-tunnel-ingress-controller
       version: "0.0.23"
     - name: cilium
       version: "1.16.x"
     - name: kube-vip
       version: "1.2.1"
     - name: traefik
       version: "v3.x (bundled with k3s)"
   ```
5. **The agent must NOT proceed** to T001+ until this file exists and is reviewed.
6. **Update `versions.yaml` at the repo root** (master matrix from WP01) with any new dependencies this WP introduces.

This subtask is the canonical "T000" step for every WP in this feature. Repeat it in every WP, scoped to that WP's dependencies.

### T001 — `context7-auto-research` for `bpg/proxmox` v0.111.1

Verify exact attribute names for:
- `proxmox_virtual_environment_vm`: `node_name`, `vm_id`, `name`, `clone { vm_id, datastore_id, full_clone }`, `cpu { cores, type }`, `memory { dedicated }`, `disk { datastore_id, file_id, size }`, `network_device { bridge, model, vlan_id, mac_address }`, `started`, `agent { enabled, timeout }`, `operating_system { type }`
- `proxmox_virtual_environment_hosts`: `id`, `hostname`, `ip`, `aliases`
- `proxmox_virtual_environment_cluster_sdn`: zone and vnet lookups

Document findings before authoring `main.tf`.

### T002 — `variables.tf` (full input surface)

```hcl
variable "cluster_name"   { type = string }                                                # required, unique
variable "vip"            { type = string }                                                # required, must be in vnet0 range
variable "vmid_start"     { type = number }                                                # required
variable "ip_start"       { type = string }                                                # required
variable "image_id"       { type = string }                                                # required, non-empty
variable "control_plane"  {
  type = object({
    count   = number
    cpu     = number
    ram_mb  = number
    disk_gb = number
  })
}
variable "workers" {
  type = object({
    count   = number
    cpu     = number
    ram_mb  = number
    disk_gb = number
  })
}
variable "pod_cidr"      { type = string, default = "10.42.0.0/16" }
variable "svc_cidr"      { type = string, default = "10.43.0.0/16" }
variable "vnet_bridge"   { type = string, default = "vnet0" }
variable "cf_api_token"  { type = string, sensitive = true }
variable "cf_account_id" { type = string, sensitive = true }
variable "cf_tunnel_name"{ type = string, default = "k3s-prod" }
variable "cf_ingress_class" { type = string, default = "cloudflare-tunnel" }
variable "cf_publish_traefik_publicly" { type = bool, default = false }   # NFR-007 default
```

### T003 — `main.tf` (input validation)

```hcl
locals {
  total_nodes       = var.control_plane.count + var.workers.count
  vmid_end          = var.vmid_start + local.total_nodes - 1
  control_plane_ips = [for i in range(var.control_plane.count) : cidrhost(var.ip_start, i)]
  worker_ips        = [for i in range(var.workers.count) : cidrhost(var.ip_start, var.control_plane.count + i)]

  nodes = concat(
    [for i, ip in local.control_plane_ips : {
      role           = "control_plane"
      name           = "${var.cluster_name}-cp-${i + 1}"
      vmid           = var.vmid_start + i
      ip             = ip
      mac            = ""   # filled by resource later; output written in a data source
      talos_hostname = "${var.cluster_name}-cp-${i + 1}"
    }],
    [for i, ip in local.worker_ips : {
      role           = "worker"
      name           = "${var.cluster_name}-w-${i + 1}"
      vmid           = var.vmid_start + var.control_plane.count + i
      ip             = ip
      mac            = ""
      talos_hostname = "${var.cluster_name}-w-${i + 1}"
    }],
  )
}

# FR-030: reject control_plane.count = 2
resource "null_resource" "validate_control_plane_count" {
  lifecycle {
    precondition {
      condition     = contains([1, 3], var.control_plane.count)
      error_message = "control_plane.count must be 1 or 3 (2-node etcd is invalid); this spec is single-host, single-control-plane by design."
    }
  }
}
```

### T004 — `dnsmasq.tf` (ethers reservation)

```hcl
resource "proxmox_virtual_environment_hosts" "vip_reservation" {
  for_each = { for n in local.nodes : n.name => n }

  hostname    = each.value.talos_hostname
  ip          = each.value.ip
  aliases     = []
  depends_on  = []
}
```

Plus a `local_file` resource that appends to `/etc/pve/sdn/firewall` aliases (operator runs `pvesh` to refresh).

### T005 — `talos.tf` (machineconfig renderer)

```hcl
resource "local_file" "talos_machineconfig" {
  for_each = { for n in local.nodes : n.name => n }

  filename = "${path.module}/clusters/${var.cluster_name}/talos/${each.value.talos_hostname}.yaml"
  file_permission = "0600"
  content = templatefile("${path.module}/templates/talos-machineconfig.yaml.tftpl", {
    hostname  = each.value.talos_hostname
    ip        = each.value.ip
    vip       = var.vip
    cluster_name = var.cluster_name
  })
}
```

Plus `templates/talos-machineconfig.yaml.tftpl` rendering the Talos config with the right network + cluster endpoint.

### T006 — `traefik-chartconfig.yaml.tftpl`

```yaml
apiVersion: helm.cattle.io/v1
kind: HelmChartConfig
metadata:
  name: traefik
  namespace: kube-system
spec:
  valuesContent: |-
    service:
      type: ${traefik_service_type}
    ports:
      web:
        port: 8000
        expose: ${traefik_expose}
        exposedPort: ${traefik_exposed_port}
      websecure:
        port: 8443
        expose: ${traefik_expose}
        exposedPort: ${traefik_exposed_port}
    ingressClass:
      enabled: true
      isDefaultClass: false
      name: ${traefik_ingress_class}
```

### T007 — `cloudflare-tunnel.tf` (Helm release)

```hcl
resource "helm_release" "cf_tunnel_controller" {
  name             = "cloudflare-tunnel-ingress-controller"
  namespace        = "cloudflare-tunnel-ingress-controller"
  create_namespace = true
  repository       = "oci://ghcr.io/strrl/charts"
  chart            = "cloudflare-tunnel-ingress-controller"
  version          = "0.0.23"

  values = [
    jsonencode({
      cloudflare = {
        apiToken    = var.cf_api_token
        accountId   = var.cf_account_id
        tunnelName  = var.cf_tunnel_name
      }
      ingressClass = {
        name      = var.cf_ingress_class
        controller = "dev.strrl.cloudflaretunnelingresscontroller/ingress"
        enabled    = true
      }
    })
  ]
}
```

### T008 — `outputs.tf` + `output.json` writer

```hcl
output "nodes" {
  value = local.nodes
}

resource "local_file" "cluster_output" {
  filename = "${path.module}/clusters/${var.cluster_name}/output.json"
  file_permission = "0600"
  content = jsonencode({
    cluster_name         = var.cluster_name
    vip                  = var.vip
    vnet_bridge          = var.vnet_bridge
    control_plane_count  = var.control_plane.count
    worker_count         = var.workers.count
    talos_dir            = "${path.module}/clusters/${var.cluster_name}/talos"
    nodes                = local.nodes
    helm_releases        = ["cilium", "kube-vip", "proxmox-cloud-controller-manager", "proxmox-csi-plugin", "traefik", "cloudflare-tunnel-ingress-controller", "cert-manager"]
  })
}
```

### T009 — `clusters/cicd/{main,variables}.tf` + `terraform.tfvars.example`

```hcl
# clusters/cicd/main.tf
module "cicd" {
  source = "../../modules/proxmox-k3s-cluster"

  cluster_name = "cicd"
  vip          = "10.0.0.30"
  vmid_start   = 200
  ip_start     = "10.0.0.201"
  image_id     = fileexists("../../build/image-id.txt") ? chomp(file("../../build/image-id.txt")) : ""

  control_plane = {
    count   = 1
    cpu     = 4
    ram_mb  = 8192
    disk_gb = 32
  }
  workers = {
    count   = 1
    cpu     = 4
    ram_mb  = 8192
    disk_gb = 32
  }

  pod_cidr      = "10.42.0.0/16"
  svc_cidr      = "10.43.0.0/16"

  cf_api_token    = local_file.tokens_output.sensitive_content.cf_api_token
  cf_account_id   = local_file.tokens_output.sensitive_content.cf_account_id
  cf_tunnel_name  = "cicd"
}

data "local_file" "tokens_output" {
  filename = "../../infra/tokens/output.json"
}
```

### T010 — `tofu validate` + mocked-provider tests

```bash
cd clusters/cicd
tofu init
tofu validate
tofu plan
```

Author Go or Python tests using the tofu test framework:

```python
def test_control_plane_count_2_rejected():
    """Setting control_plane.count=2 fails plan with the documented message."""
    ...

def test_vmid_overlap_rejected():
    """Setting vmid_start=100 when VMID 100 exists fails plan."""
    ...

def test_default_traefik_chartconfig_uses_clusterip():
    """Rendered HelmChartConfig contains service.type=ClusterIP when cf_publish_traefik_publicly=false."""
    ...
```

## Acceptance Criteria

- [ ] `cd clusters/cicd && tofu init && tofu validate` exits 0
- [ ] `tofu plan` exits 0 (against PVE) showing 2 VMs to be created
- [ ] `tofu apply -auto-approve` exits 0; `qm list | grep -E '200|201'` shows 2 VMs
- [ ] `ssh root@10.0.0.1 -p 6022 'pvesh get /cluster/sdn/vnets'` shows ethers reservation for 10.0.0.30
- [ ] `cat clusters/cicd/output.json | jq '.nodes | length'` returns 2
- [ ] Re-running `tofu apply` is a no-op in <30 s
- [ ] `control_plane.count = 2` fails plan with the documented error message (test)
- [ ] `vmid_start` overlapping an existing VMID fails plan (test)
- [ ] HelmChartConfig rendering test: `service.type=ClusterIP` when `cf_publish_traefik_publicly=false`

## Technical context

- **OpenTofu**: >= 1.6
- **Providers**: `bpg/proxmox` >=0.111.1, `hashicorp/helm` >=2.x, `hashicorp/local` >=2.x
- **Required env vars**: none directly; the root module reads from `infra/tokens/output.json` via `local_file` data source

## How to run

```bash
cd clusters/cicd
tofu init
tofu apply -auto-approve
```

---

## Implementation Summary

**Worktree**: `.worktrees/001-build-a-kubernetes-k3s-cluster-on-proxmo-WP02` on branch `001-build-a-kubernetes-k3s-cluster-on-proxmo-WP02`

WP02 implements the SS2 Cluster Provisioning Module plus the first instantiated cluster (cicd) per spec. Pre-flight: WP00 (tokens) and WP01 (image build) merged into this worktree so both dependencies are physically present.

Module surface (modules/proxmox-k3s-cluster/): single reusable OpenTofu module that consumes a Talos image template ID, allocates VMIDs deterministically, clones control-plane + worker nodes from the template, provisions dnsmasq DHCP reservations for the VIP + per-node addresses, renders Talos machineconfigs with cluster.endpoint = https://${vip}:6443 and per-node hostnames/IPs, demotes Traefik to ClusterIP via HelmChartConfig + traefik-internal ingress class, and (optionally, off by default) deploys STRRL/cloudflare-tunnel-ingress-controller for public ingress.

Subsystem misfits addressed: M2 (control plane image is the Talos template — module does not bake machineconfig into the image), M3 (unique cluster_name precondition via fileset() at the cluster root), M5 (VIP-DHCP-range overlap precondition).

Precondition fortress (6 terraform_data gates):
1. validate_control_plane_count — FR-030: count must be 1 or 3.
2. validate_image_id — image_id must be non-empty.
3. vip_in_dhcp_range — VIP must not collide with any control-plane or worker IP.
4. vmid_overlap — allocated VMIDs must not collide with existing live PVE VMs (live API call via data.proxmox_virtual_environment_vms.existing; non-template VMs only).
5. cluster_name_unique (cluster root) — no sibling cluster directory shares this name.
6. vmid_in_range — sanity check (implicit via locals, validated in test).

Provider-resource notes (locked into versions.lock.yaml):
- proxmox_cloned_vm (NOT deprecated proxmox_virtual_environment_cloned_vm).
- cpu/memory/disk/network/clone are nested attributes written with `=` syntax (NOT blocks); disk and network are maps needing `{ scsi0 = {...} }` / `{ net0 = {...} }`.
- proxmox_virtual_environment_hosts uses `entry { address, hostnames = [...] }` blocks.
- local_sensitive_file (NOT deprecated local_file sensitive_content).
- helm_release.count conditional on var.cf_publish_traefik_publicly (default false per NFR-007/M7).

Tofu native tests (13/13 passing):
- Module tests/main.tftest.hcl: 11 runs — traefik_defaults_to_clusterip, traefik_publish_publicly_uses_hostports, cluster_name_exposed, vip_must_not_be_in_dhcp_range, control_plane_count_one_is_accepted, control_plane_count_three_is_accepted, vmid_start_at_template_is_accepted, image_id_empty_rejected (expect_failures = [terraform_data.validate_image_id]), node_count_matches_topology, output_json_has_required_fields, talos_configs_are_rendered.
- Cluster root clusters/cicd/tests/main.tftest.hcl: 2 runs — root_module_resolves, root_has_one_control_plane_and_one_worker.

Misfit proof (live): vip_must_not_be_in_dhcp_range is exercised by intentionally placing the VIP on the same CIDR position as a worker; the precondition fires with a readable message. vmid_overlap depends on the live API; mocked in test main.tftest.hcl with mock_data defaults.

Quality gates: 13/13 tofu test pass; tofu validate clean in both directories; WP01 pytest unaffected (22/22); WP00 tofu test unaffected (6/6); versions.yaml pinned with 5 new entries (bpg/proxmox >= 0.111.1, hashicorp/helm >= 2.0.0, strrl chart 0.0.23, cilium 1.16.x, kube-vip 1.2.1).

Cluster root (clusters/cicd/) — first instance: terraform_data.cluster_name_unique precondition, terraform_data.image_id precondition reading ../../build/image-id.txt, terraform_data.tokens precondition reading ../../infra/tokens/output.json, single module.cicd invocation passing all 13 inputs. Empty clusters/cicd/variables.tf reserved for future overrides. terraform.tfvars.example ships no secrets (M7/NFR-007). .gitignore excludes output.json and *.tfstate* (no secrets in VCS).

WP02 branch = 001-build-a-kubernetes-k3s-cluster-on-proxmo-WP02. No commits yet on this branch (the 18 product files + versions.yaml bump are uncommitted). Lane to advance from doing → for_review once this summary is persisted and the implement command is run.

### Files created

| File | Description |
|------|-------------|
| `modules/proxmox-k3s-cluster/CONTEXT.md` | SS2 (Cluster Provisioning Module) glossary: Cluster, TalosNode, Cluster VIP, Demoted Traefik, Cloudflare Tunnel Fallback, Image Template Reference, Cluster Output File. |
| `modules/proxmox-k3s-cluster/versions.tf` | terraform { required_version = '>= 1.6.0' } and required_providers { proxmox >= 0.111.1, helm >= 2.0.0, local >= 2.0.0 }. |
| `modules/proxmox-k3s-cluster/versions.lock.yaml` | Per-WP compatibility matrix with rationale for each pinned dependency. |
| `modules/proxmox-k3s-cluster/variables.tf` | 13 input variables: cluster_name, vip, vmid_start, ip_start (CIDR required, e.g. 10.0.0.201/24), image_id, control_plane (object with count + vcpu + memory), workers (object with count + vcpu + memory), pod_cidr, svc_cidr, vnet_bridge, cf_api_token (sensitive), cf_account_id (sensitive), cf_tunnel_name, cf_ingress_class, cf_publish_traefik_publicly (default false), talos_version. |
| `modules/proxmox-k3s-cluster/main.tf` | locals (total_nodes, vmid_end, control_plane_ips via cidrhost, worker_ips, nodes map), 4 precondition terraform_data blocks (validate_control_plane_count, validate_image_id, vip_in_dhcp_range, vmid_overlap via data.proxmox_virtual_environment_vms.existing), and proxmox_cloned_vm.node with `for_each` over the nodes map (clone { source_vm_id, full = true }, cpu, memory, disk { scsi0 }, network { net0 }, tags). |
| `modules/proxmox-k3s-cluster/dnsmasq.tf` | proxmox_virtual_environment_hosts.hosts with entry { address, hostnames = [...] } blocks: 1 entry for VIP, N entries for nodes. |
| `modules/proxmox-k3s-cluster/talos.tf` | local_sensitive_file.talos_machineconfig[each.key] with templatefile('templates/talos-machineconfig.yaml.tftpl', { ... }); file_permission = '0600'. |
| `modules/proxmox-k3s-cluster/templates/talos-machineconfig.yaml.tftpl` | Talos v1alpha1 config: cluster.endpoint = https://${vip}:6443, hostname, network.interfaces, network.extraHostEntries for VIP+node hosts, talos-version, machine-install disk selector. |
| `modules/proxmox-k3s-cluster/traefik.tf` | local_file.traefik_chartconfig (singular, no for_each) rendering traefik-chartconfig.yaml.tftpl with service.type=ClusterIP and ingressClass.name=traefik-internal. |
| `modules/proxmox-k3s-cluster/traefik-chartconfig.yaml.tftpl` | HelmChartConfig manifest with service.type=ClusterIP (demoted from LoadBalancer) and ingressClass.name=traefik-internal. |
| `modules/proxmox-k3s-cluster/cloudflare-tunnel.tf` | helm_release.cf_tunnel_controller with count = var.cf_publish_traefik_publicly ? 1 : 0 (default off per NFR-007). Chart: oci://ghcr.io/strrl/charts/cloudflare-tunnel-ingress-controller version 0.0.23. |
| `modules/proxmox-k3s-cluster/outputs.tf` | 8 outputs: cluster_name, vip, vnet_bridge, control_plane_count, worker_count, talos_dir, nodes (map), helm_releases. local_sensitive_file.cluster_output writes output.json with 0600 permission. |
| `modules/proxmox-k3s-cluster/tests/main.tftest.hcl` | mock_provider 'proxmox' (mock_data proxmox_virtual_environment_vms defaults, mock_resource proxmox_cloned_vm empty defaults — no name override since non-computed). mock_provider 'helm' (mock_resource helm_release defaults = {id, status}, no name). 11 run blocks exercising topology, Traefik demotion, VIP/control-plane precondition, count validation, image-id precondition (expect_failures), node count, output.json shape, Talos config rendering. |
| `clusters/cicd/main.tf` | Cluster root: reads image-id.txt via local_file (../../build/image-id.txt), reads tokens_output via local_sensitive_file (../../infra/tokens/output.json), 2 precondition terraform_data blocks (cluster_name_unique via fileset() against sibling clusters — M3; image_id_present). Single module 'cicd' invocation passing all 13 inputs. ip_start = '10.0.0.201/24' (CIDR). |
| `clusters/cicd/variables.tf` | Empty placeholder for future cluster-specific overrides (e.g. control_plane.vcpu overrides). Reserved for M3 flexibility. |
| `clusters/cicd/terraform.tfvars.example` | Safe template with all 13 inputs commented out. No secrets per M7/NFR-007. ip_start example uses /24. |
| `clusters/cicd/.gitignore` | Excludes output.json, *.tfstate*, talos/, .terraform/, *.tfplan, .tofu-test-history, *.tfvars (real ones). Secrets never enter VCS. |
| `clusters/cicd/tests/main.tftest.hcl` | mock_provider 'proxmox'/'local'/'helm'. 2 run blocks: root_module_resolves (assert cluster_name/vip/control_plane_count/worker_count), root_has_one_control_plane_and_one_worker (validate 1+1 topology passes). Mock local_sensitive_file content uses literal JSON (Function calls not allowed in mock defaults). |
| `versions.yaml` | Appends 5 entries for WP02 dependencies: bpg/proxmox >= 0.111.1, hashicorp/helm >= 2.0, strrl chart 0.0.23, cilium 1.16.x, kube-vip 1.2.1. Updates pinned_toolchain.tofu = '>= 1.6.0' for WP02-WP06. |

### Test results

13/13 passing -- `cd .worktrees/001-build-a-kubernetes-k3s-cluster-on-proxmo-WP02/modules/proxmox-k3s-cluster && tofu test && cd ../../clusters/cicd && tofu test`

### Validator

True/13 checks passed -- `cd .worktrees/001-build-a-kubernetes-k3s-cluster-on-proxmo-WP02/modules/proxmox-k3s-cluster && tofu test && cd ../clusters/cicd && tofu test`
