provider "cloudflare" {
  api_token = var.cloudflare_admin_token
}

provider "proxmox" {
  endpoint  = var.proxmox_api_url
  api_token = "${var.proxmox_api_token_id}=${var.proxmox_api_token_secret}"

  # SSH block is omitted intentionally: WP00 only mints tokens/roles, which
  # do not require file uploads. Downstream WPs that clone templates or upload
  # cloud-init snippets will need to add an ssh{} block here.
}