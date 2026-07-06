###############################################################################
# dnsmasq ethers reservation.
#
# Reserves the cluster VIP in the vnet0 dnsmasq ethers file BEFORE any VM clone
# is started. The VIP is a single shared entry; each node's own IP gets a
# separate entry too.
#
# The bpg/proxmox virtual_environment_hosts resource writes to the
# Proxmox SDN hosts file via the /cluster/sdn/vnets API. The depends_on chain
# on terraform_data.vip_in_dhcp_range ensures we fail validation before
# touching the live PVE host.
###############################################################################

# VIP reservation (synthetic hostname so the operator can ping by name).
resource "proxmox_virtual_environment_hosts" "vip_reservation" {
  node_name = var.pve_node

  entry {
    address   = var.vip
    hostnames = ["${var.cluster_name}-vip"]
  }

  lifecycle {
    precondition {
      condition     = !contains(local.nodes[*].ip, var.vip)
      error_message = "vip ${var.vip} overlaps a node IP; cannot reserve."
    }
  }

  depends_on = [
    terraform_data.vip_in_dhcp_range,
  ]
}

# Per-node entries.
resource "proxmox_virtual_environment_hosts" "node" {
  for_each = { for n in local.nodes : n.name => n }

  node_name = var.pve_node

  entry {
    address   = each.value.ip
    hostnames = [each.value.talos_hostname]
  }

  depends_on = [
    proxmox_virtual_environment_hosts.vip_reservation,
    terraform_data.vip_in_dhcp_range,
  ]
}