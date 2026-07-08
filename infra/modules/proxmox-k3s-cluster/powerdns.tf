###############################################################################
# Authoritative-DNS records (PowerDNS) — disabled by design.
#
# 2026-07-08: this module no longer writes PowerDNS records. The
# per-node IPs the module used to fabricate (via cidrhost(var.ip_start, i))
# are now empty strings — Proxmox SDN auto-allocates them post-apply
# and the bootstrap discovers them via qemu-guest-agent. The cluster
# VIP is gone entirely (kube-vip Service owns it at runtime, not tofu).
#
# The single source of truth for cluster DNS records is now
# `scripts/sync_dns_to_sdn.py`, which reads the SDN DHCP-allocated IPs
# via qm agent <vmid> network-get-interfaces and PATCHes the same
# A + PTR records this module used to write. Run that script after
# every `tofu apply` of a cluster root.
#
# The `powerdns_api_key` / `powerdns_endpoint` / `powerdns_forward_zone`
# / `powerdns_reverse_zone` variables are RETAINED for compatibility
# with scripts/sync_dns_to_sdn.py and the consumer roots' provider
# blocks; the module itself does not consume them anymore.
#
# See docs/architecture.md (DNS Records section) for the full design.
###############################################################################

locals {
  # Kept as a sentinel so anyone reading the file understands the gate
  # moved into scripts/sync_dns_to_sdn.py.
  powerdns_records_written_by = "scripts/sync_dns_to_sdn.py (post-apply)"
}