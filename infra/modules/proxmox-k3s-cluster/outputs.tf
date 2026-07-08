###############################################################################
# Module outputs + output.json writer.
#
# SS2 -> SS3 contract: this file at clusters/<cluster_name>/output.json is the
# canonical handoff that SS3 (bootstrap_cluster.py) consumes. Schema:
#
#   {
#     "cluster_name":         "cicd",
#     "vnet_bridge":          "vnet0",
#     "control_plane_count":  1,
#     "worker_count":         1,
#     "pod_cidr":             "172.16.0.0/16",
#     "svc_cidr":             "172.17.0.0/16",
#     "cluster_dns":          "172.17.0.10",
#     "nodes":                [{"role": "control_plane", "name": ..., "vmid": ..., "ip": ""}],
#     "helm_releases":        ["cilium", "kube-vip", ...]
#   }
#
# 2026-07-08 schema changes:
#   - vip + talos_dir removed: VIP is a kube-vip service concept the
#     bootstrap can derive (or hasn't been needed since the
#     Ubuntu+k3s pivot), and Talos has been replaced with Ubuntu+k3s.
#   - nodes[].ip removed from this file: Proxmox SDN auto-allocates
#     IPs from the DHCP pool (10.0.0.50-200 on this host) and the
#     bootstrap discovers them via qemu-guest-agent at runtime.
#     scripts/sync_dns_to_sdn.py is the canonical source of truth
#     for post-apply IP wiring.
#   - nodes[].mac and nodes[].talos_hostname removed (Talos-specific).
#
# file_permission = "0600" — the output contains the cluster identity +
# topology invariants, which are not world-readable secrets but should
# not be readable to other operator accounts on the same workstation.
###############################################################################

output "cluster_name" {
  description = "Globally-unique Cluster name; consumed by SS3 for naming context."
  value       = var.cluster_name
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

output "nodes" {
  description = "Resolved node map (role, name, vmid, ip). The ip field is empty here -- discovered post-apply from Proxmox SDN via qemu-guest-agent."
  value       = local.nodes
}

output "pod_cidr" {
  description = "Pod CIDR for the k3s cluster (input)."
  value       = var.pod_cidr
}

output "svc_cidr" {
  description = "Service CIDR for the k3s cluster (input)."
  value       = var.svc_cidr
}

output "cluster_dns" {
  description = "In-cluster coredns service IP (input)."
  value       = var.cluster_dns
}

output "helm_releases" {
  description = "Helm releases SS3 will install in order. Listed here so SS3 does not need to know which chart versions the module pinned."
  value = [
    # WP08 (2026-07-08): kube-vip removed. The cluster runs
    # single-control-plane, so the apiserver endpoint is the CP
    # host IP directly.
    "cilium",
    "proxmox-cloud-controller-manager",
    "proxmox-csi-plugin",
    "cert-manager",
    "cloudflare-tunnel-ingress-controller",
    # WP07: Envoy Gateway (the GatewayClass=envoy implementation).
    "envoy-gateway",
    # Traefik itself is NOT installed by SS3; it ships with k3s
    # and is configured via the HelmChartConfig that this module
    # renders into infra/clusters/<name>/manifests/.
    "traefik-helmchartconfig",
  ]
}

resource "local_sensitive_file" "cluster_output" {
  # Module lives at infra/modules/proxmox-k3s-cluster/; cluster root
  # at infra/clusters/<name>/. So `../../clusters/<name>/output.json`
  # is two levels up (== `infra/`), then into clusters/<name>/.
  filename        = "${path.module}/../../clusters/${var.cluster_name}/output.json"
  file_permission = "0600"

  content = jsonencode({
    cluster_name        = var.cluster_name
    vnet_bridge         = var.vnet_bridge
    control_plane_count = var.control_plane.count
    worker_count        = var.workers.count
    pod_cidr            = var.pod_cidr
    svc_cidr            = var.svc_cidr
    cluster_dns         = var.cluster_dns
    nodes               = local.nodes
    helm_releases       = [
      # WP08: kube-vip removed. The cluster runs single-CP;
      # the apiserver endpoint is the CP host IP directly.
      "cilium",
      "proxmox-cloud-controller-manager",
      "proxmox-csi-plugin",
      "cert-manager",
      "cloudflare-tunnel-ingress-controller",
      # WP07: Envoy Gateway (GatewayClass=envoy).
      "envoy-gateway",
      # Traefik is configured via the HelmChartConfig that this
      # module renders into infra/clusters/<name>/manifests/.
      # It is NOT a separate helm release SS3 installs.
      "traefik-helmchartconfig",
    ]
  })
}