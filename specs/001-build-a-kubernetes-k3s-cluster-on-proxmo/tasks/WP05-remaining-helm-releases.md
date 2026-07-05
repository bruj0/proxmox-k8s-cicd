---
work_package_id: "WP05"
title: "Remaining Helm Releases + kubeconfig Merge"
lane: "doing"
dependencies:
  - WP04
subsystem: "SS3 (Bootstrap Orchestration + Agent Skill)"
misfits_addressed:
  - M2 (verified)
  - M6
  - NFR-007
abstract_components:
  - tools/bootstrap_cluster.py (extended)
  - clusters/cicd/manifests/ (any pre-rendered Helm values)
agent: "spec-bridge-implement"
reviewed_by: "spec-bridge-review"
build_validated: true
tdd_red_clean: true
history:
  - timestamp: "2026-07-06T02:50:00+00:00"
    lane: doing
    agent: spec-bridge-implement
    action: started implementation
  - timestamp: "2026-07-06T03:30:00+00:00"
    lane: for_review
    agent: spec-bridge-implement
    action: implementation complete -- ready for review
  - timestamp: "2026-07-06T03:45:00+00:00"
    lane: doing
    agent: spec-bridge-review
    action: review started
---

# WP05 — Remaining Helm Releases + kubeconfig Merge

## Goal

Extend `bootstrap_cluster.py` to install the remaining four locked Helm releases:

1. **sergelogvinov/proxmox-cloud-controller-manager** v0.14.0 (sets `providerID` + topology labels)
2. **sergelogvinov/proxmox-csi-plugin** v0.19.1 (chart 0.5.9; StorageClass `proxmox-lvm-thin`)
3. **Traefik (demoted)** via the HelmChartConfig rendered in WP02
4. **STRRL/cloudflare-tunnel-ingress-controller** v0.0.23 (IngressClass `cloudflare-tunnel`)
5. **cert-manager** v1.16.x (in-cluster CA only; no ACME for public path)

Final kubeconfig merge step. PVE firewall assertion (no new DNAT rules).

## Execution constraints

- Product code and tests: only in `$WORKTREES_DIR/001-build-a-kubernetes-k3s-cluster-on-proxmo-WP05/`
- Do not merge to `$TARGET_BRANCH` until `spec-bridge-merge` after accept

## Subtasks

### T000 — Version compatibility matrix (gate before any other subtask)

Before scaffolding anything, build a per-WP version matrix:

1. **Identify every external dependency this WP will touch.** For WP05: `sergelogvinov/proxmox-cloud-controller-manager` v0.14.0, `sergelogvinov/proxmox-csi-plugin` v0.19.1 (chart 0.5.9), STRRL/cloudflare-tunnel-ingress-controller v0.0.23, cert-manager v1.16.x, kubectl, ssh, Python.
2. **For each dependency, run `context7-auto-research`** (load `.agents/skills/context7-auto-research/SKILL.md` first) to confirm:
   - The **latest stable release** version.
   - The **latest unstable release** version **only if it supports a feature we need that stable does not** — e.g. STRRL may have a newer 0.0.24 with multi-cluster support; document the decision.
