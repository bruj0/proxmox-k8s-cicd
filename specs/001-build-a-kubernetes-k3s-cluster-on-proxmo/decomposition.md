---
feature_slug: "001-build-a-kubernetes-k3s-cluster-on-proxmo"
status: "draft"
created: "2026-07-05T15:30:00Z"
---

# Decomposition: Proxmox k3s Cluster Module + Image Builder + Bootstrapper

**Feature Branch**: `001-build-a-kubernetes-k3s-cluster-on-proxmo`
**Created**: 2026-07-05
**Status**: Draft
**Source**: [`spec.md`](spec.md), [`research.md`](research.md) (Sessions 1-7 + Final Recommendation)

---

## Misfit Inventory

Imported from spec.md `## Misfits`. Each misfit is assigned a short ID and tagged with its domain.

| ID | Misfit | Domain | Description (verbatim or condensed) |
|----|--------|--------|-------------------------------------|
| M1 | A | Concurrency / State | Packer and `tofu apply` race against the same Proxmox API; two operators running `build_image.py` simultaneously produce conflicting templates, or `tofu apply` clones from a template that is still being finalised. |
| M2 | B | Security / Network exposure | The cluster module or bootstrap script accidentally opens an inbound TCP port on vmbr0 (e.g. exposing Traefik as `LoadBalancer` instead of `ClusterIP`, or adding a DNAT chain). Violates "no host open ports" invariant. |
| M3 | C | Data Integrity / Configuration drift | A second instantiation of the cluster module (apps cluster) uses the same VIP, VMIDs, node IPs, or Talos machine cert as the first. Two clusters race for the same control-plane endpoint. |
| M4 | D | Observability / Silent failure | The cloudflared controller or bootstrap script fails to apply a Helm release and silently leaves the cluster partially configured. Cluster looks "up" but PVCs do not bind. |
| M5 | E | Configuration / DHCP collision | A Talos VM gets a DHCP lease that overlaps the kube-vip VIP (10.0.0.30/40) or another pre-allocated IP because the module forgot to call the dnsmasq host-reservation API or write the ethers entry before the VM boots. |
| M6 | F | Reversibility / Vendor lock-in | After Cloudflare Tunnel is the only public ingress, an outage of Cloudflare renders the cluster publicly unreachable. No operator-runnable fallback path documented or tested. |
| M7 | G | Security / Token exposure | The Cloudflare API token, Proxmox token secret, or SSH key material is written into tofu state, printed in apply output, or committed to git. |
| M8 | H | Compatibility / Talos+k3s mismatch | The Packer image bakes a Talos version that does not have a k3s 1.34.x compatible shim, or the Cilium Helm chart version requires a kernel feature not present in PVE 9.2.3 / kernel 7.0.6-2-pve. |

---

## Interaction Matrix

Linked pairs (X) come from spec.md `### Misfit Interaction Notes` and from analysing which misfits force changes in each other's resolution.

|     | M1 | M2 | M3 | M4 | M5 | M6 | M7 | M8 |
|-----|----|----|----|----|----|----|----|----|
| M1  | -- |    |    | X  |    |    |    | X  |
| M2  |    | -- |    |    |    | X  |    |    |
| M3  |    |    | -- |    | X  |    |    |    |
| M4  | X  |    |    | -- |    |    | X  |    |
| M5  |    |    | X  |    | -- |    |    |    |
| M6  |    | X  |    |    |    | -- |    |    |
| M7  |    |    |    | X  |    |    | -- |    |
| M8  | X  |    |    |    |    |    |    | -- |

Notable non-edges:

- **M2 and M3 are independent.** M2 is "don't add host ports", M3 is "don't reuse cluster identity". Same module, different invariants.
- **M6 only links to M2.** M6 (Cloudflare outage fallback) is meaningless without the "no host open ports" baseline.
- **M8 only links to M1.** A bad image (M8) blocks Packer (M1) and everything downstream.

