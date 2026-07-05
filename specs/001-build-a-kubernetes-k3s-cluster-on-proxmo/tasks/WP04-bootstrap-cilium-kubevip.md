---
work_package_id: "WP04"
title: "Bootstrap Script + First Two Helm Releases (Cilium + kube-vip)"
lane: "for_review"
dependencies:
  - WP02
subsystem: "SS3 (Bootstrap Orchestration + Agent Skill)"
misfits_addressed:
  - M4
  - M7 (partial)
abstract_components:
  - tools/bootstrap_cluster.py
  - tools/lib/talos_client.py
  - tools/lib/helm_client.py
  - tools/lib/kubeconfig_merger.py
  - tools/lib/secret_loader.py (extended)
  - tools/lib/log.py (extended)
  - tools/tests/test_bootstrap_cluster.py
agent: "spec-bridge-implement"
build_validated: true
history:
  - lane: "doing"
    agent: "spec-bridge-implement"
    at: "2026-07-05T15:35:00Z"
    commit: "implement-WP04"
  - lane: "for_review"
    agent: "spec-bridge-implement"
    at: "2026-07-05T15:40:00Z"
    commit: "c13e192"
---

# WP04 — Bootstrap Script + First Two Helm Releases

## Goal

`tools/bootstrap_cluster.py` reads `clusters/<name>/output.json` + the Talos configs, applies machineconfig via `talosctl`, installs k3s, then installs **Cilium + kube-vip** as the first two of the locked Helm releases. Aborts on any failure with structured error JSON.

## Execution constraints

- Product code and tests: only in `$WORKTREES_DIR/001-build-a-kubernetes-k3s-cluster-on-proxmo-WP04/`
- Do not merge to `$TARGET_BRANCH` until `spec-bridge-merge` after accept

## Subtasks

### T000 — Version compatibility matrix (gate before any other subtask)

Before scaffolding anything, build a per-WP version matrix:

1. **Identify every external dependency this WP will touch.** For WP04: `talosctl` (Talos 1.10.x), k3s v1.34.x, Cilium chart 1.16.x, kube-vip chart 1.2.1, Python ≥3.11, `helm`, `kubectl`, `ssh`.
2. **For each dependency, run `context7-auto-research`** (load `.agents/skills/context7-auto-research/SKILL.md` first) to confirm:
   - The **latest stable release** version of each.
   - The **latest unstable release** version **only if it supports a feature we need that stable does not** — e.g. Talos 1.11.x might have a Talos 2 preview that breaks the k3s shim; document the decision.
3. **Cross-check compatibility**:
   - Talos 1.10.x ↔ k3s 1.34.x shim compatibility
   - Cilium 1.16.x ↔ PVE kernel 7.0.6-2-pve (kernel must have all required eBPF features; reject if missing)
   - k3s 1.34.x ↔ Traefik v3.x (Traefik is bundled with k3s; confirm the bundled version supports the `service.type=ClusterIP` + custom `IngressClass` configuration we need)
   - kube-vip 1.2.1 ↔ Cilium's CNI (kube-vip ARP must coexist with Cilium's kube-proxy replacement; confirm no known conflict)
4. **Document the result** in `tools/versions.lock.yaml`:
   ```yaml
   dependencies:
     - name: talosctl
       version: "v1.10.x"
     - name: k3s
       version: "v1.34.x"
     - name: cilium
       version: "1.16.x"
     - name: kube-vip
       version: "1.2.1"
     - name: python
       version: ">= 3.11"
     - name: helm
       version: ">= 3.13"
     - name: kubectl
       version: "matches k3s minor (1.34)"
   cross_check:
     talos_k3s_shim: "compatible"
     cilium_pve_kernel: "compatible (7.0.6-2-pve has all eBPF features)"
     k3s_traefik_bundled: "compatible (v3.x supports IngressClass + ClusterIP)"
     kubevip_cilium: "compatible (kube-vip runs in host network; Cilium runs in pod network)"
   ```
5. **The agent must NOT proceed** to T001+ until this file exists and is reviewed.

This subtask is the canonical "T000" step for every WP in this feature. Repeat it in every WP, scoped to that WP's dependencies.

### T001 — `context7-auto-research` for `talosctl` and `k3s`