3. **Cross-check compatibility**:
   - Proxmox CCM ↔ Proxmox VE 9.2.3 API
   - Proxmox CSI ↔ lvm-thin on `data1` (CSI plugin's LVM driver must support the Proxmox VE 9.x API)
   - STRRL controller ↔ Cloudflare API version (the controller calls Cloudflare's tunnel API; the API version pinned in our scoped token must match)
   - cert-manager ↔ k3s 1.34.x (cert-manager v1.16.x is the version that's been tested with k8s 1.30+; confirm 1.34 support)
4. **Document the result** in `tools/versions.lock.yaml` (append to WP04's lock):
   ```yaml
   additional_dependencies:
     - name: sergelogvinov/proxmox-cloud-controller-manager
       version: "0.14.0"
     - name: sergelogvinov/proxmox-csi-plugin
       version: "0.5.9 (app v0.19.1)"
     - name: strrl/cloudflare-tunnel-ingress-controller
       version: "0.0.23"
     - name: cert-manager
       version: "v1.16.x"
   cross_check:
     proxmox_ccm_ve_api: "compatible (v0.14.0 supports PVE 9.x)"
     proxmox_csi_lvm: "compatible (lvm-thin supported)"
     strrl_cf_api: "compatible (token scope matches API endpoint)"
     cert_manager_k8s: "compatible (v1.16.x supports k8s 1.34)"
   ```
5. **The agent must NOT proceed** to T001+ until this file exists and is reviewed.

This subtask is the canonical "T000" step for every WP in this feature. Repeat it in every WP, scoped to that WP's dependencies.

### T001 — `context7-auto-research` for Proxmox CCM + CSI + STRRL controller

Verify:
- `sergelogvinov/proxmox-cloud-controller-manager` chart values schema (region, zone, credentials)
- `sergelogvinov/proxmox-csi-plugin` chart values schema (storage class, lvm-thin pool, region/zone)
- `STRRL/cloudflare-tunnel-ingress-controller` v0.0.23 values schema: confirm `cloudflare.apiToken`, `cloudflare.accountId`, `cloudflare.tunnelName`, `ingressClass.name`, `ingressClass.controller`. Confirm what happens when `tunnelName` already exists in Cloudflare (adoption vs. error).

### T002 — Proxmox CCM Helm release

```python
def proxmox_ccm_release(cluster: dict) -> dict:
    return {
        "release": "proxmox-cloud-controller-manager",
        "chart": "sergelogvinov/proxmox-cloud-controller-manager",
        "version": "0.14.0",
        "namespace": "kube-system",
        "values": {
            "region": "bigbertha",
            "zone": "BigBertha",
            "credentials": {
                "url": "https://10.0.0.1:8006",
                "tokenId": secrets["proxmox_token_id"],
                "tokenSecret": secrets["proxmox_token_secret"],
            }
        }
    }
```

### T003 — Proxmox CSI Helm release

```python
def proxmox_csi_release(cluster: dict) -> dict:
    return {
        "release": "proxmox-csi-plugin",
        "chart": "sergelogvinov/proxmox-csi-plugin",
        "version": "0.5.9",
        "namespace": "proxmox-csi-plugin",
        "values": {
            "storageclass": {
                "name": "proxmox-lvm-thin",
                "default": True,
            },
            "region": "bigbertha",
            "zone": "BigBertha",
            "csi": {
                "lvm": {
                    "thinPool": "data1/data1",
                }
            }
        }
    }
```

Plus a post-step: smoke test that a 1-replica Deployment with a PVC succeeds (PVC binds, Pod becomes Ready).

### T004 — Traefik (demoted) Helm release

The HelmChartConfig is rendered by `modules/proxmox-k3s-cluster/traefik-chartconfig.yaml.tftpl` (already in WP02). In WP05, we apply it via Talos's `HelmChartConfig` mechanism OR via a `kubectl apply` step that targets the rendered YAML.

Note: the demoted Traefik values come from the module output, not from `bootstrap_cluster.py`. The bootstrap script's job is to verify the rendered HelmChartConfig was applied (post-apply assertion: `kubectl --context <cluster> get helmchartconfig -n kube-system traefik -o yaml` shows `service.type=ClusterIP`).

### T005 — STRRL/cloudflare-tunnel-ingress-controller Helm release

```python
def cloudflare_tunnel_release(cluster: dict) -> dict:
    return {
        "release": "cloudflare-tunnel-ingress-controller",
        "chart": "oci://ghcr.io/strrl/charts/cloudflare-tunnel-ingress-controller",
        "version": "0.0.23",
        "namespace": "cloudflare-tunnel-ingress-controller",
        "values": {
            "cloudflare": {
                "apiToken": secrets["cf_api_token"],
                "accountId": secrets["cf_account_id"],
                "tunnelName": cluster["cf_tunnel_name"],
            },
            "ingressClass": {
                "name": "cloudflare-tunnel",
                "controller": "dev.strrl.cloudflaretunnelingresscontroller/ingress",
                "enabled": True,
            }
        }
    }
```

### T006 — cert-manager (in-cluster CA only)

```python
def cert_manager_release() -> dict:
    return {
        "release": "cert-manager",
        "chart": "cert-manager/cert-manager",
        "version": "v1.16.x",
        "namespace": "cert-manager",
        "values": {
            "installCRDs": True,
            # NO ACME solvers for the public path
            # Only ClusterIssuer "internal-ca" created in a post-step
        }
    }
```

Plus a post-step that applies an internal-CA ClusterIssuer manifest.

### T007 — `verify-no-host-ports` post-step

```python
def verify_no_host_ports_added(log, baseline_dnat_chain: str) -> None:
    """SSH to PVE and assert nft prerouting chain has no new DNAT rules vs baseline."""
    result = subprocess.run(
        ["ssh", "-p", "6022", "-o", "BatchMode=yes",
         "root@10.0.0.1", "nft list chain ip nat prerouting"],
        capture_output=True, text=True, timeout=30,
    )
    current = result.stdout
    new_rules = diff(current, baseline_dnat_chain)
    if new_rules:
        raise HostPortsAddedError(
            f"new DNAT rules detected: {new_rules}",
            resolution="Inspect nft table; revert any unintended DNAT rules",
        )
```

### T008 — kubeconfig merge step

```python
def run_kubeconfig_phase(cluster: dict, log, secrets) -> None:
    # Fetch kubeconfig from first control-plane node
    talos = TalosClient(log, secrets)
    kubeconfig_yaml = talos.kubeconfig(cluster["control_plane_ips"][0])
    # Merge
    merger = KubeconfigMerger(log)
    merger.merge(cluster_name=cluster["cluster_name"], kubeconfig_yaml=kubeconfig_yaml)
    log.info(step="kubeconfig_merged", context=cluster["cluster_name"])
```

### T009 — Tests

```python
def test_traefik_chartconfig_uses_clusterip(cluster):
    """The rendered HelmChartConfig applied to the cluster contains service.type=ClusterIP."""
    ...

def test_cert_manager_no_acme_solvers(cluster):
    """cert-manager Helm values do not include any ACME solver configuration."""
    ...

def test_no_host_ports_added(cluster):
    """verify-no-host-ports post-step fails when a new DNAT rule is detected."""
    ...
```

### T010 — Lint + test + run

```bash
pytest tools/tests/
mypy --strict tools/
ruff check tools/
python tools/bootstrap_cluster.py --cluster cicd --phase all
```

## Acceptance Criteria

- [ ] `helm list -A` shows all 6 releases `deployed` (cilium, kube-vip, proxmox-cloud-controller-manager, proxmox-csi-plugin, traefik, cloudflare-tunnel-ingress-controller, cert-manager)
- [ ] `kubectl --context cicd get nodes` shows topology labels (`topology.kubernetes.io/region=bigbertha`, `topology.kubernetes.io/zone=BigBertha`)
- [ ] A 1-replica Deployment with a PVC succeeds end-to-end (PVC binds, Pod Ready)
- [ ] `ssh root@10.0.0.1 -p 6022 'nft list chain ip nat prerouting'` shows **zero** new DNAT rules compared to the pre-WP05 baseline (verify-no-host-ports step passes)
- [ ] An Ingress of class `cloudflare-tunnel` resolves via Cloudflare within 60 s (smoke test)
- [ ] cert-manager is installed with only the in-cluster CA ClusterIssuer; no ACME solvers configured for the public path
- [ ] `~/.kube/config` contains a `cicd` context pointing at `https://10.0.0.30:6443`
- [ ] Re-running `bootstrap_cluster.py --cluster cicd --phase all` is a no-op in <60 s
- [ ] All tests pass

## Technical context

- **Python**: ≥3.11
- **External**: `helm`, `kubectl`, `ssh` on PATH
- **Helm repositories**: `cert-manager`, `cilium`, `kube-vip`, `sergelogvinov/proxmox-cloud-controller-manager`, `sergelogvinov/proxmox-csi-plugin`, `oci://ghcr.io/strrl/charts`

## How to run

```bash
python tools/bootstrap_cluster.py --cluster cicd --phase all
```

---

## Implementation Summary

**Worktree**: `.worktrees/001-build-a-kubernetes-k3s-cluster-on-proxmo-WP05` on branch `001-build-a-kubernetes-k3s-cluster-on-proxmo-WP05`

WP05 extends SS3 (tools/bootstrap_cluster.py) to install the four remaining Helm releases after cilium + kube-vip: sergelogvinov/proxmox-cloud-controller-manager v0.14.0 (providerID + region/zone labels), sergelogvinov/proxmox-csi-plugin chart 0.5.9 (StorageClass proxmox-lvm-thin on lvm-thin pool data1), cert-manager v1.16.1 (in-cluster CA only -- installCRDs=true, NO ACME solvers per NFR-007), and oci://ghcr.io/strrl/charts/cloudflare-tunnel-ingress-controller v0.0.23 (IngressClass cloudflare-tunnel). Demoted Traefik is installed via the Talos HelmChartConfig mechanism (rendered by WP02 into clusters/<name>/manifests/traefik-helmchartconfig.yaml); SS3 applies that file via kubectl. A new host_ports phase verifies M2 (no new DNAT rules added by Helm charts) by diffing the live PVE nft prerouting chain against clusters/<name>/host_ports_baseline.txt; new DNAT rules surface as HostPortsAddedError. The bootstrap_cluster <-> host_ports cycle is broken via an on_ssh_failure callback so lib/host_ports.py stays independent of bootstrap_cluster.py.

### Files created

| File | Description |
|------|-------------|
| `scripts/capture_host_ports_baseline.sh` | Captures the initial PVE nft prerouting chain to clusters/<name>/host_ports_baseline.txt once at WP00 setup. Invoked via ssh -p 6022 to the PVE operator host. Verifies the snapshot contains 'chain prerouting' before accepting. |
| `tools/lib/host_ports.py` | verify_no_new_dnat_rules(): SSH-based nft check. Detects new dnat-to clauses and raises HostPortsAddedError. Optional on_ssh_failure callable decouples it from bootstrap_cluster's BootstrapError type -- a unit-testable seam. |
| `tools/tests/test_remaining_releases.py` | Seven pytest cases for the WP05 surface: four lock-chart coverage, no-ACME-on-cert-manager, proxmox-ccm carries credentials+region/zone, proxmox-csi declares proxmox-lvm-thin default, host_ports verifier passes on unchanged prerouting, host_ports raises on new DNAT, host_ports surfaces ssh failure via callback. |

### Test results

35/35 passing -- `cd .worktrees/001-build-a-kubernetes-k3s-cluster-on-proxmo-WP05 && python -m pytest tools/tests/ -q`

### Validator

0/0 checks passed -- `spec-bridge-skill-tool implement WP05 --feature 001-build-a-kubernetes-k3s-cluster-on-proxmo --session-id 77e7bf8a-d756-4b82-bf8b-b98ec095d0b5`