### Strongly linked groups

- {M1, M4, M7, M8} — bootstrap orchestration + secret hygiene + image compatibility. Linked by the bootstrap pipeline: a Packer race (M1) compounds silent Helm failures (M4), secret leaks (M7) compound silent failures (M4), and a bad image (M8) blocks the pipeline at the very start (M1).
- {M3, M5} — cluster identity + DHCP safety. Both are "two clusters overlap" problems that require the same module-level invariant (parameterised VIP/VMID/IP/cert-prefix that the module rejects if duplicated).
- {M2, M6} — no host open ports vs. Cloudflare fallback. B is the design intent; F is the only sanctioned way to break it.

---

## Subsystem Identification

Alexander's principle: dense internal coupling, sparse external coupling. Three subsystems emerge.

### Subsystem 1: Image Build Pipeline (Packer)

**Misfits**: M1, M8, partially M4

**Boundary justification**: Image production is upstream of cluster provisioning and is what M1 (race) and M8 (compatibility) directly attack. M4 (silent Helm failure) is partially here because a bad image blocks every downstream step.

**Key responsibilities**:
- Drive `hashicorp/proxmox` v1.2.3 to clone Talos ISO -> install -> halt -> convert to template.
- Be idempotent (re-running with same `--talos-version` is a no-op).
- Validate the Talos version against a known-compatible matrix.
- Surface structured errors (M1, M4) when PVE API is unreachable or Packer fails mid-build.
- Clean up half-baked VMs on failure.

**Why it's its own subsystem**: the image is reused across both cluster instances (cicd and apps), and changing the image version requires changing every cluster. It has no real-time coupling with cluster provisioning -- the cluster module reads `build/image-id.txt` as a static file at plan time.

---

### Subsystem 2: Cluster Provisioning Module (OpenTofu)

**Misfits**: M2, M3, M5, M6

**Boundary justification**: Cluster identity (M3) and DHCP safety (M5) are both invariants enforced by the same module-level inputs (VIP, VMID range, IP range, Talos cert prefix) that the module validates at plan time. No-host-ports (M2) and the Cloudflare fallback (M6) are both enforced by the same Traefik HelmChartConfig plus the explicit-fallback-flip mechanism. M2, M3, M5, M6 are all module invariants; they belong together.

**Key responsibilities**:
- Clone N VMs from the template (1 control-plane + N workers, configurable per cluster).
- Reserve the cluster VIP in dnsmasq ethers before any VM is started (M5).
- Reject `control_plane.count = 2` (etcd 2-node invalid).
- Output a `nodes` map (name, vmid, ip, mac, talos_hostname, role) for the bootstrap script.
- Render Talos machineconfig files per VM.
- Render the Cloudflare-fallback-off-by-default HelmChartConfig (M2, M6).
- Emit `terraform_data` triggers that the bootstrap script consumes.

**Why it's its own subsystem**: it owns the declarative cluster state. It does not execute the bootstrap; it produces the inputs the bootstrap script reads. Boundary with subsystem 3 is well-defined: a JSON/YAML file on disk.

---

### Subsystem 3: Bootstrap Orchestration + Agent Skill

**Misfits**: M4 (silent failure), M7 (token exposure), partially M1 (Packer race surfaces here)

**Boundary justification**: This subsystem is the runtime layer that applies the modules' outputs to a live cluster. It is where M4 manifests (Helm releases partially deployed) and where M7 is enforced (secrets read at runtime from env, never logged). It also wraps the whole pipeline in an Agent Skill (`.agents/skills/proxmox-k3s-pipeline/SKILL.md`) so any agent can drive it.

