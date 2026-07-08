###############################################################################
# Input variables for the proxmox-k3s-cluster module.
#
# Every variable has an explicit `description` so the consuming root
# (clusters/cicd/main.tf) gets helpful errors. Validation preconditions
# live in main.tf as terraform_data resources (tofu validate requires
# runtime expressions which can't be in variable `validation` blocks).
###############################################################################

variable "pve_node" {
  type        = string
  default     = "proxmox-host"
  description = "Name of the Proxmox node (cluster member) where VMs are cloned and SDN hosts entries are written. Defaults to 'proxmox-host'; override in cluster root tfvars when the host has a different name."
}

variable "cluster_name" {
  type        = string
  description = "Globally-unique name for this Cluster. Used in PowerDNS hostnames, Cloudflare Tunnel name, and output.json."
}

# vip (kube-vip Service VIP) and ip_start (per-node CIDR) were removed
# 2026-07-08: IPs are owned by Proxmox SDN (DHCP, 10.0.0.50-200 range)
# and discovered post-apply via qemu-guest-agent network-get-interfaces
# (see scripts/sync_dns_to_sdn.py). The bootstrap uses these discovered
# IPs as the only source of truth -- not anything tofu can fabricate.
#
# vmid_start is still required because PVE VMIDs are operator-managed,
# not auto-allocated; the cluster module still needs an explicit start
# to compute vmid_end and detect collisions with existing VMs.

variable "vmid_start" {
  type        = number
  description = "First VMID to allocate. Range [vmid_start .. vmid_start + total - 1] must not overlap any existing VM in the target Proxmox host."
}

variable "image_id" {
  type        = string
  description = "Proxmox VMID of the Ubuntu+k3s image template baked by SS1 (build/image-id.txt). Empty or whitespace fails plan."
}

variable "control_plane" {
  type = object({
    count   = number
    cpu     = number
    ram_mb  = number
    disk_gb = number
  })
  description = "Control-plane node sizing. count must be 1 or 3 (FR-030)."
}

variable "workers" {
  type = object({
    count   = number
    cpu     = number
    ram_mb  = number
    disk_gb = number
  })
  description = "Worker node sizing. count may be 0, 1, or more."
}

# pod_cidr / svc_cidr / cluster_dns are k3s-internal cluster scopes, not
# Proxmox-managed IPs. They MUST be non-overlapping RFC1918 ranges (the
# legacy defaults 10.42/10.43/10.43.0.10 sat inside the host LAN
# 10.0.0.0/8 and broke pod->apiserver routing, k3s-io/k3s#4627).
# Defaults are 172.x so a fresh cluster works out of the box; per-cluster
# roots override them (cicd uses 172.16/17, apps uses 172.20/21) so
# tcpdumps are visually distinguishable.
variable "pod_cidr" {
  type        = string
  default     = "172.16.0.0/16"
  description = "Pod CIDR for the k3s cluster. MUST NOT overlap the host LAN."
}

variable "svc_cidr" {
  type        = string
  default     = "172.17.0.0/16"
  description = "Service CIDR for the k3s cluster. MUST NOT overlap the host LAN."
}

variable "cluster_dns" {
  type        = string
  default     = "172.17.0.10"
  description = "In-cluster coredns service IP. Must be inside svc_cidr."
}

variable "vnet_bridge" {
  type        = string
  default     = "vnet0"
  description = "Proxmox SDN bridge the cluster attaches to."
}

variable "cf_api_token" {
  type        = string
  sensitive   = true
  description = "Cloudflare scoped API token (from infra/tokens/output.json). Required only when cf_publish_traefik_publicly=true."
}

variable "cf_account_id" {
  type        = string
  sensitive   = true
  description = "Cloudflare account ID. Required only when cf_publish_traefik_publicly=true."
}

variable "cf_tunnel_name" {
  type        = string
  default     = "k3s-prod"
  description = "Name of the Cloudflare Tunnel resource to bind to."
}

variable "cf_ingress_class" {
  type        = string
  default     = "cloudflare-tunnel"
  description = "Name of the ingress class owned by cloudflare-tunnel-ingress-controller."
}

variable "cf_publish_traefik_publicly" {
  type        = bool
  default     = false
  description = "Operator opt-in to publish Traefik on hostPorts AND install the cloudflare-tunnel-ingress-controller Helm release. Default off (NFR-007). Setting this true is mutually exclusive with the default Traefik ClusterIP path."
}

# talos_version removed 2026-07-08 (Talos-to-Ubuntu pivot already
# landed in WP01/02; this var was a leftover artifact used only by the
# per-VM machineconfig renderer, which is also being removed in this
# refactor).

variable "disk_storage_pool" {
  type        = string
  default     = "data1"
  description = <<-EOT
    PVE storage pool to put the cloned VM's root disk on. Live-host
    default is `data1` (BigBertha's only lvmthin pool with the disk
    image baked by SS1). Cleanroom defaults used `local-lvm`, which
    doesn't exist on hosts installed with separate lvmthin pools.
    Pinned 2026-07-06 after the Step-2 apply surfaced "Provider
    produced inconsistent result" on datastore_id.
  EOT
}

# ---------------------------------------------------------------------------
# PowerDNS authoritative-DNS variables.
#
# The cluster's authoritative DNS is PowerDNS (pdns @ 10.0.0.3:8081 on
# the SDN, zone intranet.local. forward + 10.in-addr.arpa. reverse).
# The pan-net/powerdns provider writes A + PTR records for every node
# here, pointing at the SDN DHCP-allocated addresses. PowerDNS is the
# single source of truth for cluster DNS; PVE's hosts file is not used.
#
# powerdns_api_key defaults to "" which skips record creation entirely --
# `tofu test` and CI runs that don't have the secret can still plan.
# ---------------------------------------------------------------------------

variable "powerdns_endpoint" {
  type        = string
  default     = "http://10.0.0.3:8081"
  description = "PowerDNS API base URL. Default matches the SDN `pdns` instance on this host (PVE-managed at 10.0.0.3:8081)."
}

variable "powerdns_api_key" {
  type        = string
  default     = ""
  sensitive   = true
  description = <<-EOT
    PowerDNS API key. Sourced from PowerDNS_API_KEY env (translated to
    TF_VAR_powerdns_api_key by scripts/apply_tofu.py) or set in the root
    tfvars for a one-shot apply. Empty string disables DNS record
    creation entirely -- the rest of the module still applies.
  EOT
}

variable "powerdns_forward_zone" {
  type        = string
  default     = "intranet.local."
  description = "PowerDNS forward zone suffix. Records are emitted as `<host>.<cluster_name>.intranet.local.` (FQDN with trailing dot). Live-host default matches PVE /cluster/sdn/zones intranet zone."
}

variable "powerdns_reverse_zone" {
  type        = string
  default     = "10.in-addr.arpa."
  description = "PowerDNS reverse zone suffix. PTR records use cidrhost(num) split into octets. Live-host default matches PVE SDN reversedns=pdns config."
}