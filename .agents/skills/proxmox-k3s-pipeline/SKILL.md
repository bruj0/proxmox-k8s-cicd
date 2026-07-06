---
name: proxmox-k3s-pipeline
description: Bring up two k3s clusters (cicd + apps) on a single Proxmox host using OpenTofu, Packer, and an Agent-driven bootstrap. Use when the user says "bring up both clusters", "deploy the pipeline", "run spec 001", "bootstrap a cluster", "scale workers", "decommission a cluster", or "fix the cloudflare fallback". Outputs a fully bootstrapped cluster pair with public HTTPS via Cloudflare Tunnel (no host open ports) and apps->cicd cross-cluster Service consumption via ExternalName.
---

# Proxmox k3s Pipeline

End-to-end pipeline for provisioning two Talos/k3s clusters on a single
Proxmox host. The pipeline drives five numbered
top-level phases; the bootstrap phase (Phase 4) further decomposes
into six ordered sub-phases (talos, k3s, helm, kubeconfig, host_ports,
externalname). Each (sub-)phase has a single CLI entry point and
explicit success criteria that the agent MUST assert before proceeding.

## When to load this skill

Load when the operator asks to bring up, scale, troubleshoot, or
decommission the k3s clusters provisioned by spec 001.

## Glossary (canonical vocabulary)

The bounded context for this skill is in
[CONTEXT.md](./CONTEXT.md). The five canonical terms are:

- **Agent Skill**: this document (the agentskills.io SKILL.md artifact
  loaded by Claude Code, Cursor, etc.).
- **Operator**: the human or AI agent that invokes the skill.
- **Pipeline**: the five-top-level-phase end-to-end sequence (build
  image -> provision cluster -> capture baseline -> bootstrap -> final
  verification).
- **Phase**: one numbered top-level stage of the pipeline. Phase 4
  (bootstrap) further decomposes into six sub-phases.
- **Runbook**: a single-concern copy-pasteable procedure under
  `docs/runbooks/`. Runbooks do not require an Agent; the operator
  follows them directly.

## Step 0 — Load the context7-auto-research gate (MANDATORY)

Before invoking any external library, load
`.agents/skills/context7-auto-research/SKILL.md` and run
`context7-auto-research` for each library the pipeline touches.
**Do NOT rely on training data for library APIs.** The pipeline
uses the following pinned versions; record the rationale for each
in the operator's reply before invoking the library:

| Library | Version | Rationale (context7) |
|---|---|---|
| `bpg/proxmox` (OpenTofu provider) | `0.111.1` | rationale: latest stable that exposes `proxmox_cloned_vm`; v0.111.1 introduces the `host` attribute that the WP02 module uses |
| `hashicorp/proxmox` (Packer plugin) | `1.2.3` | rationale: latest stable Packer plugin; required for `packer init` to discover the `proxmox-iso` builder |
| `STRRL/cloudflare-tunnel-ingress-controller` (Helm chart) | `0.0.23` | rationale: only stable version on the strrl chart repo as of 2026-07; pinned because the upstream CRDs are still alpha |
| `cilium` (Helm chart) | `1.16.x` | rationale: matches the Talos 1.10.x kernel constraint and supports `gatewayAPI.enabled` plus eBPF host routing |
| `sergelogvinov/proxmox-cloud-controller-manager` (Helm chart) | `0.14.0` | rationale: latest stable; required for `topology.kubernetes.io/region` + `zone` labels on the apps cluster nodes |
| `sergelogvinov/proxmox-csi-plugin` (Helm chart) | `0.5.9` | rationale: chart 0.5.9 supports PVE 9.x and lvm-thin on `data1/data1` |
| `talosctl` | `1.10.x` | rationale: matches the Talos image baked by SS1; required for `talosctl apply-config` and `talosctl kubeconfig` |
| `k3s` | `1.34.x` | rationale: matches the Cilium + kube-vip versions; no known CVEs |
| `helm` | `3.x` | rationale: required for `helm upgrade --install`; matches what k3s 1.34 ships |

Document each library's rationale in the operator's reply **before**
calling the library.

## Step 1 — Phase 1: Build the VM image (SS1)

Goal: bake a Talos Linux golden image into a Proxmox template
(VMID 900). One-shot; idempotent on rerun (image-id.txt already
exists -> no-op).

```bash
make build-image
```

Success criteria (assert ALL before proceeding):
1. `qm list | grep -w 900` returns a single row with `template` column
   equal to `yes`.
2. `cat build/image-id.txt` returns exactly `900` followed by newline.
3. `tools/build_image.py --audit-log build/audit.log` exits 0.

Failure handling: halt the pipeline and surface the structured error
(`error`, `resolution` keys) to the operator. Do NOT proceed to
Phase 2.

## Step 2 — Phase 2: Provision the cicd cluster (SS2)

Goal: apply OpenTofu against `infra/clusters/cicd/` to create 3 control-plane
+ N worker Talos VMs and render `output.json` +
`manifests/traefik-helmchartconfig.yaml`.

