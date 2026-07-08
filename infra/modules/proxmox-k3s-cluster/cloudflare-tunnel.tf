###############################################################################
# STRRL/cloudflare-tunnel-ingress-controller Helm release.
#
# Gated on var.cf_publish_traefik_publicly=true. Default off — the demoted
# Traefik ClusterIP path is the production default.
#
# When enabled, the chart:
#   1. Authenticates to Cloudflare using var.cf_api_token (from infra/tokens/output.json).
#   2. Creates a tunnel named var.cf_tunnel_name in the Cloudflare account.
#   3. Watches Ingress objects with ingressClass.name == var.cf_ingress_class
#      and tunnels them to the in-cluster backend Service.
#
# Spec NFR-007: scoped Cloudflare token only (Zone Read + Zone DNS Write +
# Cloudflare Tunnel:Edit). The cf_api_token we receive from SS0's output.json
# already complies.
###############################################################################

resource "helm_release" "cf_tunnel_controller" {
  count = var.cf_publish_traefik_publicly ? 1 : 0

  name             = "cloudflare-tunnel-ingress-controller"
  namespace        = "cloudflare-tunnel-ingress-controller"
  create_namespace = true
  repository       = "oci://ghcr.io/strrl/charts"
  chart            = "cloudflare-tunnel-ingress-controller"
  version          = "0.0.23"

  values = [
    jsonencode({
      cloudflare = {
        apiToken   = var.cf_api_token
        accountId  = var.cf_account_id
        tunnelName = var.cf_tunnel_name
      }
      ingressClass = {
        name      = var.cf_ingress_class
        controller = "dev.strrl.cloudflaretunnelingresscontroller/ingress"
        enabled   = true
      }
    }),
  ]

  # 2026-07-08: local_sensitive_file.talos_machineconfig dependency
  # removed alongside the Talos machineconfig renderer.
}