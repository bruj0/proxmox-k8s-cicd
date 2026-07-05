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
  filename        = "${path.module}/../clusters/${var.cluster_name}/traefik-helmchartconfig.yaml"
  file_permission = "0644"

  content = templatefile("${path.module}/traefik-chartconfig.yaml.tftpl", {
    traefik_service_type  = local.traefik_service_type
    traefik_ingress_class = local.traefik_ingress_class
    traefik_expose        = local.traefik_expose
    traefik_exposed_port  = local.traefik_exposed_port
  })

  depends_on = [
    local_sensitive_file.talos_machineconfig,
  ]
}