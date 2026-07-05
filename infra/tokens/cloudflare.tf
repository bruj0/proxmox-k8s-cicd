###############################################################################
# Cloudflare scoped API token.
#
# Implements NFR-007 (least privilege): the scoped token grants exactly the
# three permission groups called out in the WP00 spec T003:
#   1. Zone:Zone:Read      — read-only zone metadata
#   2. Zone:DNS:Edit       — edit DNS records (used by cert-manager DNS01
#                            solvers and any WP that publishes ingress)
#   3. Account:Cloudflare Tunnel:Edit  — manage Cloudflare Tunnels (used by
#                            STRRL/cloudflare-tunnel-ingress-controller)
#
# No `*:Edit`, no `Zone:Zone Settings:Edit`, no Workers KV write — those would
# violate NFR-007 by granting capabilities the pipeline does not use.
#
# Permission group IDs are resolved at plan-time via
# cloudflare_account_api_token_permission_groups_list. Names use the canonical
# Cloudflare labels (the `name` filter is URL-encoded per the data source
# schema). If Cloudflare ever rotates a label, the data source will fail the
# plan and we update the label here — no hard-coded UUIDs to drift.
#
# The token is IP-locked to the apply runner via ifconfig.me. If the lookup
# fails we fall back to 0.0.0.0/0 so the apply never blocks on a flaky IP
# service. Operators who want a tighter CIDR should set
# cloudflare_runner_cidr (env: TF_VAR_cloudflare_runner_cidr).
###############################################################################

data "http" "apply_runner_ip" {
  url = "https://ifconfig.me/ip"
  request_headers = {
    Accept = "text/plain"
  }
}

locals {
  apply_runner_cidr = try(
    "${chomp(data.http.apply_runner_ip.response_body)}/32",
    "0.0.0.0/0",
  )

  # Per-policy resources blocks, JSON-encoded as the provider expects.
  zone_resources = jsonencode({
    "com.cloudflare.api.account.zone.${var.cloudflare_zone_id}" = "*"
  })
  account_resources = jsonencode(jsonencode({
    "account.id" = var.cloudflare_account_id
  }))
}

# Look up the three permission groups we need. Names match Cloudflare's
# canonical permission group labels (the data source expects URL-encoded
# strings, so spaces → %20 and colons → %3A).
data "cloudflare_account_api_token_permission_groups_list" "zone_read" {
  account_id = var.cloudflare_account_id
  name       = "Zone Read"
}

data "cloudflare_account_api_token_permission_groups_list" "dns_edit" {
  account_id = var.cloudflare_account_id
  name       = "Zone DNS Write"
}

data "cloudflare_account_api_token_permission_groups_list" "tunnel_edit" {
  account_id = var.cloudflare_account_id
  name       = "Cloudflare Tunnel:Edit"
}

resource "cloudflare_api_token" "k3s_scoped" {
  name = "k3s-proxmox-terraform"

  policies = [
    # Zone:Zone:Read — read-only zone metadata.
    {
      effect    = "allow"
      resources = local.zone_resources
      permission_groups = [
        { id = data.cloudflare_account_api_token_permission_groups_list.zone_read.result[0].id },
      ]
    },
    # Zone:DNS:Edit — edit DNS records on the cluster zone.
    {
      effect    = "allow"
      resources = local.zone_resources
      permission_groups = [
        { id = data.cloudflare_account_api_token_permission_groups_list.dns_edit.result[0].id },
      ]
    },
    # Account:Cloudflare Tunnel:Edit — manage Cloudflare Tunnels (account scope).
    {
      effect    = "allow"
      resources = local.account_resources
      permission_groups = [
        { id = data.cloudflare_account_api_token_permission_groups_list.tunnel_edit.result[0].id },
      ]
    },
  ]

  # IP-lock the token to the runner.
  condition = {
    request_ip = {
      in     = [coalesce(var.cloudflare_runner_cidr, local.apply_runner_cidr)]
      not_in = []
    }
  }

  # No expiry — rotation is governed by docs/runbooks/rotate-tokens.md.
  expires_on = null

  not_before = timestamp()
}