```bash
cd infra/clusters/cicd
tofu init
tofu apply -auto-approve
```

Success criteria (assert ALL before proceeding):
1. `tofu output -json > infra/clusters/cicd/output.json` exits 0 and
   `output.json` parses as JSON with `cluster_name`, `vip`,
   `pod_cidr`, `svc_cidr`, `nodes[]` keys.
2. `infra/clusters/cicd/manifests/traefik-helmchartconfig.yaml` exists and
   parses as YAML (kustomize-compatible schema).
3. `tofu test` exits 0 (no warnings about VMID overlap with apps).

## Step 3 — Phase 3: Capture host-ports baseline (M2 setup)

This is a one-shot baseline capture. Run BEFORE the first cluster
bootstrap, then never again unless the operator is decommissioning and
recreating the cluster from scratch.

```bash
PVE_SSH=root@10.0.0.1 PVE_SSH_PORT=6022 \
  ./scripts/capture_host_ports_baseline.sh infra/clusters/cicd
```

Success criteria: `infra/clusters/cicd/host_ports_baseline.txt` exists and
contains the literal substring `chain prerouting`.

## Step 4 — Phase 4: Bootstrap (SS3)

Runs the six-phase bootstrap. Order is enforced by `PHASES` in
`tools/bootstrap_cluster.py`. Each phase records its success in
`infra/clusters/<name>/bootstrap_state.json`; rerunning is a no-op for
completed phases.

For the cicd cluster:

```bash
python tools/bootstrap_cluster.py --cluster cicd
```

For the apps cluster (after cicd is healthy AND `infra/clusters/apps/`
has been provisioned by Phase 2-equivalent):

```bash
python tools/bootstrap_cluster.py --cluster apps
```

The six phases, in order:

1. `talos` — `talosctl apply-config` on every node, wait for
   healthy, bootstrap k3s.
2. `k3s` — verify `/healthz` returns `ok`.
3. `helm` — install Cilium + kube-vip (WP04) and the remaining four
   releases (proxmox-ccm, proxmox-csi, cloudflare-tunnel,
   cert-manager, WP05) + apply the rendered Traefik HelmChartConfig.
4. `kubeconfig` — pull admin kubeconfig, merge into
   `~/.kube/config`.
5. `host_ports` — assert no new DNAT rules have been added to the PVE
   nft prerouting chain (M2 misfit verifier).
6. `externalname` — apps-cluster only: apply the cross-cluster
   ExternalName Services kustomization (WP06).

Idempotency: on a rerun, the script reads
`infra/clusters/<name>/bootstrap_state.json` and skips phases whose name
appears in `phases_done`. This is the canonical "convergence from
partial state" path required by NFR-011. **Idempotency is the contract;
the operator may safely rerun the bootstrap at any point.**

Success criteria (assert ALL before proceeding):
1. `kubectl --context cicd get nodes` shows all control-plane +
   worker nodes in `Ready` state.
2. `kubectl --context cicd -n kube-system get pods --all-namespaces`
   shows Cilium + kube-vip + proxmox-ccm + proxmox-csi +
   cloudflare-tunnel + cert-manager pods `Running`.
3. `python tools/bootstrap_cluster.py --cluster cicd --phases all`
   exits 0 in <60 seconds (idempotent rerun).

## Step 5 — Phase 5: Final verification (SC-001..SC-006)

Run the verification matrix in `docs/verification.md`:

- **SC-001**: clean-room end-to-end bring-up completes in <=60 min.
- **SC-002**: PVC + Deployment succeeds on both clusters.
- **SC-003**: Ingress of class `cloudflare-tunnel` resolves via
  Cloudflare within 60 s.
- **SC-004**: `nft list chain ip nat prerouting` shows zero new DNAT
  rules.
- **SC-005**: rerun idempotency — tofu apply + bootstrap_cluster.py
  on a fully-bootstrapped cluster converges to no-op in <60 s.
- **SC-006**: `tofu destroy` cleanly removes all VMs.

NFRs verified at this phase:
- **NFR-010**: this SKILL.md has YAML frontmatter with `name` and
  non-empty `description` (test: `tools/tests/test_agent_skill.py`).
- **NFR-011**: rerun idempotency (covered above).
- **NFR-012**: every external library mentioned with version pin and
  rationale (Step 0 table; test: `tools/tests/test_agent_skill.py`).
- **NFR-013**: resource budget <= 16 vCPU + 24 GiB for default shape
  (asserted at the cluster module level).
- **NFR-014**: each new worker Ready in <5 min (asserted by the
  scale-workers runbook).

## How to invoke

```bash
cat .agents/skills/proxmox-k3s-pipeline/SKILL.md
# Or just type "bring up both clusters" to any agent that has the
# skill loaded (Claude Code, Cursor, etc.).
```

## Consumers tested

- Claude Code (latest stable, 2026-07).
- Cursor (latest stable, 2026-07).

Both consumers correctly parse the YAML frontmatter and load the
body. See `versions.lock.yaml` for the cross_check verdict.