Verify exact flags for:
- `talosctl apply-config --nodes <ip> --file <machineconfig.yaml>`
- `talosctl health --nodes <ip> --wait-timeout 5m`
- `k3s server --cluster-init --tls-san <vip> --disable=traefik`
- `k3s server --server https://<vip>:6443 --tls-san <vip>`
- `k3s agent --server https://<vip>:6443`

Document the one-node etcd caveat: k3s 1.34.x on a single control-plane VM runs etcd as a 1-node cluster, which is HA-degraded but functional for spec 001's single-host tolerance.

### T002 — `tools/lib/talos_client.py` + `tools/lib/helm_client.py`

```python
# tools/lib/talos_client.py
import subprocess, json
from pathlib import Path

class TalosClient:
    def __init__(self, log, secrets):
        self.log = log
        self.secrets = secrets

    def apply_config(self, node_ip: str, machineconfig_path: Path, talosconfig: Path) -> None:
        result = subprocess.run([
            "talosctl", "--talosconfig", str(talosconfig),
            "apply-config", "--nodes", node_ip,
            "--file", str(machineconfig_path),
        ], capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            raise TalosApplyError(result.stderr)

    def health(self, node_ip: str, talosconfig: Path) -> None:
        result = subprocess.run([
            "talosctl", "--talosconfig", str(talosconfig),
            "health", "--nodes", node_ip, "--wait-timeout", "5m",
        ], capture_output=True, text=True, timeout=360)
        if result.returncode != 0:
            raise TalosHealthError(result.stderr)

# tools/lib/helm_client.py
class HelmClient:
    def install_or_upgrade(self, release: str, chart: str, namespace: str, values: dict, version: str) -> None:
        # Use `helm upgrade --install --wait` for idempotency
        result = subprocess.run([
            "helm", "upgrade", "--install", "--wait",
            "--namespace", namespace, "--create-namespace",
            release, chart, "--version", version,
            *self._values_args(values),
        ], capture_output=True, text=True, timeout=900)
        if result.returncode != 0:
            raise HelmInstallError(release=release, error=result.stderr)
```

### T003 — `tools/lib/kubeconfig_merger.py`

Read `~/.kube/config`, back up to `~/.kube/config.bak.<unix-ts>`, merge a new context under `<cluster>` name, write atomically (write to temp + rename).

```python
# tools/lib/kubeconfig_merger.py
import json, shutil, time
from pathlib import Path

class KubeconfigMerger:
    def __init__(self, log):
        self.log = log
        self.path = Path.home() / ".kube" / "config"

    def merge(self, cluster_name: str, kubeconfig_yaml: str) -> None:
        if self.path.exists():
            backup = self.path.with_suffix(f".bak.{int(time.time())}")
            shutil.copy2(self.path, backup)
            self.log.info(step="kubeconfig_backup", path=str(backup))
        # parse existing + new, merge contexts, write
        ...
```

### T004 — `tools/bootstrap_cluster.py` (skeleton)

```python
#!/usr/bin/env python3
import argparse, json, sys, time
from pathlib import Path

from tools.lib.log import StructuredLogger
from tools.lib.talos_client import TalosClient, TalosApplyError
from tools.lib.helm_client import HelmClient, HelmInstallError
from tools.lib.kubeconfig_merger import KubeconfigMerger
from tools.lib.secret_loader import SecretLoader


PHASES = ["talos", "k3s", "helm", "kubeconfig"]

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cluster", required=True)
    parser.add_argument("--phase", default="all", choices=["all"] + PHASES)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    log = StructuredLogger(f"bootstrap-{args.cluster}", verbose=args.verbose)
    secrets = SecretLoader(log)

    output_json = Path(f"clusters/{args.cluster}/output.json")
    if not output_json.exists():
        log.error(step="read_output_json", error=f"{output_json} not found",
                  resolution=f"Run tofu apply -chdir=clusters/{args.cluster} first",
                  jq_filter='. | select(.step=="read_output_json")')
        return 1
    cluster = json.loads(output_json.read_text())

    phases = PHASES if args.phase == "all" else [args.phase]
    for phase in phases:
        try:
            if phase == "talos":
                run_talos_phase(cluster, log)
            elif phase == "k3s":
                run_k3s_phase(cluster, log)
            elif phase == "helm":
                run_helm_phase(cluster, log)
            elif phase == "kubeconfig":
                run_kubeconfig_phase(cluster, log)
        except (TalosApplyError, HelmInstallError) as e:
            log.error(step=phase, error=str(e),
                      resolution=phase_resolution_hints(phase),
                      jq_filter=f'. | select(.step=="{phase}")')
            return 2

    log.info(step="complete", cluster=cluster["cluster_name"])
    return 0
```

