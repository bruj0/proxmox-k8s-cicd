###############################################################################
# Provider versions for the proxmox-k3s-cluster module.
#
# Pinned per the spec Technical context:
#   - OpenTofu       >= 1.6.0   (module tested with v1.12)
#   - bpg/proxmox    >= 0.111.1 (the spec T001-mandated minimum)
#   - hashicorp/helm >= 2.x     (only the helm_release resource is used)
#   - hashicorp/local >= 2.x    (local_sensitive_file is required by SS2->SS3 contract)
#
# Versions.lock.yaml mirrors these constraints with research rationale.
###############################################################################

terraform {
  required_version = ">= 1.6.0"

  # NOTE: no `backend` block here. Per OpenTofu docs, a backend block in
  # a module is silently ignored with a warning ("Any selected backend
  # applies to the entire configuration, so OpenTofu expects provider
  # configurations only in the root module"), and modules never carry
  # state. State for instances of this module lives in the calling root
  # (e.g. infra/clusters/cicd at the GitLab state name "cluster-cicd").

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