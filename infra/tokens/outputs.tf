###############################################################################
# Outputs — consumed by downstream WPs (WP02-WP06) via output.json.
#
# The primary inter-system contract is `infra/tokens/output.json` (gitignored,
# chmod 0600), written by the `local_sensitive_file.tokens_output` resource
# declared in output_json.tf. This file is what WP02-WP06 read.
#
# The Terraform outputs below mirror the output.json keys for two purposes:
#   1. `tofu output` ergonomics during operator troubleshooting.
#   2. Re-exporting the file path so downstream tooling can `tofu output
#      -raw tokens_output_path` to find the contract file.
#
# Every secret-bearing output is marked `sensitive` so it does not leak into
# `tofu plan` / `tofu output` streams.
###############################################################################

output "cloudflare_scoped_token" {
  description = "Value of the Cloudflare scoped API token. Stored in output.json (gitignored, mode 0600)."
  value       = cloudflare_api_token.k3s_scoped.value
  sensitive   = true
}

output "cloudflare_scoped_token_id" {
  description = "Stable id of the Cloudflare scoped API token."
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
  description = "Proxmox API token secret. Stored in output.json (gitignored, mode 0600)."
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
  value       = sort(proxmox_virtual_environment_role.k3s_cluster.privileges)
}

output "cloudflare_permission_groups" {
  description = "Permission group labels granted to the scoped token. For audit / runbook consumption."
  value = {
    zone_read   = data.cloudflare_account_api_token_permission_groups_list.zone_read.result[0].name
    dns_edit    = data.cloudflare_account_api_token_permission_groups_list.dns_edit.result[0].name
    tunnel_edit = data.cloudflare_account_api_token_permission_groups_list.tunnel_edit.result[0].name
  }
}

output "tokens_output_path" {
  description = "Absolute path to output.json on disk. Downstream WPs read this file to consume the inter-system contract."
  value       = abspath(local_sensitive_file.tokens_output.filename)
}