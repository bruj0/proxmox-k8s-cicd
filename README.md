# proxmox-k8s-cicd

End-to-end pipeline that provisions **two k3s clusters** (`cicd` and
`apps`) on a **single Proxmox VE host**, with **public HTTPS via
Cloudflare Tunnel** (no host open ports) and **apps -> cicd
cross-cluster Service consumption via ExternalName**.

The pipeline is driven by a single
[agentskills.io](https://agentskills.io)-format skill at
[`.agents/skills/proxmox-k3s-pipeline/SKILL.md`](.agents/skills/proxmox-k3s-pipeline/SKILL.md)
that walks an Operator (human or AI agent) through five top-level phases
end-to-end. The bootstrap phase further decomposes into six sub-phases.

## What you get after running the skill

After a successful end-to-end run, your Proxmox host has:

- **1 Proxmox template** (VMID 900, `ubuntu-noble-template`) containing
  Ubuntu 24.04 LTS Noble + `qemu-guest-agent` + `openssh-server` +
  `cloud-init`, ready to be cloned.
- **4 cloned k3s nodes** (2 per cluster) — 1 control-plane + 1 worker
  each, with the operator's SSH key authorized and a DHCP-allocated
  SDN IP:
  - `cicd-cp-1`, `cicd-w-1` (vip `10.0.0.30`, pod_cidr `10.42.0.0/16`)
  - `apps-cp-1`,  `apps-w-1`  (vip `10.0.0.40`, pod_cidr `10.44.0.0/16`)
- **A k3s cluster on each side** (CNI: Cilium, VIP: kube-vip) plus
  the standard `proxmox-ccm` + `proxmox-csi` + `traefik` + `cert-manager`
  + `cloudflare-tunnel-ingress-controller` Helm releases.
- **Public HTTPS** for the cicd cluster via Cloudflare Tunnel — no
  inbound port opened on the Proxmox host.
- **Cross-cluster Service consumption** from `apps` -> `cicd` via
  ExternalName Services, so apps workloads can reach
  `gitlab.cicd-system.svc.cluster.local` (which resolves to the
  cicd VIP via PowerDNS).
- **PowerDNS A + PTR records** aligned with the IPs the SDN actually
  assigned (see [`scripts/sync_dns_to_sdn.py`](scripts/sync_dns_to_sdn.py)).

## Repository layout

```
.
├── .agents/skills/proxmox-k3s-pipeline/   # the canonical Agent Skill
│   ├── SKILL.md                            # the playbook (loaded by Claude Code / Cursor)
│   ├── versions.lock.yaml                  # compatibility matrix + cross_check
│   └── CONTEXT.md                          # bounded-context vocabulary
├── docs/
│   ├── architecture.md                     # end-to-end architecture + Mermaid
│   ├── cluster-instances.md                # how to add a 3rd/4th/... cluster
│   ├── verification.md                     # SC-001..SC-007 + NFR-010..NFR-014
│   ├── proxmox-serial-capture.md           # debug-only: serial console recipe
│   └── runbooks/                           # copy-pasteable operator procedures
├── infra/
│   ├── tokens/                             # Phase 0: mint Proxmox + CF scoped tokens
│   ├── modules/proxmox-k3s-cluster/        # Phase 2: clone template + SDN + DNS
│   └── clusters/{cicd,apps}/               # Phase 2: per-cluster root modules
├── scripts/
│   ├── apply_tofu.py                       # one entry-point for `tofu apply`
│   ├── sync_dns_to_sdn.py                  # post-apply PowerDNS A/PTR fix-up
│   ├── capture_serial.py                   # debug-only: VM serial console capture
│   ├── capture_host_ports_baseline.sh      # Phase 3: snapshot host-ports baseline
│   └── gitlab_backend.sh                   # init helper for the GitLab HTTP backend
├── tools/                                  # the Python automation layer
│   ├── build_image/                        # Phase 1: bake the VMID 900 template
│   ├── bootstrap_cluster.py                # Phase 4: 6-sub-phase k3s bootstrap
│   └── lib/                                # shared libraries (pve, helm, secrets, log)
├── specs/001-build-a-kubernetes-k3s-cluster-on-proxmo/  # planning artefacts
├── tools/tests/                            # 92 pytest tests (build, skill, pve, ...)
├── Makefile                                # make build-image / make test / make lint
├── versions.yaml                           # master compatibility matrix
├── spec-bridge.conf                        # spec-bridge workspace config
└── README.md (this file)
```

## Five-phase pipeline (high level)

| Phase | Subsystem | Output | Tool |
|---|---|---|---|
| 0 | Token Provisioning | `infra/tokens/output.json` (mode 0600) + PVE role + CF scoped token | `python scripts/apply_tofu.py tokens` |
| 1 | Image Build | Proxmox template at VMID 900 + `build/image-id.txt` | `python -m tools.build_image` (or `make build-image`) |
| 2 | Cluster Provisioning | 4 cloned VMs (111-114) + PowerDNS A/PTR records | `python scripts/apply_tofu.py cicd && python scripts/apply_tofu.py apps` |
| 3 | Host-ports baseline | `logs/host-ports-baseline.json` snapshot | `bash scripts/capture_host_ports_baseline.sh` |
| 4 | Cluster Bootstrap | working k3s clusters with all Helm releases | `python -m tools.bootstrap_cluster --cluster {cicd|apps}` |
| 5 | Final verification | SC-001..SC-007 + NFR-010..NFR-014 checks | see [`docs/verification.md`](docs/verification.md) |

The skill ([`.agents/skills/proxmox-k3s-pipeline/SKILL.md`](.agents/skills/proxmox-k3s-pipeline/SKILL.md))
is the authoritative step-by-step; the docs here are summaries.

## Quick start (operator-only, live host)

Prerequisites: a Proxmox VE host reachable over SSH (the skill assumes
`kvm.bruj0.net:6022` but the same recipe applies to any PVE 9.x host
with a single-node SDN zone).

```bash
# 0. Set env (or ./.env with the same keys)
cat > .env <<'EOF'
PROXMOX_API_URL=https://kvm.bruj0.net:8006/api2/json
PROXMOX_API_TOKEN=root@pam!tf-bootstrap=<uuid>
CLOUDFLARE_TOKEN_CREATOR=<cfat_...>
CLOUDFLARE_ACCOUNT_ID=<uuid>
CLOUDFLARE_DOMAIN=bruj0.net
CLOUDFLARE_GLOBAL_API_KEY=<key>
CLOUDFLARE_GLOBAL_API_EMAIL=<email>
CLOUDFLARE_ZONE_ID=<uuid>
GITLAB_PAT=<glpat-...>
POWERDNS_API_KEY=<key>
EOF

# 1. Mint tokens (Phase 0)
python scripts/apply_tofu.py tokens

# 2. Bake the Ubuntu+k3s golden template at VMID 900 (Phase 1)
python -m tools.build_image \
  --pve-endpoint https://kvm.bruj0.net:8006/api2/json \
  --pve-node BigBertha \
  --pve-ssh-host kvm.bruj0.net --pve-ssh-port 6022 \
  --ssh-pubkey-path ~/.ssh/kvm.bruj0.net.pub

# 3. Clone the 4 cluster VMs (Phase 2)
python scripts/apply_tofu.py cicd
python scripts/apply_tofu.py apps

# 4. Sync PowerDNS A/PTR to the IPs the SDN assigned (post-apply fix-up)
python scripts/sync_dns_to_sdn.py \
  --vmid 111 --name cicd-cp-1 --vmid 112 --name cicd-w-1 \
  --vmid 113 --name apps-cp-1 --vmid 114 --name apps-w-1

# 5. Bootstrap the clusters (Phase 4, ~5 min per cluster)
python -m tools.bootstrap_cluster --cluster cicd
python -m tools.bootstrap_cluster --cluster apps
```

After step 5, `~/.kube/config` contains contexts for both clusters and
the cicd cluster is reachable from the public internet via the
Cloudflare Tunnel.

## Development

```bash
make test           # 92 pytest tests
make lint           # ruff + mypy on tools/
make test-infra-tokens    # tofu test for infra/tokens
make test-infra-modules   # tofu test for every module under infra/modules
make test-infra-clusters  # tofu test for every instance under infra/clusters
```

## Status (2026-07-07)

- **Phases 0-2 verified end-to-end** on `kvm.bruj0.net` (BigBertha,
  PVE 9.2.3, kernel 7.0.6-2-pve) with the canonical Proxmox+Ubuntu
  recipe: `virt-customize` bakes `qemu-guest-agent` into the cloud
  image BEFORE the VM is created, Proxmox's native cloud-init drive
  (`--ide2 data1:cloudinit`) replaces the custom NoCloud seed ISO.
  4 cluster VMs (111-114) are cloned, running, and report
  `qm agent ping` OK.
- **Phases 3-5** are documented from the original spec; the
  Phase-4 sub-phases are `cloudinit, k3s, helm, kubeconfig, host_ports,
  externalname`.
- **92 pytest tests pass**; the test suite pins every load-bearing
  recipe change against the live host.
- The pipeline pivoted off Talos Linux on 2026-07-07 because the
  serial-console debug loop on PVE 9.2.3 was unworkable. The
  full pivot history is at the bottom of
  [`.agents/skills/proxmox-k3s-pipeline/SKILL.md`](.agents/skills/proxmox-k3s-pipeline/SKILL.md).

## See also

- [`.agents/skills/proxmox-k3s-pipeline/SKILL.md`](.agents/skills/proxmox-k3s-pipeline/SKILL.md) — the canonical playbook
- [`docs/architecture.md`](docs/architecture.md) — subsystem boundaries + Mermaid
- [`docs/verification.md`](docs/verification.md) — success criteria + NFRs
- [`docs/runbooks/`](docs/runbooks/) — single-concern operator procedures
- [`specs/001-build-a-kubernetes-k3s-cluster-on-proxmo/`](specs/001-build-a-kubernetes-k3s-cluster-on-proxmo/) — original planning artefacts
- [`AGENTS.md`](AGENTS.md) — for AI agents making changes to this repo
