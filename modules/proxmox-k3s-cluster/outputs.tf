###############################################################################
# Module outputs + output.json writer.
#
# SS2 -> SS3 contract: this file at clusters/<cluster_name>/output.json is the
# canonical handoff that SS3 (bootstrap_cluster.py) consumes. Schema:
#
#   {
#     "cluster_name":         "cicd",
#     "vip":                  "10.0.0.30",
#     "vnet_bridge":          "vnet0",
#     "control_plane_count":  1,
#     "worker_count":         1,
#     "talos_dir":            "<module>/clusters/<cluster_name>/talos",
#     "nodes":                [{"role": "control_plane", "name": ..., "vmid": ..., ...}],
#     "helm_releases":        ["cilium", "kube-vip", ...]
#   }
#
# file_permission = "0600" — the output contains node IPs and the cluster
# identity, which are not world-readable secrets but should not be readable
# to other operator accounts on the same workstation.
###############################################################################

output "cluster_name" {
  description = "Globally-unique Cluster name; consumed by SS3 for Talos cert prefix."
  value       = var.cluster_name
}

output "vip" {
  description = "Cluster VIP; consumed by SS3 for kubeconfig and talosctl endpoint."
  value       = var.vip
}

output "vnet_bridge" {
  description = "Proxmox SDN bridge the cluster attaches to."
  value       = var.vnet_bridge
}

output "control_plane_count" {
  description = "Number of control-plane nodes."
  value       = var.control_plane.count
}

output "worker_count" {
  description = "Number of worker nodes."
  value       = var.workers.count
}

output "talos_dir" {
  description = "Directory containing per-VM Talos machineconfig YAML files."
  value       = "${path.module}/../clusters/${var.cluster_name}/talos"
}

output "nodes" {
  description = "Resolved node map (role, name, vmid, ip, mac, talos_hostname)."
  value       = local.nodes
}

output "helm_releases" {
  description = "Helm releases SS3 will install in order. Listed here so SS3 does not need to know which chart versions the module pinned."
  value = [
    "cilium",
    "kube-vip",
    "proxmox-cloud-controller-manager",
    "proxmox-csi-plugin",
    "traefik",
    "cloudflare-tunnel-ingress-controller",
    "cert-manager",
  ]
}

resource "local_sensitive_file" "cluster_output" {
  filename        = "${path.module}/../clusters/${var.cluster_name}/output.json"
  file_permission = "0600"

  content = jsonencode({
    cluster_name        = var.cluster_name
    vip                 = var.vip
    vnet_bridge         = var.vnet_bridge
    control_plane_count = var.control_plane.count
    worker_count        = var.workers.count
    talos_dir           = "${path.module}/../clusters/${var.cluster_name}/talos"
    nodes               = local.nodes
    helm_releases       = [
      "cilium",
      "kube-vip",
      "proxmox-cloud-controller-manager",
      "proxmox-csi-plugin",
      "traefik",
      "cloudflare-tunnel-ingress-controller",
      "cert-manager",
    ]
  })
}