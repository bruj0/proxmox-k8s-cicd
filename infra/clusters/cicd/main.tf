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
    powerdns = {
      source  = "pan-net/powerdns"
      version = ">= 1.5.0"
    }
  }
}

# PowerDNS authoritative DNS for the cluster's records. Modules can't
# declare their own provider blocks (they collide with depends_on), so the
# provider is configured here at the root and inherited into the module.
#
# NOTE: 10.0.0.3:8081 is the SDN-internal PowerDNS, only reachable from
# BigBertha. scripts/apply_tofu.py opens an SSH tunnel
# (ssh -L 8081:10.0.0.3:8081 root@kvm.bruj0.net) for the duration of the
# apply, so we connect to localhost on the operator host.
#
# Empty api_key default lets `tofu test` pass without secrets -- the
# powerdns_record resources short-circuit via local.powerdns_enabled
# when api_key is empty, so no real API calls are made.
provider "powerdns" {
  api_key    = var.powerdns_api_key
  server_url = "http://127.0.0.1:8081"
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
      error_message = "build/image-id.txt is empty or missing; run tools/build_image.py first to bake the Ubuntu+k3s template."
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
  vmid_start                  = 200
  image_id                    = length(data.local_file.image_id.content) > 0 ? chomp(data.local_file.image_id.content) : ""
  vnet_bridge                 = "vnet0"
  # 2026-07-08: vip + ip_start inputs removed from the module. The
  # bootstrap discovers per-node IPs from Proxmox SDN via
  # qemu-guest-agent at runtime (see scripts/sync_dns_to_sdn.py),
  # and the cluster VIP is a kube-vip Service concept not owned by
  # tofu.
  #
  # pod_cidr / svc_cidr / cluster_dns are kept here so each cluster
  # root can pin distinct, non-host-LAN-overlapping RFC1918 ranges.
  # cicd uses 172.16/172.17, apps uses 172.20/172.21 -- the 3-step
  # gap makes tcpdumps visually distinguishable per cluster. These
  # are cluster scopes, NOT Proxmox-managed IPs.
  pod_cidr                    = "172.16.0.0/16"
  svc_cidr                    = "172.17.0.0/16"
  cluster_dns                 = "172.17.0.10"
  cf_api_token                = jsondecode(data.local_sensitive_file.tokens_output.content).cf_api_token
  cf_account_id               = jsondecode(data.local_sensitive_file.tokens_output.content).cf_account_id
  cf_tunnel_name              = "cicd"
  cf_ingress_class            = "cloudflare-tunnel"
  cf_publish_traefik_publicly = false

  # Live-host pin: BigBertha's only lvmthin pool with the
  # Phase-1-baked disk image is data1. See SKILL.md Step 1b.1 + 1b.7
  # and infra/modules/proxmox-k3s-cluster/variables.tf::disk_storage_pool.
  disk_storage_pool             = "data1"

  # PowerDNS authoritative DNS for this cluster. Set TF_VAR_powerdns_api_key
  # via scripts/apply_tofu.py (reads POWERDNS_API_KEY from .env). Empty
  # disables record creation -- the rest of the cluster still applies.
  powerdns_api_key              = var.powerdns_api_key

  control_plane = {
    count   = 1
    cpu     = 4
    ram_mb  = 4096
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