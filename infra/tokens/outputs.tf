###############################################################################
# Outputs — consumed by downstream WPs (WP02-WP06) via output.json.
#
# Every secret-bearing output is marked sensitive so it does not leak into
# `tofu plan` / `tofu output` streams. The apply wrapper writes output.json
# with the values intact (the file is .gitignored — see .gitignore in this
# directory).
###############################################################################

output "cloudflare_scoped_token" {
  description = "Value of the Cloudflare scoped API token. Store only in output.json (gitignored)."
  value       = cloudflare_api_token.k3s_scoped.value
  sensitive   = true
}

output "cloudflare_scoped_token_id" {
  description = "Stable id of the Cloudflare scoped API token (useful for rotations and audits)."
  value       = cloudflare_api_token.k3s_scoped.id
}

output "cloudflare_scoped_token_expires_on" {
  description = "ISO-8601 expiry of the scoped token. Always null today; rotation is governed by the runbook."
  value       = cloudflare_api_token.k3s_scoped.expires_on
}

output "proxmox_token_id" {
  description = "Proxmox API token id (USER@REALM!TOKEN) — safe to commit."
  value       = "${proxmox_virtual_environment_user.k3s_terraform.user_id}!${proxmox_user_token.k3s_terraform_tf.token_name}"
}

output "proxmox_token_value" {
  description = "Proxmox API token secret. Store only in output.json (gitignored)."
  value       = proxmox_user_token.k3s_terraform_tf.value
  sensitive   = true
}

output "proxmox_endpoint" {
  description = "Proxmox endpoint URL the token is bound to."
  value       = var.proxmox_endpoint
}

output "proxmox_role_id" {
  description = "Proxmox role id granted to the k3s-terraform user."
  value       = proxmox_virtual_environment_role.k3s_cluster.role_id
}

output "proxmox_role_privileges" {
  description = "Effective privilege set on the k3s-cluster role (sorted)."
  value       = sort(tolist(proxmox_virtual_environment_role.k3s_cluster.privileges))
}

output "cloudflare_permission_groups" {
  description = "Permission group labels granted to the scoped token. For audit / runbook consumption."
  value = {
    dns_read  = data.cloudflare_account_api_token_permission_groups_list.dns_read.result[0].name
    dns_write = data.cloudflare_account_api_token_permission_groups_list.dns_write.result[0].name
    kv_write  = data.cloudflare_account_api_token_permission_groups_list.kv_write.result[0].name
  }
}