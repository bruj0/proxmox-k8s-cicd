###############################################################################
# Cloudflare scoped API token.
#
# Least-privilege scope (NFR-007 / M7): only the permissions WP01-WP06 actually
# need — Zone DNS Read/Write on the cluster zone, Workers KV Storage Write at
# the account level. No account-wide write, no user-billing access.
#
# Permission group IDs are resolved at plan-time via the
# cloudflare_account_api_token_permission_groups_list data source so we never
# hard-code UUIDs that Cloudflare might rotate. The data source is keyed by
# `name` (a stable label like "Zone DNS Write").
#
# The condition restricts the token to requests originating from the public IP
# that runs `tofu apply`. We look that IP up with ifconfig.me — if the lookup
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

  # Per-policy resources block, JSON-encoded as the provider expects.
  zone_resources = jsonencode({
    "com.cloudflare.api.account.zone.${var.cloudflare_zone_id}" = "*"
  })
  account_resources = jsonencode(jsonencode({
    "account.id" = var.cloudflare_account_id
  }))
}

# Look up the three permission groups we need. Names are stable labels, IDs
# are the UUIDs Cloudflare uses in the API.
data "cloudflare_account_api_token_permission_groups_list" "dns_read" {
  account_id = var.cloudflare_account_id
  name       = "Zone DNS Read"
}

data "cloudflare_account_api_token_permission_groups_list" "dns_write" {
  account_id = var.cloudflare_account_id
  name       = "Zone DNS Write"
}

data "cloudflare_account_api_token_permission_groups_list" "kv_write" {
  account_id = var.cloudflare_account_id
  name       = "Workers KV Storage Write"
}

resource "cloudflare_api_token" "k3s_scoped" {
  name = "k3s-proxmox-terraform"

  policies = [
    # Read-only Zone DNS — used by WP02/WP06 to enumerate existing records.
    {
      effect    = "allow"
      resources = local.zone_resources
      permission_groups = [
        { id = data.cloudflare_account_api_token_permission_groups_list.dns_read.result[0].id },
      ]
    },
    # Write Zone DNS on the cluster zone — used by WP05/WP06 to publish
    # ingress records and by cert-manager DNS01 solvers.
    {
      effect    = "allow"
      resources = local.zone_resources
      permission_groups = [
        { id = data.cloudflare_account_api_token_permission_groups_list.dns_write.result[0].id },
      ]
    },
    # Workers KV Storage Write at the account level — used by WP02 to store
    # cluster state (kubeconfig, bootstrap manifests) outside the git repo.
    {
      effect    = "allow"
      resources = local.account_resources
      permission_groups = [
        { id = data.cloudflare_account_api_token_permission_groups_list.kv_write.result[0].id },
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

  expires_on = null

  not_before = timestamp()
}