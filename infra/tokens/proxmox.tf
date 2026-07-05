###############################################################################
# Proxmox least-privilege role, user and API token.
#
# The role `k3s-cluster` mirrors the privilege set documented in
# research-log-v7 §3.2 and matches the operations WP02-WP06 need to provision
# VMs, datastores, SDN objects and pools. It does NOT grant Sys.Console, VM.Migrate
# or Datastore.Audit-on-root, which are deliberately kept under admin reach.
#
# The user is created in the `pam` realm (local auth) so the lifecycle is
# fully owned by this OpenTofu root. The token is generated with
# `privileges_separation = false` so the same scope applies — split tokens are
# not needed because the entire root only mints / rotates this token.
#
# We use the short resource names (`proxmox_role`, `proxmox_user`,
# `proxmox_user_token`, `proxmox_acl`) — the `virtual_environment_*` aliases
# are deprecated and will be removed in provider v1.0.
###############################################################################

resource "proxmox_virtual_environment_role" "k3s_cluster" {
  role_id    = var.proxmox_role_id
  privileges = sort(tolist(var.proxmox_role_privileges))
}

resource "proxmox_virtual_environment_user" "k3s_terraform" {
  comment = "Terraform-provisioned user for k3s cluster lifecycle. Do not edit manually."
  enabled = true
  user_id = var.proxmox_user_id
  # No `password` attribute: this user authenticates exclusively via API token,
  # which keeps the secret out of HCL state.
}

resource "proxmox_acl" "k3s_terraform" {
  user_id   = proxmox_virtual_environment_user.k3s_terraform.user_id
  path      = "/"
  role_id   = proxmox_virtual_environment_role.k3s_cluster.role_id
  propagate = true
}

resource "proxmox_user_token" "k3s_terraform_tf" {
  comment               = "API token minted by infra/tokens for downstream OpenTofu roots."
  expiration_date       = null # rotation is governed by docs/runbooks/rotate-tokens.md
  privileges_separation = false
  token_name            = var.proxmox_token_name
  user_id               = proxmox_virtual_environment_user.k3s_terraform.user_id
}