**Key responsibilities**:
- Apply Talos machineconfig to each node via `talosctl apply-config`.
- Install k3s server/agent with the right flags on first server vs. others.
- Install the six locked Helm releases in order (Cilium, kube-vip, Proxmox CCM, Proxmox CSI, demoted Traefik, Cloudflare Tunnel Controller).
- Apply the ExternalName manifest for cross-cluster Services.
- Read secrets from env / secret-store, never from files committed to the repo (M7).
- Emit structured error logs that name the failing step (M4).
- Surface a clean public interface -- the Agent Skill -- so the operator talks to an agent, not to the script directly.

**Why it's its own subsystem**: it owns runtime behaviour (what the module declares). The boundary with subsystem 2 is "module plan output -> script input". The boundary with the operator is the Agent Skill, which is itself a subsystem-level artefact.

---

## Constructive Diagrams

### Subsystem 1 -- Image Build Pipeline

```
Operator / Agent Skill
        |
        | invoke build_image.py --talos-version vX.Y.Z
        v
   build_image.py
        |
        | (1) check build/image-id.txt for matching VMID
        |     --> if found, exit 0 ("already up to date")
        | (2) validate talos_version against versions.yaml
        |     --> if mismatch with PVE kernel, exit non-zero
        | (3) invoke Packer
        v
   Packer (hashicorp/proxmox v1.2.3)
        |
        | clone from Talos ISO -> boot -> install to disk -> halt -> convert
        v
   PVE: VMID 900 (template)
        |
        | (4) write VMID 900 -> build/image-id.txt
        v
   build/image-id.txt consumed by subsystem 2 (tofu apply)
```

**Components derived**:
- `build_image.py` -- resolves M1 (idempotency), M4 (structured errors), M8 (matrix validation)
- `modules/proxmox-k3s-cluster/versions.yaml` -- resolves M8 (compatibility matrix)
- `build/image-id.txt` -- decoupling artefact between subsystems 1 and 2

---

### Subsystem 2 -- Cluster Provisioning Module

```
Operator / Agent Skill
        |
        | tofu apply (clusters/cicd/, clusters/apps/)
        v
   Root module (clusters/<name>/main.tf)
        |
        | calls modules/proxmox-k3s-cluster with:
        |   cluster_name, vip, vmid_start, ip_start,
        |   control_plane.count, workers.count, image_id
        v
   modules/proxmox-k3s-cluster
        |
        | (1) validate count in {1, 3} for control_plane
        | (2) validate vip / VMID range / IP range not used by another cluster
        | (3) write dnsmasq ethers reservation for VIP
        | (4) clone N VMs from template at var.image_id
        | (5) render Talos machineconfig per VM (writes to clusters/<name>/talos/)
        | (6) render Cloudflare Tunnel controller + Traefik HelmChartConfig (off by default)
        v
   Output: nodes map (name, vmid, ip, mac, role, talos_hostname)
           consumed by subsystem 3
```

**Components derived**:
- `modules/proxmox-k3s-cluster/main.tf` -- resolves M3 (identity uniqueness), M5 (DHCP safety)
- `modules/proxmox-k3s-cluster/variables.tf` -- required inputs that the module rejects if duplicated (M3)
- `modules/proxmox-k3s-cluster/dnsmasq.tf` -- ethers reservation (M5)
- `modules/proxmox-k3s-cluster/talos.tf` -- per-VM machineconfig renderer (consumed by subsystem 3)
- `modules/proxmox-k3s-cluster/traefik-chartconfig.yaml.tftpl` -- HelmChartConfig rendering for M2/M6
- Root modules `clusters/cicd/main.tf` and `clusters/apps/main.tf` -- two instances of the module

---

### Subsystem 3 -- Bootstrap Orchestration + Agent Skill

