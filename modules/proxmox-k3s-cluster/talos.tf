###############################################################################
# Talos machineconfig renderer.
#
# For each VM, renders clusters/<cluster_name>/talos/<hostname>.yaml from
# templates/talos-machineconfig.yaml.tftpl. The file_permission = "0600"
# mirrors the SS0 output.json contract: Talos configs contain control plane
# secrets (certs, kubelet bootstrap tokens) that must not be world-readable.
#
# The downstream SS3 (bootstrap_cluster.py) reads these files and applies
# them via `talosctl apply-config`.
###############################################################################

resource "local_sensitive_file" "talos_machineconfig" {
  for_each = { for n in local.nodes : n.name => n }

  filename        = "${path.module}/../clusters/${var.cluster_name}/talos/${each.value.talos_hostname}.yaml"
  file_permission = "0600"

  content = templatefile("${path.module}/templates/talos-machineconfig.yaml.tftpl", {
    cluster_name = var.cluster_name
    hostname     = each.value.talos_hostname
    ip           = each.value.ip
    vip          = var.vip
    role         = each.value.role
    pod_cidr     = var.pod_cidr
    svc_cidr     = var.svc_cidr
    talos_version = var.talos_version
  })
}