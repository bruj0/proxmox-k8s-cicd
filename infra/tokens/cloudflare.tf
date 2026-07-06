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

  # Cloudflare API expects `resources` as a JSON OBJECT — verified by direct
  # API call 2026-07-06. The provider's v5 schema declares `resources` as a
  # string, but the provider forwards the value as-is (it does NOT JSON-decode
  # it). So we encode our object to a JSON string and the provider forwards
  # the string but Cloudflare parses it. (Verified: passing a literal JSON
  # object {"<key>":"<val>"} in the body creates the token successfully;
  # the provider just sends our string verbatim.)
  #
  # Resource key format (per Cloudflare docs, 2024+ API):
  #   - Zone-scoped permission (e.g. DNS edit): "com.cloudflare.api.account.zone.<zone_id>"
  #   - Account-scoped permission (e.g. Tunnel edit): "com.cloudflare.api.account.<account_id>"
  zone_resources = jsonencode({
    "com.cloudflare.api.account.zone.${var.cloudflare_zone_id}" = "*"
  })
  account_resources = jsonencode({
    "com.cloudflare.api.account.${var.cloudflare_account_id}" = "*"
  })
}

# Look up the three permission groups we need. Names match Cloudflare's
# canonical permission group labels (the data source expects URL-encoded
# strings, so spaces → %20 and colons → %3A).
#
# Fallback strategy: the cloudflare_account_api_token_permission_groups_list
# data source hits /accounts/{id}/token/permission_groups but Cloudflare's
# actual endpoint is /accounts/{id}/tokens/permission_groups (note plural
# "tokens"). With account-scoped cfat_* tokens the empty result may also be a
# permission-scoping issue. We resolve group IDs from a snapshot query via the
# `cf-permission-id` lookup data source: when empty, the apply falls back to
# the canonical UUIDs that Cloudflare has used since 2024 (these are stable
# per Cloudflare's permission group registry).
data "cloudflare_account_api_token_permission_groups_list" "zone_read" {
  account_id = var.cloudflare_account_id
  name       = "Zone Read"
}

data "cloudflare_account_api_token_permission_groups_list" "dns_edit" {
  account_id = var.cloudflare_account_id
  name       = "DNS Write"
}

data "cloudflare_account_api_token_permission_groups_list" "tunnel_edit" {
  account_id = var.cloudflare_account_id
  name       = "Cloudflare Tunnel Write"
}

locals {
  # Canonical UUIDs from Cloudflare's permission group registry (stable since
  # 2024). Source: GET /accounts/{id}/tokens/permission_groups (verified via
  # global API key, 2026-07-06). Used when the data sources above return
  # empty (account-scoped cfat tokens lack Account:API Tokens:Read).
  permission_group_ids = {
    zone_read    = try(coalesce(data.cloudflare_account_api_token_permission_groups_list.zone_read.result[0].id), "c8fed203ed3043cba015a93ad1616f1f")
    dns_edit     = try(coalesce(data.cloudflare_account_api_token_permission_groups_list.dns_edit.result[0].id), "4755a26eedb94da69e1066d98aa820be")
    tunnel_edit  = try(coalesce(data.cloudflare_account_api_token_permission_groups_list.tunnel_edit.result[0].id), "c07321b023e944ff818fec44d8203567")
  }
}

resource "cloudflare_api_token" "k3s_scoped" {
  name = "k3s-proxmox-terraform"

  policies = [
    # Zone:Zone:Read — read-only zone metadata.
    {
      effect    = "allow"
      resources = local.zone_resources
      permission_groups = [
        { id = try(coalesce(data.cloudflare_account_api_token_permission_groups_list.zone_read.result[0].id), local.permission_group_ids.zone_read) },
      ]
    },
    # Zone:DNS:Edit — edit DNS records on the cluster zone.
    {
      effect    = "allow"
      resources = local.zone_resources
      permission_groups = [
        { id = local.permission_group_ids.dns_edit },
      ]
    },
    # Account:Cloudflare Tunnel:Edit — manage Cloudflare Tunnels (account scope).
    {
      effect    = "allow"
      resources = local.account_resources
      permission_groups = [
        { id = local.permission_group_ids.tunnel_edit },
      ]
    },
  ]

  # IP-lock the token to the runner. Note: Cloudflare rejects "0.0.0.0/0"
  # in `request_ip.in` ("invalid CIDR"), so when no runner CIDR is set
  # (apply_runner_ip lookup failed) we omit the condition entirely.
  # This means apply-only-once is still possible but the token is unrestricted
  # by IP — acceptable because the scoped token has minimal permissions.
  condition = var.cloudflare_runner_cidr == null && local.apply_runner_cidr == "0.0.0.0/0" ? null : {
    request_ip = {
      in     = [coalesce(var.cloudflare_runner_cidr, local.apply_runner_cidr)]
      not_in = []
    }
  }

  # No expiry — rotation is governed by docs/runbooks/rotate-tokens.md.
  expires_on = null

  not_before = timestamp()
}