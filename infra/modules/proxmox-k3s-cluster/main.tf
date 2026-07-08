###############################################################################
# Module core: locals, validation preconditions, and VM cloning.
#
# Identity invariant (M3): every cluster module invocation produces a
# deterministic set of (name, role, vmid) tuples from cluster_name +
# vmid_start. Per-node IPs are NOT computed here -- Proxmox SDN
# auto-allocates them from the DHCP pool (10.0.0.50-200 on this host)
# and the bootstrap reads them via qemu-guest-agent at runtime. See
# scripts/sync_dns_to_sdn.py for the DNS-side mirror.
#
# Misfits verified by tests/main.tftest.hcl:
#   M2 (no host ports)   default Traefik HelmChartConfig uses ClusterIP;
#                        cf_publish_traefik_publicly=true opt-in is gated.
#   M3 (cluster identity) cluster_name is exposed as an output so the consuming
#                        root can enforce uniqueness across all Clusters.
#
# Spec acceptance:
#   FR-030  control_plane.count must be 1 or 3.
#   vmid    vmid_start..vmid_start+total-1 must not overlap any populated VM.
#   image   image_id must be non-empty.
#
# 2026-07-08: the VIP (kube-vip Service IP) and per-node IP CIDR
# (var.ip_start, used by cidrhost() to fabricate IPs) are removed.
# IPs are owned by Proxmox SDN, not tofu; the bootstrap (SS3) discovers
# them post-apply via qemu-guest-agent. See CONTEXT.md for the
# bounded-context glossary.
###############################################################################

locals {
  total_nodes = var.control_plane.count + var.workers.count
  vmid_end    = var.vmid_start + local.total_nodes - 1

  # The `ip` field on each node is left blank here; it is populated
  # post-apply by scripts/sync_dns_to_sdn.py (which already mirrors the
  # SDN DHCP-allocated addresses to PowerDNS). Until that helper is
  # extended to also patch this file, downstream consumers should treat
  # `nodes[].ip` as "to be discovered at bootstrap time".
  nodes = concat(
    [for i in range(var.control_plane.count) : {
      role = "control_plane"
      name = "${var.cluster_name}-cp-${i + 1}"
      vmid = var.vmid_start + i
      ip   = ""
    }],
    [for i in range(var.workers.count) : {
      role = "worker"
      name = "${var.cluster_name}-w-${i + 1}"
      vmid = var.vmid_start + var.control_plane.count + i
      ip   = ""
    }],
  )
}

# ---------------------------------------------------------------------------
# Validation: FR-030 control_plane.count must be 1 or 3.
# ---------------------------------------------------------------------------

resource "terraform_data" "validate_control_plane_count" {
  input = {
    count = var.control_plane.count
  }

  lifecycle {
    precondition {
      condition     = contains([1, 3], var.control_plane.count)
      error_message = "control_plane.count must be 1 or 3 (2-node etcd is invalid); this spec is single-host, single-control-plane by design."
    }
  }
}

# ---------------------------------------------------------------------------
# Validation: image_id must be non-empty.
# ---------------------------------------------------------------------------

resource "terraform_data" "validate_image_id" {
  input = {
    image_id = var.image_id
  }

  lifecycle {
    precondition {
      condition     = length(trimspace(var.image_id)) > 0
      error_message = "image_id is empty; run tools/build_image.py first to bake the Ubuntu+k3s template."
    }
  }
}

# ---------------------------------------------------------------------------
# Validation: VMID range must not overlap existing VMs on the target node.
# ---------------------------------------------------------------------------

data "proxmox_virtual_environment_vms" "existing" {
  depends_on = [terraform_data.validate_control_plane_count]
}

locals {
  existing_vmids = toset([
    for v in data.proxmox_virtual_environment_vms.existing.vms :
    tonumber(v.vm_id) if !v.template
  ])

  requested_vmids = toset([for n in local.nodes : n.vmid])

  overlap = setintersection(local.existing_vmids, local.requested_vmids)
}

resource "terraform_data" "vmid_overlap" {
  input = {
    overlap = local.overlap
  }

  lifecycle {
    precondition {
      condition     = length(local.overlap) == 0
      error_message = "vmid_start=${var.vmid_start} collides with existing VMIDs: ${join(",", [for v in local.overlap : tostring(v)])}. Choose a vmid_start that does not overlap any populated VM."
    }
  }
}

# ---------------------------------------------------------------------------
# Clone the VMs from the Ubuntu+k3s image template.
# ---------------------------------------------------------------------------

resource "proxmox_cloned_vm" "node" {
  for_each = { for n in local.nodes : n.name => n }

  name        = each.value.name
  node_name   = var.pve_node
  description = "${var.cluster_name} ${each.value.role} (Ubuntu+k3s)"
  started     = true

  clone = {
    source_vm_id    = tonumber(var.image_id)
    full            = true
    # Live-host fix 2026-07-06: explicitly pin the clone target
    # datastore to data1 (BigBertha has no local-lvm lvmthin pool).
    # Without this, the bpg/proxmox provider plan-vs-apply diff
    # blows up with 'inconsistent result: datastore_id was
    # local-lvm but now data1' -- it stored the configured value
    # in plan but the clone copy defaults to wherever the
    # source-VM disk happens to live.
    target_datastore = var.disk_storage_pool
  }

  cpu = {
    cores = each.value.role == "control_plane" ? var.control_plane.cpu : var.workers.cpu
    type  = "host"
  }

  memory = {
    # Live-host fix 2026-07-06: bpg/proxmox 0.111.x `proxmox_cloned_vm.memory`
    # exposes `size` (total RAM) and `balloon` (min guaranteed). `balloon = 0`
    # disables the balloon driver so size is a hard RAM cap, matching the
    # cluster-root ram_mb semantics.
    size    = each.value.role == "control_plane" ? var.control_plane.ram_mb : var.workers.ram_mb
    balloon = 0
  }

  disk = {
    scsi0 = {
      datastore_id = var.disk_storage_pool
      size_gb      = each.value.role == "control_plane" ? var.control_plane.disk_gb : var.workers.disk_gb
    }
  }

  network = {
    net0 = {
      bridge = var.vnet_bridge
      model  = "virtio"
    }
  }

  tags = [
    var.cluster_name,
    each.value.role,
    "ubuntu-k3s",
  ]

  depends_on = [
    terraform_data.validate_image_id,
    terraform_data.vmid_overlap,
  ]
}