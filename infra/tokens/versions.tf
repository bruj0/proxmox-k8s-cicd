###############################################################################
# Provider version constraints for infra/tokens.
#
# Pins match the WP00 spec (Technical context):
#   - OpenTofu >= 1.6 (we test with 1.12 in CI; 1.6 is the minimum supported)
#   - cloudflare/cloudflare >= 4.0 (spec); 5.x is the current stable per
#     context7 research on 2026-07-05 and supports the
#     cloudflare_account_api_token_permission_groups_list data source we use to
#     resolve permission group IDs at plan time. 4.x is the spec floor.
#   - bpg/proxmox >= 0.111.1 (spec); 0.111.1 is where the role/user/token
#     resource names we depend on live.
#
# The authoritative source for these constraints is `versions.lock.yaml` at the
# same directory — see WP00 review Issue 2.
###############################################################################

terraform {
  required_version = ">= 1.6.0"

  # Backend: GitLab-managed Terraform state.
  # Project: infra-state/bigbertha (project_id=84156476) at gitlab.com.
  # Per-stack state name: infra-tokens.
  # All connection / auth parameters are supplied at init time via
  # scripts/gitlab_backend.sh -- backend "http" body stays empty
  # so no credentials are ever written into this HCL.
  backend "http" {}

  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = ">= 4.0"
    }
    proxmox = {
      source  = "bpg/proxmox"
      version = ">= 0.111.1"
    }
    local = {
      source  = "hashicorp/local"
      version = ">= 2.0"
    }
  }
}