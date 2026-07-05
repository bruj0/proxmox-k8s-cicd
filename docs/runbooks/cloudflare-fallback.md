# Runbook: Cloudflare Tunnel fallback

Use this runbook when the Cloudflare Tunnel controller has failed and the
operator needs to expose the cluster's Traefik ingress directly on the PVE
host's public IP. **This adds host ports; do not run unless Cloudflare is
genuinely unavailable for an extended period.**

## Prerequisites

- BigBertha reachable at `root@10.0.0.1` on port 6022 (the non-default
  PVE ssh port).
- The cluster module is already applied (`tofu state list` shows
  `module.proxmox_k3s_cluster.cicd`).
- The cluster is currently serving Cloudflare-mediated traffic.

## Procedure

1. Flip the cluster variable:

   ```bash
   cd clusters/cicd
   tofu apply -var="cf_publish_traefik_publicly=true"
   ```

   Tofu automatically re-renders the Traefik HelmChartConfig to expose
   ports 80/443 on the cluster VIP (`10.0.0.30`).

2. Add the DNAT rules on BigBertha:

   ```bash
   ssh root@10.0.0.1 -p 6022 nft add rule ip nat prerouting tcp dport 443 dnat to 10.0.0.30:443
   ssh root@10.0.0.1 -p 6022 nft add rule ip nat prerouting tcp dport 80  dnat to 10.0.0.30:80
   ```

3. Update Cloudflare DNS records:

   - Change the `*.example.com` CNAMEs (which the
     cloudflare-tunnel-ingress-controller manages) to A records
     pointing at `151.80.34.63` (BigBertha's public IP).
   - Do this via the Cloudflare dashboard or via the
     cloudflare-tunnel-ingress-controller's pause-and-override flow.

4. Update PowerDNS so internal clients bypass the tunnel:

   ```bash
   ssh root@10.0.0.3 pdnsutil set-record intranet example.com A 151.80.34.63
   ```

   (PowerDNS is the cluster's upstream nameserver; per FR-034 internal
   clients use 10.0.0.3.)

## Verify

```bash
curl -sf http://gitlab.intranet/-/health
```

The endpoint must return 200 within 5 s.

## Rollback

Reverse the four steps:

1. `tofu apply -var="cf_publish_traefik_publicly=false"` in
   `clusters/cicd/`.
2. Delete the nft DNAT rules:

   ```bash
   ssh root@10.0.0.1 -p 6022 nft delete rule ip nat prerouting handle <handle>
   ```

   (Find the handle via `nft -a list chain ip nat prerouting | grep dnat`.)

3. Restore the CNAMEs in Cloudflare (or restart the
   cloudflare-tunnel-ingress-controller so it rewrites them).
4. Restore the A record in PowerDNS back to whatever it was.

## Misfit notes

This runbook intentionally violates the M2 misfit (no new host ports)
when active. The host_ports phase of the bootstrap will FAIL until the
baseline is re-captured. After the rollback, run:

```bash
PVE_SSH=root@10.0.0.1 PVE_SSH_PORT=6022 \
  ./scripts/capture_host_ports_baseline.sh clusters/cicd
```

to re-baseline and bring the verifier back to green.