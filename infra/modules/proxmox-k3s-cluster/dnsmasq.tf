###############################################################################
# dnsmasq ethers reservation.
#
# Reserves the cluster VIP in the vnet0 dnsmasq ethers file BEFORE any VM clone
# is started. The VIP is a single shared entry; each node's own IP gets a
# separate entry too.
#
# Authoritative DNS for this host is PowerDNS (see powerdns.tf). The
# proxmox_virtual_environment_hosts resources that used to live here were
# a no-op against PVE's local hosts file (PowerDNS overrides them) and
# produced persistent drift on every plan. They have been removed.
#
# The depends_on chain on terraform_data.vip_in_dhcp_range ensures we fail
# validation before touching the live PVE host.
###############################################################################