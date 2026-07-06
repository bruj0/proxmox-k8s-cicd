---
context_name: "Cluster Provisioning Module"
version: "1.1"
subsystem: "modules/proxmox-k3s-cluster"
created: "2026-07-05T20:55:00Z"
updated: "2026-07-06T14:25:00Z"
---

# Cluster Provisioning Module

The reusable OpenTofu module `modules/proxmox-k3s-cluster` plus its first root
instance at `clusters/cicd/`. The module owns cluster identity (name uniqueness),
the vnet0 dnsmasq ethers reservation for the VIP, the per-VM Talos machineconfig
render, the demoted Traefik HelmChartConfig, and the optional STRRL
/cloudflare-tunnel-ingress-controller Helm release. It also enforces the
no-host-ports invariant for Traefik unless an operator explicitly flips the
fallback flag.

## Language

**Cluster**:
A user-named k3s cluster topology. Uniquely identified by `cluster_name` (used
in Talos cert prefix, dnsmasq hostnames, output.json). Multiple Cluster
instances may coexist in the same Proxmox host provided their VMID and IP
ranges do not overlap.
_Avoid_: `k3s_cluster`, `talos_cluster` (ambiguous — Cluster is both).
_Subsystems_: SS2
_Files_: `modules/proxmox-k3s-cluster/main.tf`, `modules/proxmox-k3s-cluster/variables.tf`
_Relates to_: TalosNode (1..N per Cluster), Cluster Output (one per Cluster)

**TalosNode**:
One Proxmox VM in a Cluster. Identified by `role` (`control_plane` | `worker`),
`vmid`, `ip`, `mac`, and `talos_hostname`. Rendered to a Talos machineconfig
file at `clusters/<cluster_name>/talos/<talos_hostname>.yaml`.
_Avoid_: `node`, `k3s_node` (too generic).
_Subsystems_: SS2, SS3 (consumed via the Talos config files)
_Files_: `modules/proxmox-k3s-cluster/main.tf`, `modules/proxmox-k3s-cluster/talos.tf`

**Cluster VIP**:
A single virtual IP within the vnet0 IP range that kube-vip binds to the
active control-plane node. Reserved in the vnet0 dnsmasq ethers file BEFORE any
VM clone is started. Distinct from the per-node IPs.
_Avoid_: `k8s_vip`, `control_plane_vip`, `loadbalancer_ip` (overloaded).
_Subsystems_: SS2 (owned), SS3 (consumed via output.json)
_Files_: `modules/proxmox-k3s-cluster/dnsmasq.tf`, `modules/proxmox-k3s-cluster/outputs.tf`

**Demoted Traefik**:
The k3s-bundled Traefik ingress controller, configured by
`traefik-chartconfig.yaml.tftpl` with `service.type=ClusterIP` and
`ingressClass.name=traefik-internal` (default). HostPorts are NOT exposed by
default — they are an explicit operator opt-in (`cf_publish_traefik_publicly=true`)
that should remain off in production per NFR-007.
_Avoid_: `traefik_internal`, `internal_lb` (overloaded).
_Subsystems_: SS2
_Files_: `modules/proxmox-k3s-cluster/traefik-chartconfig.yaml.tftpl`
_Relates to_: Cloudflare Tunnel Fallback (mutually exclusive)

**Cloudflare Tunnel Fallback**:
The STRRL/cloudflare-tunnel-ingress-controller Helm release, gated on
`var.cf_publish_traefik_publicly=true`. When enabled, terminates external HTTP at
Cloudflare's edge and forwards to the cluster. Off by default — the demoted
Traefik is the production path.
_Avoid_: `cf_ingress`, `tunnel_ingress` (loses the operator fallback semantics).
_Subsystems_: SS2 (owned), SS3 (Helm release status)
_Files_: `modules/proxmox-k3s-cluster/cloudflare-tunnel.tf`

**Image Template Reference**:
The Proxmox VMID of the Talos image template baked by SS1, propagated as a
plain-text file at `build/image-id.txt`. Consumed by SS2 via the `image_id`
input variable. Empty or whitespace = plan fails closed.
_Avoid_: `talos_image_vmid`, `template_vmid` (loses the cross-subsystem contract).
_Subsystems_: SS1 (producer), SS2 (consumer), SS3 (consumer via output.json)
_Files_: `modules/proxmox-k3s-cluster/variables.tf`, `clusters/cicd/main.tf`
_History_:
- 2026-07-05 (001-build-a-kubernetes-k3s-cluster-on-proxmo): initial definition (value `900`).
- 2026-07-06: live-host baking on BigBertha (PVE 9.2.3) verified the contract end-to-end. Phase 1 produced a `talos-template` VM at VMID 900 with `template: 1` in `/etc/pve/qemu-server/900.conf`; `build/image-id.txt = "900\n"`. The contract now hinges on two upstream fixes recorded in the SS1 Agent Skill (`.agents/skills/proxmox-k3s-pipeline/SKILL.md` Step 1b): (i) the `k3s-cluster` PVE role carries the 19 privs SS1 needs (was 12 per spec T005), and (ii) the `output.json` `proxmox_token_secret` field is the bare UUID, not the full token string. See also `docs/runbooks/rotate-tokens.md` for how rotation preserves this contract.

**Cluster Output File**:
A JSON file written by the module at `clusters/<cluster_name>/output.json`
(file_permission = "0600") containing the resolved Cluster topology — nodes,
VIP, vnet_bridge, talos_dir, helm_releases. Consumed by SS3.
_Avoid_: `cluster_state`, `tf_output` (loses the schema).
_Subsystems_: SS2 (producer), SS3 (consumer)
_Files_: `modules/proxmox-k3s-cluster/outputs.tf`