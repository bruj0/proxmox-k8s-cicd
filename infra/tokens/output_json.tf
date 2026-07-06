###############################################################################
# output.json writer.
#
# Spec T007 mandates a `local_file` resource that writes output.json with
# chmod 0600. The hashicorp/local v2 provider deprecated the `sensitive_content`
# attribute on `local_file` in favour of the dedicated `local_sensitive_file`
# resource, which is what we use here. Functionally equivalent: the
# `content` attribute is automatically treated as sensitive, so secret values
# never appear in `tofu plan` output.
#
# The file is re-written on every apply (idempotent — same content, same
# permissions). The apply wrapper `scripts/apply.sh` no longer needs a manual
# `tofu output -json | jq` post-step.
###############################################################################

# bpg/proxmox v0.111.x exposes `proxmox_user_token.<...>.value` as the
# FULL api-token string in `USER@REALM!TOKENID=secret` form. The
# spec T007 contract splits this into `proxmox_token_id` (the prefix)
# and `proxmox_token_secret` (the suffix, bare UUID). PVEAuth header
# is `PVEAPIToken=USER@REALM!TOKENID=secret` -- consumers concatenate
# `${proxmox_token_id}=${proxmox_token_secret}` (see scripts/apply.sh).
locals {
  proxmox_token_full  = proxmox_user_token.k3s_terraform_tf.value
  proxmox_token_parts = split("=", local.proxmox_token_full)
  proxmox_token_only  = length(local.proxmox_token_parts) > 1 ? local.proxmox_token_parts[1] : ""
}

resource "local_sensitive_file" "tokens_output" {
  filename        = "${path.module}/output.json"
  file_permission = "0600"

  content = jsonencode({
    cloudflare_scoped_token = cloudflare_api_token.k3s_scoped.value
    cloudflare_account_id   = var.cloudflare_account_id
    cloudflare_zone_id      = var.cloudflare_zone_id
    proxmox_token_id        = "${proxmox_virtual_environment_user.k3s_terraform.user_id}!${proxmox_user_token.k3s_terraform_tf.token_name}"
    proxmox_token_secret    = local.proxmox_token_only
    pve_endpoint            = var.proxmox_endpoint
  })
}