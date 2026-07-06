###############################################################################
# Packer template for baking Talos Linux into a Proxmox template (SS1).
#
# Builder: hashicorp/proxmox v1.2.3+ proxmox-iso. We boot the Talos ISO,
# wait for the boot loader to time out (Talos is installer-mode by default —
# it boots, prints the dashboard, and waits for an operator), then halt the
# VM and convert it to a Proxmox template.
#
# For FIRST-TIME builds (no template exists yet) the operator must:
#   1. Upload the Talos ISO to the proxmox host's local storage (e.g. via
#      `scp` or the PVE UI).
#   2. Pre-create a base Talos VM named `talos-base` at VMID 999, configured
#      with 2GB RAM, 10GB disk, and the Talos ISO attached.
# Subsequent builds can clone that base VM and only need to refresh the
# Talos image (boot_command exits the dashboard loop with `reboot`).
#
# Cross-reference: this template is invoked by tools/build_image.py which
# passes the four PVE vars via -var flags. The VMID here is fixed at 900
# per WP spec (TEMPLATE_VMID constant in build_image.py).
###############################################################################

packer {
  required_plugins {
    proxmox = {
      version = ">= 1.2.3"
      source  = "github.com/hashicorp/proxmox"
    }
  }
}

# ---------------------------------------------------------------------------
# Variables — passed by `tools/build_image.py` via -var flags.
# ---------------------------------------------------------------------------

variable "talos_version" {
  type        = string
  description = "Talos Linux release tag (e.g. v1.10.0). Validated against versions.yaml by the wrapper."
}

variable "pve_endpoint" {
  type        = string
  description = "Proxmox VE API URL (e.g. https://proxmox-host:8006/api2/json)."
}

variable "pve_node" {
  type        = string
  description = "Proxmox node name (default: proxmox-host)."
  default     = "proxmox-host"
}

variable "pve_token_id" {
  type        = string
  description = "Proxmox API token id (USER@REALM!TOK)."
  sensitive   = true
}

variable "pve_token_secret" {
  type        = string
  description = "Proxmox API token secret. Never logged."
  sensitive   = true
}

# ---------------------------------------------------------------------------
# Source: clone-from-base-VM. The base VM (VMID 999) is set up once by the
# operator and pinned to the Talos ISO. We boot it, halt, and convert to
# template.
# ---------------------------------------------------------------------------

source "proxmox-clone" "talos" {
  proxmox_url              = var.pve_endpoint
  # hashicorp/proxmox v1.2.x token auth format:
  #   username = "user@realm!tokenid"   (with the trailing !tokenid)
  #   token    = "<bare-secret-uuid>"  (NOT "user@realm!tokenid=secret")
  username                 = var.pve_token_id
  token                    = var.pve_token_secret
  node                     = var.pve_node
  vm_id                    = "900"
  vm_name                  = "talos-template"
  template_name            = "talos-${var.talos_version}"
  template_description     = "Talos ${var.talos_version} template"
  insecure_skip_tls_verify = true

  # Clone from the base Talos VM (operator-created once with the ISO attached).
  # In hashicorp/proxmox v1.2.3 the argument is `clone_vm_id`, NOT
  # `clone_from_vm_id`. The base VM at VMID 999 must already exist with the
  # Talos ISO on ide2 (Step 1 pre-flight).
  clone_vm_id = "999"

  # Talos headless: no SSH needed after build (cluster uses talosctl over API).
  # We keep ssh_username/password in case the operator enables `talosctl
  # gen config` debug mode.
  ssh_username     = "talos"
  ssh_password     = "talos"
  ssh_wait_timeout = "0s"

  # VM sizing: matches what WP02 expects for controlplane nodes.
  cores  = 4
  memory = 4096

  # EFI boot — Talos Linux only boots UEFI on modern PVE.
  bios    = "ovmf"
  machine = "q35"
  efi_config {
    # Default upstream template hardcodes `local-lvm` (the storage
    # pool the Proxmox installer creates by default). BigBertha's
    # installer was configured with two separate lvmthin pools
    # (`data1`, `data2`) and no `local-lvm`, so retarget both the EFI
    # and the main disk to `data1` -- same pool used by the
    # operator-prepared `talos-base` VMID 999 in Step 1 pre-flight.
    efi_storage_pool  = "data1"
    efi_type          = "4m"
    pre_enrolled_keys = true
  }

  scsi_controller = "virtio-scsi-single"

  disks {
    type         = "scsi"
    storage_pool = "data1"
    disk_size    = "20G"
    format       = "raw"
    io_thread    = true
    discard      = true
    ssd          = true
  }

  network_adapters {
    model    = "virtio"
    bridge   = "vmbr0"
    firewall = true
  }

  qemu_agent = true
  os         = "l26"

  # If the Talos base VM was set up with the ISO attached, Packer simply
  # needs to halt it after boot. We do this via the shell provisioner below.
  boot_wait = "30s"
}

# ---------------------------------------------------------------------------
# Build: halt the VM (Talos is already installed) and convert to template.
# ---------------------------------------------------------------------------

build {
  name    = "talos-${var.talos_version}"
  sources = ["source.proxmox-clone.talos"]

  # The hashicorp/proxmox v1.2.x proxmox-clone builder halts the cloned
  # VM automatically (after `boot_wait`) and converts it to a PVE
  # template because `template_name` is set. No SSH provisioner is
  # needed; Talos is headless and exposes no SSH (cluster control
  # happens via the Talos API, not SSH). The base VM 999 must
  # therefore already have Talos fully installed on disk before
  # this build runs -- see the Step 1 pre-flight for the operator
  # procedure.
}