variable "cloudflare_admin_token" {
  description = "Cloudflare admin API token used to mint the scoped child token. Must be supplied via the CLOUDFLARE_TOKEN_CREATOR environment variable (sourced from .env at the repo root). Never commit or echo this value."
  type        = string
  sensitive   = true

  validation {
    condition     = length(var.cloudflare_admin_token) >= 40
    error_message = "CLOUDFLARE_TOKEN_CREATOR must be at least 40 characters; received value is too short to be a valid Cloudflare API token."
  }
}

variable "cloudflare_account_id" {
  description = "Cloudflare account ID under which the scoped token is issued."
  type        = string
}

variable "cloudflare_zone_id" {
  description = "Cloudflare zone ID used to scope the DNS-edit permission on the scoped token."
  type        = string
}

variable "cloudflare_runner_cidr" {
  description = "Optional CIDR (e.g. 203.0.113.5/32) to IP-lock the scoped token to. When null, the apply wrapper auto-detects the runner's public IP via ifconfig.me."
  type        = string
  default     = null

  validation {
    condition     = var.cloudflare_runner_cidr == null || can(cidrnetmask(var.cloudflare_runner_cidr))
    error_message = "cloudflare_runner_cidr must be a valid CIDR (e.g. 203.0.113.5/32) or null."
  }
}

variable "proxmox_api_url" {
  description = "Base URL of the Proxmox VE API endpoint (e.g. https://pve.example.com:8006/api2/json)."
  type        = string
}

variable "proxmox_api_token_id" {
  description = "Proxmox API token ID used to bootstrap the provider (USER@REALM!TOKEN). Must be supplied via the PROXMOX_API_TOKEN environment variable."
  type        = string
  sensitive   = true
}

variable "proxmox_api_token_secret" {
  description = "Proxmox API token secret used to bootstrap the provider. Must be supplied via the PROXMOX_API_TOKEN environment variable."
  type        = string
  sensitive   = true
}

variable "proxmox_endpoint" {
  description = "Human-friendly Proxmox node endpoint (e.g. pve.example.com). Stored alongside the token for operator reference."
  type        = string
}

variable "proxmox_user_id" {
  description = "Proxmox user identifier minted for Terraform-driven cluster provisioning."
  type        = string
  default     = "k3s-terraform@pam"
}

variable "proxmox_token_name" {
  description = "Token name attached to the Proxmox user. Combined with user_id it forms the Proxmox API token ID."
  type        = string
  default     = "tf"
}

variable "proxmox_role_id" {
  description = "Proxmox role identifier granting least-privilege cluster provisioning permissions."
  type        = string
  default     = "k3s-cluster"
}

variable "proxmox_role_privileges" {
  description = "Least-privilege privilege set for the k3s-cluster role. Sourced from research-log-v7 to satisfy NFR-007."
  type        = set(string)
  default = [
    "VM.Allocate",
    "VM.Audit",
    "VM.Clone",
    "VM.Config.CDROM",
    "VM.Config.CPU",
    "VM.Config.Cloudinit",
    "VM.Config.Disk",
    "VM.Config.Memory",
    "VM.Config.Network",
    "VM.Config.Options",
    "VM.GuestAgent.Audit",
    "VM.PowerMgmt",
    "VM.Snapshot",
    "VM.Snapshot.Rollback",
    "Datastore.Allocate",
    "Datastore.AllocateSpace",
    "Datastore.Audit",
    "Pool.Allocate",
    "Pool.Audit",
    "Sys.Audit",
    "Sys.Modify",
    "SDN.Use",
  ]
}