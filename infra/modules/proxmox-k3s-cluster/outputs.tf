###############################################################################
# Module outputs.
#
# SS2 -> SS3 contract: infra/clusters/<cluster_name>/output.json is the
# canonical handoff that SS3 (tools/bootstrap_cluster.py) consumes. It is
# written by the bootstrap dispatcher, NOT by tofu. Tofu had a
# `local_sensitive_file.cluster_output` resource here historically (it
# would emit a snapshot on every apply), but the snapshot went stale the
# moment SDN auto-allocated IPs and the bootstrap discovered the live
# values via qemu-guest-agent. Two writers with different refresh cadences
# is a recipe for drift. The bootstrap is now the sole writer of
# output.json; this file just declares the canonical `output` blocks so
# other operators can `tofu output -json` for documentation / debugging.
#
# 2026-07-09 schema changes (move writer from tofu -> bootstrap):
#   - `local_sensitive_file.cluster_output` REMOVED from this file.
#   - The helm_releases list is no longer emitted; it is a static
#     declaration that lives in bootstrap_cluster.py (see
#     `tools/lib/helm_client.py::gateway_releases` and the bootstrap
#     helm install path).
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
  description = "Helm releases SS3 will install in order. The bootstrap reads its own copy (tools/lib/helm_client.py) instead of consuming this output, but we keep it for `tofu output -json` tooling."
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