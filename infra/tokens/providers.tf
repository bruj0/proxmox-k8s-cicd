provider "cloudflare" {
  # Tokens module needs to:
  #   - list permission groups (requires Account:API Tokens:Edit on the
  #     admin token, which cfat_* tokens often lack)
  #   - create a scoped child API token (requires user-level auth; cfat_*
  #     tokens cannot do this — the POST /user/tokens endpoint needs
  #     X-Auth-Email + X-Auth-Key)
  # Global API key + email works for both. Operators without one fall back
  # to the scoped cfat_ token and accept that the scoped child token cannot
  # be auto-rotated. (See infra/tokens/CONTEXT.md M7 rationale.)
  #
  # The Cloudflare provider requires ExactlyOneOf(api_key, api_token), so we
  # pick whichever auth method the operator supplied. Operators with both
  # configured get the global key path (it has more permissions).
  api_key   = var.cloudflare_global_api_key != null ? var.cloudflare_global_api_key : null
  email     = var.cloudflare_global_api_key != null ? var.cloudflare_global_api_email : null
  api_token = var.cloudflare_global_api_key == null ? var.cloudflare_admin_token : null
}

provider "proxmox" {
  endpoint  = var.proxmox_api_url
  api_token = "${var.proxmox_api_token_id}=${var.proxmox_api_token_secret}"

  # SSH block is omitted intentionally: WP00 only mints tokens/roles, which
  # do not require file uploads. Downstream WPs that clone templates or upload
  # cloud-init snippets will need to add an ssh{} block here.
}