```
Operator
   |
   | "Bring up both clusters"
   v
AI Agent (loads .agents/skills/proxmox-k3s-pipeline/SKILL.md)
   |
   | instructs agent to run the 5-phase pipeline
   v
bootstrap_cluster.py --cluster <name>
   |
   | (1) talosctl apply-config --nodes <all IPs>          [subsystem 2 input]
   | (2) helm install Cilium
   | (3) helm install kube-vip
   | (4) helm install Proxmox CCM
   | (5) helm install Proxmox CSI
   | (6) helm install Traefik (demoted)
   | (7) helm install STRRL/cloudflare-tunnel-ingress-controller
   | (8) helm install cert-manager (in-cluster CA only)
   | (9) fetch kubeconfig from first CP, merge into ~/.kube/config
   | (10) for apps cluster, apply ExternalName manifest
   |
   | on any failure: emit structured error JSON, abort, exit non-zero
   | secrets: read from env at runtime, never logged
   v
kubectl --context <name> get nodes   -> Ready
```

**Components derived**:
- `tools/bootstrap_cluster.py` -- resolves M4 (structured abort), M7 (env-based secrets), M1 (Packer race surfaces here too)
- `tools/build_image.py` -- also part of this subsystem from the agent's POV (same skill drives both)
- `clusters/apps/manifests/cicd-system/externalname.yaml` -- the cross-cluster wiring (consumed when bootstrap_cluster.py runs with --cluster apps)
- `.agents/skills/proxmox-k3s-pipeline/SKILL.md` -- the Agent Skill that drives everything

---

## Cross-Subsystem Contracts

### Contract: Subsystem 1 (Image Build) -> Subsystem 2 (Cluster Module)

- **Artefact exchanged**: `build/image-id.txt` -- a single line containing the Proxmox template VMID.
- **Failure mode**: file missing or empty. Subsystem 2's plan-time validation rejects empty `image_id` with `Error: image_id is empty; run tools/build_image.py first`.
- **Coupling type**: file-system artefact (one-way).
- **Reversibility**: deleting the file forces subsystem 2 to fail at plan time (safe).

### Contract: Subsystem 2 (Cluster Module) -> Subsystem 3 (Bootstrap)

- **Artefact exchanged**: a JSON or YAML output at `clusters/<name>/output.json` containing:
  ```json
  {
    "cluster_name": "cicd",
    "vip": "10.0.0.30",
    "nodes": [
      {"role":"control_plane","name":"cicd-cp-1","vmid":200,"ip":"10.0.0.201","mac":"...","talos_hostname":"cicd-cp-1"},
      {"role":"worker","name":"cicd-w-1","vmid":201,"ip":"10.0.0.202","mac":"...","talos_hostname":"cicd-w-1"}
    ],
    "talos_dir": "clusters/cicd/talos/"
  }
  ```
- **Failure mode**: file missing. Subsystem 3 aborts with `Error: <name>/output.json not found; run tofu apply first`.
- **Coupling type**: file-system artefact (one-way).
- **Reversibility**: deleting the file is safe; subsystem 3 refuses to run without it.

### Contract: Subsystem 3 (Bootstrap) -> Operator (via Agent Skill)

- **Artefact exchanged**: structured logs (human-readable console + JSON file), an updated `~/.kube/config`, and a final summary report.
- **Failure mode**: any step that fails exits non-zero with a structured error JSON. The Agent Skill instructs the agent to halt and wait for the operator.
- **Coupling type**: in-process invocation (the agent reads the skill and invokes the script).
- **Reversibility**: every action the script takes is reversible by `tofu destroy` (subsystem 2) or by re-running the script with the same args (idempotency).

---

## Mapping to Plan

