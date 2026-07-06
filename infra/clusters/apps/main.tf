###############################################################################
# Cluster instance: clusters/apps.
#
# Second root that consumes modules/proxmox-k3s-cluster. Proves M3 by existing
# alongside clusters/cicd with non-overlapping identity (VIP, VMIDs, IPs, CIDRs).
#
# Identity invariants (M3):
#   - VIP 10.0.0.40 (cicd: 10.0.0.30)
#   - VMID range 210..211 (cicd: 200..201)
#   - per-node IP range 10.0.1.0..10.0.1.1 within 10.0.1.0/24
#     (cicd: 10.0.0.0/24 -> 10.0.0.0, 10.0.0.1)
#     NOTE: cidrhost treats the IP portion as the network address, so two
#     clusters MUST use distinct /24 networks to keep per-node IPs disjoint.
#     The WP03 prompt's ip_start="10.0.0.211" notation is the operator's intent
#     for "starting near .211"; we translate that intent into a distinct /24.
#   - pod_cidr 10.44.0.0/16, svc_cidr 10.45.0.0/16 (cicd: 10.42/10.43)
#   - cf_tunnel_name "apps" (cicd: "cicd")
#
# This root reads SS0's output.json (tokens) and SS1's build/image-id.txt
# (template VMID) via data sources. Secrets never live in state.
###############################################################################

terraform {
  required_version = ">= 1.6.0"

  # Backend: GitLab-managed Terraform state.
  # Project: infra-state/bigbertha (project_id=84156476) at gitlab.com.
  # Per-stack state name: cluster-apps.
  # Connection parameters supplied at init time via scripts/gitlab_backend.sh.
  backend "http" {}

  required_providers {
    proxmox = {
      source  = "bpg/proxmox"
      version = ">= 0.111.1"
    }
    helm = {
      source  = "hashicorp/helm"
      version = ">= 2.0.0"
    }
    local = {
      source  = "hashicorp/local"
      version = ">= 2.0.0"
    }
  }
}

# ---------------------------------------------------------------------------
# Data sources for upstream contracts.
# ---------------------------------------------------------------------------

data "local_file" "image_id" {
  filename = "${path.module}/../../../build/image-id.txt"
}

data "local_sensitive_file" "tokens_output" {
  filename = "${path.module}/../../../infra/tokens/output.json"
}

# ---------------------------------------------------------------------------
# Sibling Cluster collision check (M3).
#
# We glob clusters/*/output.json (excluding ourselves) and assert none
# contains the same cluster_name. We also reject apps if cicd's output.json
# has already been written with the apps name (which would mean a previous
# plan run set a collision -- in practice this fires only after a destructive
# rename of a sibling cluster).
# ---------------------------------------------------------------------------

data "local_file" "sibling_outputs" {
  for_each = {
    for p in fileset("${path.module}/..", "*/output.json") :
    p => "${path.module}/../${p}"
    if !startswith(p, "apps/")
  }

  filename = each.value
}

resource "terraform_data" "cluster_name_unique" {
  input = {
    cluster_name     = "apps"
    # Ignore sibling outputs that don't parse as JSON (live
    # hosts may have transient files such as build/image-id.txt
    # if the glob pattern inadvertently catches them, and the
    # tofu-test mock provider returns a primitive number by
    # default). Try/jsondecode returns "unknown" for unparseable
    # siblings and those are filtered out below.
    sibling_clusters = [for f in data.local_file.sibling_outputs : try(jsondecode(f.content).cluster_name, null) if try(jsondecode(f.content).cluster_name, null) != null]
  }

  lifecycle {
    precondition {
      condition     = !contains([for f in data.local_file.sibling_outputs : try(jsondecode(f.content).cluster_name, null) if try(jsondecode(f.content).cluster_name, null) != null], "apps")
      error_message = "cluster_name 'apps' collides with an existing sibling Cluster's output.json."
    }
  }
}

# ---------------------------------------------------------------------------
# SS1 contract enforcement: build/image-id.txt must be non-empty (M3 + FR-002).
# ---------------------------------------------------------------------------

resource "terraform_data" "image_id_present" {
  input = {
    image_id = data.local_file.image_id.content
  }

  lifecycle {
    precondition {
      condition     = length(trimspace(data.local_file.image_id.content)) > 0
      error_message = "build/image-id.txt is empty or missing; run tools/build_image.py first to bake the Talos template."
    }
  }
}

# ---------------------------------------------------------------------------
# Module invocation.
# ---------------------------------------------------------------------------

module "apps" {
  source = "../../modules/proxmox-k3s-cluster"

  pve_node = "BigBertha"

  cluster_name                = "apps"
  vip                         = "10.0.0.40"
  vmid_start                  = 210
  ip_start                    = "10.0.2.0/24"   # apps nodes in 10.0.2.0/24 (cicd is in 10.0.1.0/24). Both host vnet0 = 10.0.0.1/8 routes these. (cidrhost on 10.0.2.0/24 yields 10.0.2.0, 10.0.2.1.)
  image_id                    = length(data.local_file.image_id.content) > 0 ? chomp(data.local_file.image_id.content) : ""
  vnet_bridge                 = "vnet0"
  pod_cidr                    = "10.44.0.0/16"
  svc_cidr                    = "10.45.0.0/16"
  cf_api_token                = jsondecode(data.local_sensitive_file.tokens_output.content).cf_api_token
  cf_account_id               = jsondecode(data.local_sensitive_file.tokens_output.content).cf_account_id
  cf_tunnel_name              = "apps"
  cf_ingress_class            = "cloudflare-tunnel"
  cf_publish_traefik_publicly = false

  # Live-host pin: BigBertha's only lvmthin pool with the
  # Phase-1-baked disk image is data1. See SKILL.md Step 1b.1 + 1b.7
  # and infra/modules/proxmox-k3s-cluster/variables.tf::disk_storage_pool.
  disk_storage_pool             = "data1"

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

  depends_on = [
    terraform_data.cluster_name_unique,
    terraform_data.image_id_present,
  ]
}