###############################################################################
# Cluster instance: clusters/cicd.
#
# This is the first root that consumes modules/proxmox-k3s-cluster.
# Reads SS0's output.json (tokens) and SS1's build/image-id.txt (template VMID)
# via data sources. All secrets come from env -> SS0 -> here; we never persist
# them in state.
#
# Identity check (M3): we assert that this cluster_name ("cicd") does not
# collide with any other Cluster that has been planned by a sibling root
# (clusters/*/main.tf). The simple implementation here is to refuse to plan
# if a sibling's output.json already references the same cluster_name.
###############################################################################

terraform {
  required_version = ">= 1.6.0"

  # Backend: GitLab-managed Terraform state.
  # Project: infra-state/bigbertha (project_id=84156476) at gitlab.com.
  # Per-stack state name: cluster-cicd.
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
# contains the same cluster_name.
# ---------------------------------------------------------------------------

data "local_file" "sibling_outputs" {
  for_each = {
    for p in fileset("${path.module}/..", "*/output.json") :
    p => "${path.module}/../${p}"
    if !startswith(p, "cicd/")
  }

  filename = each.value
}

resource "terraform_data" "cluster_name_unique" {
  input = {
    cluster_name     = "cicd"
    # Ignore sibling outputs that don't parse as JSON (see apps
    # main.tf comment for details).
    sibling_clusters = [for f in data.local_file.sibling_outputs : try(jsondecode(f.content).cluster_name, null) if try(jsondecode(f.content).cluster_name, null) != null]
  }

  lifecycle {
    precondition {
      condition     = !contains([for f in data.local_file.sibling_outputs : try(jsondecode(f.content).cluster_name, null) if try(jsondecode(f.content).cluster_name, null) != null], "cicd")
      error_message = "cluster_name 'cicd' collides with an existing sibling Cluster's output.json."
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

module "cicd" {
  source = "../../modules/proxmox-k3s-cluster"

  pve_node                    = "BigBertha"
  cluster_name                = "cicd"
  vip                         = "10.0.0.30"
  vmid_start                  = 200
  ip_start                    = "10.0.1.0/24"
  image_id                    = length(data.local_file.image_id.content) > 0 ? chomp(data.local_file.image_id.content) : ""
  vnet_bridge                 = "vnet0"
  pod_cidr                    = "10.42.0.0/16"
  svc_cidr                    = "10.43.0.0/16"
  cf_api_token                = jsondecode(data.local_sensitive_file.tokens_output.content).cf_api_token
  cf_account_id               = jsondecode(data.local_sensitive_file.tokens_output.content).cf_account_id
  cf_tunnel_name              = "cicd"
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