### T005 — Cilium Helm release

```python
def cilium_release(cluster: dict) -> dict:
    return {
        "release": "cilium",
        "chart": "cilium/cilium",
        "version": "1.16.x",   # pin at implementation time
        "namespace": "kube-system",
        "values": {
            "kubeProxyReplacement": "true",
            "gatewayAPI": {"enabled": True},
            "ipv4NativeRoutingCIDR": "10.0.0.0/8",
            "ipam": {
                "mode": "cluster-pool",
                "operator": {
                    "clusterPoolIPv4PodCIDRList": cluster["pod_cidr"]
                }
            },
            "hubble": {"enabled": False},   # deferred to downstream spec
        }
    }
```

### T006 — kube-vip Helm release

```python
def kube_vip_release(cluster: dict) -> dict:
    return {
        "release": "kube-vip",
        "chart": "kube-vip/kube-vip",
        "version": "1.2.1",
        "namespace": "kube-system",
        "values": {
            "interface": "eth0",   # inside the VM; vnet0 from the host's POV
            "leaderElection": True,
            "services": {
                "etcd": {"enabled": False},  # k3s manages its own etcd
            },
            "controlPlane": {
                "enabled": True,
                "hostPort": 6443,
            },
        }
    }
```

### T007 — Helm phase with structured error

```python
def run_helm_phase(cluster: dict, log: StructuredLogger) -> None:
    helm = HelmClient(log)
    releases = [cilium_release(cluster), kube_vip_release(cluster)]
    for r in releases:
        log.info(step="helm_install", release=r["release"], version=r["version"])
        try:
            helm.install_or_upgrade(
                release=r["release"],
                chart=r["chart"],
                namespace=r["namespace"],
                values=r["values"],
                version=r["version"],
            )
        except HelmInstallError as e:
            raise   # let main() catch and emit structured error
```

### T008 — pytest fixtures

```python
# tools/tests/test_bootstrap_cluster.py
def test_helm_phase_aborts_on_first_failure(monkeypatch, cluster):
    """If Cilium install fails, kube-vip is NOT attempted."""
    ...

def test_kubeconfig_backup_created(tmp_path, monkeypatch, cluster):
    """Existing ~/.kube/config is backed up before merge."""
    ...

def test_secrets_never_logged(monkeypatch, caplog, cluster):
    """cf_api_token, proxmox_token_secret never appear in any log line."""
    ...

def test_structured_error_includes_resolution_hint(cluster):
    """Error JSON has resolution and jq_filter fields."""
    ...
```

### T009 — `Makefile` + docs

```makefile
bootstrap-cluster:
	@python tools/bootstrap_cluster.py --cluster $${CLUSTER:-cicd} --phase all
```

Document in the WP prompt's "How to run" section.

### T010 — Lint + test

```bash
pytest tools/tests/test_bootstrap_cluster.py
mypy --strict tools/
ruff check tools/
```

## Acceptance Criteria

- [ ] `python tools/bootstrap_cluster.py --cluster cicd --phase all` brings up k3s and reports `cilium` + `kube-vip` as `deployed`
- [ ] `kubectl --context cicd get nodes` returns 2 Ready
- [ ] `helm list -A` shows cilium and kube-vip as `deployed`
- [ ] Cilium pod networking passes a `kubectl exec` reachability test between two Pods on different nodes
- [ ] kube-vip VIP is reachable from outside the cluster (`nc -zv 10.0.0.30 6443`)
- [ ] If Cilium install fails (mocked), kube-vip is NOT attempted (test)
- [ ] If kube-vip install fails, kubeconfig is NOT merged (test)
- [ ] kubeconfig is backed up before merge (test)
- [ ] Secret values never appear in any log line (test)
- [ ] Re-running the script with `--phase all` is a no-op in <60 s
- [ ] pytest + mypy + ruff all pass

## Technical context

- **Python**: ≥3.11
- **External**: `talosctl`, `helm`, `kubectl`, `ssh` on PATH
- **K3s**: v1.34.x with `--disable=traefik` (Traefik is installed via a separate Helm release, not bundled)

## How to run

```bash
python tools/bootstrap_cluster.py --cluster cicd --phase all
```