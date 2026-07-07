###############################################################################
# Authoritative-DNS records (PowerDNS).
#
# Proxmox SDN on this host routes the cluster's authoritative DNS to
# PowerDNS (pdns @ 10.0.0.3:8081) -- not PVE's local hosts file. The
# bpg/proxmox `proxmox_virtual_environment_hosts` resource we used to
# write to `/nodes/<n>/hosts` was always a no-op for the cluster
# (PowerDNS overrides it) and produced persistent drift on every plan.
#
# pan-net/powerdns >= 1.5.x creates A records in the `intranet.local.`
# forward zone and PTR records in the `10.in-addr.arpa.` reverse zone
# -- the same two zones PVE configured via /cluster/sdn/dns.
#
# NOTE: This module declares NO `provider "powerdns"` block -- modules
# cannot carry provider configurations when called from roots that use
# `depends_on`/`count`/`for_each`. The provider is declared in each
# consuming root (infra/clusters/{cicd,apps}/main.tf) and inherited
# here via standard provider configuration.
#
# Records emitted per cluster:
#   A   <cluster>-cp-1.<forward_zone>   -> <local.nodes[0].ip>
#   A   <cluster>-w-1.<forward_zone>    -> <local.nodes[1].ip>
#   A   <cluster>-vip.<forward_zone>    -> <var.vip>
#   PTR <reversed-IP>.10.in-addr.arpa.  -> <host>.intranet.local.  (x3)
#
# Disabled when powerdns_api_key == "" so `tofu test` and CI runs
# without the secret still plan.
###############################################################################

locals {
  # powerdns_api_key is `sensitive = true`. Deriving the boolean gate via
  # length() would also propagate sensitivity, which is forbidden in
  # `for_each`. `nonsensitive()` strips the marker so the gate stays a
  # plain bool (the key itself never leaves the provider block).
  powerdns_enabled = length(nonsensitive(var.powerdns_api_key)) > 0

  # Build the list of forward + reverse records we want to exist.
  # Each entry: { name = "host.fqdn.", type = "A"|"PTR", value = "ip"|"fqdn." }
  powerdns_forward_records = concat(
    [for n in local.nodes : {
      name  = "${n.name}.${var.powerdns_forward_zone}"
      type  = "A"
      ttl   = 300
      value = n.ip
    }],
    [{
      name  = "${var.cluster_name}-vip.${var.powerdns_forward_zone}"
      type  = "A"
      ttl   = 300
      value = var.vip
    }],
  )

  # Reverse: split IP octets into the PTR-name suffix used by PowerDNS.
  powerdns_reverse_records = concat(
    [for n in local.nodes : {
      # 10.0.1.0 -> "0.1.0.10.in-addr.arpa."
      name  = "${join(".", reverse(split(".", n.ip)))}.${var.powerdns_reverse_zone}"
      type  = "PTR"
      ttl   = 300
      value = "${n.name}.${var.powerdns_forward_zone}"
    }],
    [{
      name  = "${join(".", reverse(split(".", var.vip)))}.${var.powerdns_reverse_zone}"
      type  = "PTR"
      ttl   = 300
      value = "${var.cluster_name}-vip.${var.powerdns_forward_zone}"
    }],
  )
}

# ---------------------------------------------------------------------------
# Forward A records (one resource per record; small count, cleaner state).
# ---------------------------------------------------------------------------

resource "powerdns_record" "forward" {
  for_each = local.powerdns_enabled ? {
    for r in local.powerdns_forward_records : r.name => r
  } : {}

  zone    = var.powerdns_forward_zone
  name    = each.value.name
  type    = each.value.type
  ttl     = each.value.ttl
  records = [each.value.value]
}

# ---------------------------------------------------------------------------
# Reverse PTR records.
# ---------------------------------------------------------------------------

resource "powerdns_record" "reverse" {
  for_each = local.powerdns_enabled ? {
    for r in local.powerdns_reverse_records : r.name => r
  } : {}

  zone    = var.powerdns_reverse_zone
  name    = each.value.name
  type    = each.value.type
  ttl     = each.value.ttl
  records = [each.value.value]
}