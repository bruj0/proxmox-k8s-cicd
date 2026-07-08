###############################################################################
# Traefik HelmChartConfig renderer.
#
# M2 (no host ports) verification:
#   - Default  -> service.type=ClusterIP, ingressClass.name=traefik-internal
#                 ports.web/websecure.expose=false
#   - Fallback -> service.type=ClusterIP, ingressClass.name=traefik-internal
#                 ports.web/websecure.expose=true, exposedPort=80/443 (hostPorts)
#
# The fallback path is enabled only when var.cf_publish_traefik_publicly=true.
# It is NOT a public-by-default configuration; it is the operator's explicit
# opt-in for clusters that don't want Cloudflare Tunnel.
###############################################################################

locals {
  traefik_service_type = "ClusterIP"
  traefik_ingress_class = "traefik-internal"

  # hostPorts opt-in path.
  traefik_expose      = var.cf_publish_traefik_publicly
  traefik_exposed_port = var.cf_publish_traefik_publicly ? 80 : 0
  traefik_exposed_port_https = var.cf_publish_traefik_publicly ? 443 : 0
}

resource "local_file" "traefik_chartconfig" {
  # Two `..` because module is at infra/modules/proxmox-k3s-cluster/;
  # cluster root at infra/clusters/<name>/. The traefik config lives
  # under manifests/ (skill/plan contract) so bootstrap_cluster.py
  # can find it via tools/lib/helm_client.py:208.
  filename        = "${path.module}/../../clusters/${var.cluster_name}/manifests/traefik-helmchartconfig.yaml"
  file_permission = "0644"

  content = templatefile("${path.module}/traefik-chartconfig.yaml.tftpl", {
    traefik_service_type  = local.traefik_service_type
    traefik_ingress_class = local.traefik_ingress_class
    traefik_expose        = local.traefik_expose
    traefik_exposed_port  = local.traefik_exposed_port
  })

  # 2026-07-08: the local_sensitive_file.talos_machineconfig dependency
  # was removed alongside the Talos machineconfig renderer. The Traefik
  # HelmChartConfig is independent of any per-VM first-boot setup.
}