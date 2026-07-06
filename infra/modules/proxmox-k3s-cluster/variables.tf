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
  description = "Globally-unique name for this Cluster. Used in Talos cert prefix, dnsmasq hostnames, and output.json."
}

variable "vip" {
  type        = string
  description = "Single virtual IP within vnet0 that kube-vip binds to the active control-plane node."
}

variable "vmid_start" {
  type        = number
  description = "First VMID to allocate. Range [vmid_start .. vmid_start + total - 1] must not overlap any existing VM in the target Proxmox host."
}

variable "ip_start" {
  type        = string
  description = <<-EOT
    CIDR network in which per-node IPs are placed (e.g. 10.0.0.0/24).
    cidrhost(var.ip_start, i) returns the i-th host in that network (index 0 is the
    network address). The IP portion of the CIDR is treated as the NETWORK, not as
    a starting host -- so 10.0.0.201/24 yields hosts 10.0.0.0, 10.0.0.1, ... (NOT
    10.0.0.201). The /24 mask is REQUIRED.
  EOT
}

variable "image_id" {
  type        = string
  description = "Proxmox VMID of the Talos image template baked by SS1 (build/image-id.txt). Empty or whitespace fails plan."
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

variable "pod_cidr" {
  type        = string
  default     = "10.42.0.0/16"
  description = "Pod CIDR for the k3s cluster. Used in Talos machineconfig."
}

variable "svc_cidr" {
  type        = string
  default     = "10.43.0.0/16"
  description = "Service CIDR for the k3s cluster. Used in Talos machineconfig."
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

variable "talos_version" {
  type        = string
  default     = "v1.10.0"
  description = "Talos version baked into the image template. Used for the per-VM Talos machineconfig."
}