| Subsystem | Components | Suggested WP Scope |
|-----------|------------|-------------------|
| **1 -- Image Build Pipeline** | `tools/build_image.py`, `modules/proxmox-k3s-cluster/versions.yaml`, `build/image-id.txt` | **WP01 -- Image build + module skeleton**. Includes the OpenTofu module directory, the Packer template config, and `build_image.py`. Proves the Packer-to-template contract end-to-end. |
| **2 -- Cluster Provisioning** | `modules/proxmox-k3s-cluster/{main,variables,dnsmasq,talos}.tf`, `traefik-chartconfig.yaml.tftpl`, root modules `clusters/cicd/`, `clusters/apps/` | **WP02 -- Cluster module + first cluster (cicd)**: tofu apply cicd end-to-end (VMs exist, ethers reserved, Talos machineconfig written, output.json present). |
| 2 cont. | (same module, second instance) | **WP03 -- Second cluster instance (apps)**: tofu apply apps, with distinct VIP / VMID / IP / cert-prefix. Proves M3, M5 hold across instances. |
| **3 -- Bootstrap Orchestration + Skill** | `tools/bootstrap_cluster.py`, `clusters/apps/manifests/cicd-system/externalname.yaml`, `.agents/skills/proxmox-k3s-pipeline/SKILL.md` | **WP04 -- Bootstrap script + cilium/kube-vip**: brings cicd to "k3s up + 2 Helm releases Ready". Proves M4 (silent failure abort). |
| 3 cont. | (same script, more releases) | **WP05 -- Remaining Helm releases + kubeconfig merge**: CCM + CSI + Traefik demote + Cloudflare Tunnel + cert-manager. Proves M2, M6 (no host open ports by default). |
| 3 cont. | (same script, cross-cluster) | **WP06 -- Cross-cluster Services + apps bootstrap**: ExternalName manifest, bootstrap apps cluster end-to-end. Proves apps -> cicd reachability. |
| 3 cont. | Agent Skill | **WP07 -- Agent Skill + verification + docs**: author `.agents/skills/proxmox-k3s-pipeline/SKILL.md`, write the cloudflare-fallback runbook, run final SC-001 through SC-006 verifications. |
| Optional / cleanup | decommission LXC 103 `cloudflared` | **WP08 (optional) -- LXC decommission**: 16 GB recovered on `data1`. Independent of everything else. |

### WP independence

| WP | Depends on | Independent test |
|----|-----------|------------------|
| WP01 | nothing | `build_image.py` exits 0, `qm list` shows template, `image-id.txt` populated |
| WP02 | WP01 | `tofu apply -chdir=clusters/cicd` succeeds; VMs exist, ethers reserved |
| WP03 | WP02 (for shared module) | `tofu apply -chdir=clusters/apps` succeeds; 2 instances coexist, no overlap |
| WP04 | WP02 | `bootstrap_cluster.py --cluster cicd` brings cilium+kube-vip Ready |
| WP05 | WP04 | All 6 Helm releases Ready on cicd; cloudflare-tunnel IngressClass resolves |
| WP06 | WP05 + WP03 | apps cluster bootstrapped, ExternalName Services resolve to cicd |
| WP07 | WP06 | Agent Skill drives the whole pipeline end-to-end |
| WP08 | none | (optional) LXC 103 decommissioned, 16 GB recovered |

### Misfits resolved per WP

| WP | Misfits fully resolved at completion |
|----|--------------------------------------|
| WP01 | M1 (Packer race), M8 (compatibility) |
| WP02 | M3, M5 (cluster identity + DHCP), M2, M6 (no host open ports by default) |
| WP03 | M3, M5 (proven across instances) |
| WP04 | M4 (silent failure abort) |
| WP05 | M2, M6 (proven via cloudflare-tunnel ingress + fallback runbook) |
| WP06 | (cross-cluster reachability verified) |
| WP07 | M7 (secrets handled via env at runtime; skill codifies the rule) |

### Note on M7

M7 (token exposure) is not "resolved" by any single WP -- it is enforced by every script and every tofu variable in every WP. The Agent Skill (WP07) codifies the rule ("read all secrets from env at runtime, never from committed files") and the runbook documents the verification.

### Note on M4

M4 (silent Helm failure) is partially resolved by WP04 (the script aborts on the first failed release) and fully resolved by WP07 (the runbook includes the verification step that catches the "looks up but PVCs don't bind" failure mode).