###############################################################################
# Proxmox least-privilege role, user and API token.
#
# Implements NFR-007 (least privilege): the role `k3s-cluster` grants exactly
# the 12 privileges called out in the WP00 spec T005 — the minimum set the
# bpg/proxmox provider and the sergelogvinov/proxmox-csi-plugin require to
# provision VMs, attach disks, configure SDN, and use datastores.
#
# The user is created in the `pam` realm (local auth) so the lifecycle is
# fully owned by this OpenTofu root. The token is generated with
# `privileges_separation = false` so the same scope applies — split tokens are
# not needed because the entire root only mints / rotates this token.
#
# Resource naming:
#   - proxmox_virtual_environment_role / proxmox_virtual_environment_user: the
#     long-form names. The short aliases (proxmox_role / proxmox_user) are
#     not exposed by provider v0.111.x.
#   - proxmox_acl / proxmox_user_token: the short-form names. The long
#     aliases (proxmox_virtual_environment_acl / virtual_environment_user_token)
#     are deprecated and will be removed in provider v1.0.
###############################################################################

resource "proxmox_virtual_environment_role" "k3s_cluster" {
  role_id = var.proxmox_role_id

  # Privilege set extends the 12 from spec T005 with the 8 privs Packer
  # (hashicorp/proxmox proxmox-clone v1.2.3) and the cluster tofu modules
  # actually need end-to-end:
  #   VM.Audit                 — read VM 999 cfg / status (Packer, tofu)
  #   VM.Clone                 — clone base VMID 999 to template 900 (Packer)
  #   VM.Migrate               — failed-template cleanup (Packer)
  #   VM.Config.CDROM          — attach/detach Talos ISO (tofu, Packer)
  #   VM.Config.HWType         — set machine=q35 (Packer UEFI boot)
  #   VM.Snapshot.Rollback     — restore from snapshot if template bake fails
  #   Sys.Audit                — required for /access namespace reads (PVE)
  #   Sys.Modify               — required for /cluster/sdn/vnets writes
  #                              (proxmox_virtual_environment_hosts writes
  #                               the vnet0 hosts file; PVE 9.2.x rejects
  #                               without Sys.Modify, even with SDN.Use).
  # Total: 20 privs. Records NFR-007's intent: least-privilege for the
  # *cluster lifecycle*, not just the bpg/proxmox provider primitives.
  privileges = sort([
    "Datastore.AllocateSpace",
    "Datastore.Audit",
    "SDN.Use",
    "Sys.Audit",
    "Sys.Modify",
    "VM.Allocate",
    "VM.Audit",
    "VM.Clone",
    "VM.Config.CPU",
    "VM.Config.CDROM",
    "VM.Config.Disk",
    "VM.Config.HWType",
    "VM.Config.Memory",
    "VM.Config.Network",
    "VM.Config.Options",
    "VM.Console",
    "VM.Migrate",
    "VM.PowerMgmt",
    "VM.Snapshot",
    "VM.Snapshot.Rollback",
  ])
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