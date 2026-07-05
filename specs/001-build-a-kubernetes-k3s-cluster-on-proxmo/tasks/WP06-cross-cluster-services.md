---
work_package_id: "WP06"
title: "Cross-Cluster Services + Apps Bootstrap"
lane: "doing"
dependencies:
  - WP03
  - WP05
subsystem: "SS3 + SS2 (cross-cluster wiring)"
misfits_addressed:
  - M3 (apps cluster does not collide with cicd)
abstract_components:
  - clusters/apps/manifests/cicd-system/externalname.yaml
  - clusters/apps/manifests/cicd-system/kustomization.yaml
  - tools/bootstrap_cluster.py (extended --cluster apps branch)
tdd_red_clean: true
build_validated: true
agent: "spec-bridge-implement"
history:
  - timestamp: "2026-07-05T14:32:38+00:00"
    lane: doing
    agent: spec-bridge-implement
    action: started implementation
---

# WP06 — Cross-Cluster Services + Apps Bootstrap

## Goal

Author the ExternalName Services manifest (`clusters/apps/manifests/cicd-system/externalname.yaml`) declaring four ExternalName Services (`gitlab`, `registry`, `minio`, `minio-console`). Extend `bootstrap_cluster.py` to apply this manifest when invoked with `--cluster apps`. Verify apps → cicd reachability.

## Execution constraints

- Product code and tests: only in `$WORKTREES_DIR/001-build-a-kubernetes-k3s-cluster-on-proxmo-WP06/`
- Do not merge to `$TARGET_BRANCH` until `spec-bridge-merge` after accept

## Subtasks

### T000 — Version compatibility matrix (gate before any other subtask)

Before scaffolding anything, build a per-WP version matrix:

1. **Identify every external dependency this WP will touch.** For WP06: Kubernetes ExternalName (stable API since k8s 1.0), CoreDNS (comes with k3s), kustomize (used to bundle the ExternalName manifest), kubectl.
2. **For each dependency, run `context7-auto-research`** (load `.agents/skills/context7-auto-research/SKILL.md` first) to confirm:
   - The **latest stable release** version of kustomize (and confirm the YAML syntax used in `kustomization.yaml` matches the kustomize version's expected schema).
   - That the apps cluster's CoreDNS upstream config (inherited from /etc/resolv.conf on the host) works against the version of CoreDNS that ships with k3s 1.34.x.
3. **Cross-check compatibility**:
   - ExternalName Service ↔ CoreDNS in apps cluster (ExternalName is core k8s API; no compat risk)
   - kustomize ↔ Kubernetes manifests (kustomize 5.x is the modern standard; confirm the manifest syntax is v1beta1-compatible)
4. **Document the result** in `clusters/apps/manifests/versions.lock.yaml`:
   ```yaml
   dependencies:
     - name: kubernetes-external-name
       version: "stable (core API)"
     - name: kustomize
       version: ">= 5.x"
     - name: kubectl
       version: "matches k3s minor (1.34)"
   cross_check:
     externalname_coredns: "compatible"
     kustomize_manifests: "v1beta1-compatible"
   ```
5. **The agent must NOT proceed** to T001+ until this file exists and is reviewed.

This subtask is the canonical "T000" step for every WP in this feature. Repeat it in every WP, scoped to that WP's dependencies.

### T001 — `clusters/apps/manifests/cicd-system/externalname.yaml`

```yaml
# ExternalName Services in the apps cluster pointing at cicd cluster hostnames.
# Resolution flow: apps CoreDNS -> ExternalName (gitlab.intranet) -> PowerDNS (10.0.0.3) -> cicd VIP 10.0.0.30
apiVersion: v1
kind: Service
metadata:
  name: gitlab
  namespace: cicd-system
spec:
  type: ExternalName
  externalName: gitlab.intranet
  ports:
    - name: http
      port: 80
      targetPort: 80
    - name: ssh
      port: 22
      targetPort: 22
---
apiVersion: v1
kind: Service
metadata:
  name: registry
  namespace: cicd-system
spec:
  type: ExternalName
  externalName: registry.intranet
  ports:
    - name: https
      port: 443
      targetPort: 443
---
apiVersion: v1
kind: Service
metadata:
  name: minio
  namespace: cicd-system
spec:
  type: ExternalName
  externalName: minio.intranet
  ports:
    - name: https
      port: 9000
      targetPort: 9000
---
apiVersion: v1
kind: Service
metadata:
  name: minio-console
  namespace: cicd-system
spec:
  type: ExternalName
  externalName: minio-console.intranet
  ports:
    - name: https
      port: 9001
      targetPort: 9001
```

### T002 — `clusters/apps/manifests/cicd-system/kustomization.yaml`

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: cicd-system
resources:
  - externalname.yaml
```

### T003 — Extend `bootstrap_cluster.py` with `--cluster apps` branch

```python
def run_externalname_phase(cluster: dict, log) -> None:
    if cluster["cluster_name"] != "apps":
        return   # only apps cluster gets cross-cluster Services
    log.info(step="apply_externalname", manifest="clusters/apps/manifests/cicd-system/")
    result = subprocess.run([
        "kubectl", "--context", cluster["cluster_name"],
        "apply", "-k", "clusters/apps/manifests/cicd-system/",
    ], capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise ManifestApplyError(result.stderr)

def run_verify_cross_cluster(cluster: dict, log) -> None:
    if cluster["cluster_name"] != "apps":
        return
    log.info(step="verify_cross_cluster")
    # Apply a test Pod that curls gitlab.cicd-system.svc.cluster.local
    # (Requires that cicd has at least a simple HTTP service running; for spec 001
    # this can be a `whoami` Deployment on cicd as a stand-in.)
    ...
```

Add `--phase externalname` to PHASES list.

### T004 — Smoke test

Author a test Pod in `clusters/apps/manifests/test-pod.yaml` (gitignored or kept for testing):

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: cross-cluster-test
  namespace: default
spec:
  containers:
    - name: curl
      image: curlimages/curl:8.7.1
      command: ["sleep", "300"]
  restartPolicy: Never
```

Then:

```bash
kubectl --context apps exec cross-cluster-test -- \
  curl -sf http://gitlab.cicd-system.svc.cluster.local/-/health
```

### T005 — Verify apps CoreDNS upstream

```bash
kubectl --context apps get configmap -n kube-system coredns -o yaml
```

Assert the upstream nameservers include `10.0.0.3`. If not, patch via `kubectl --context apps -n kube-system edit configmap coredns`.

### T006 — `verify-apps-to-cicd` smoke test

The WP prompt documents the manual verification:
1. Confirm `kubectl --context apps get svc -n cicd-system` shows 4 ExternalName Services
2. Confirm `kubectl --context apps exec <test-pod> -- nslookup gitlab.intranet 10.0.0.3` returns the cicd VIP (10.0.0.30)
3. Confirm `kubectl --context apps exec <test-pod> -- curl -sf http://gitlab.cicd-system.svc.cluster.local` returns 200/302

### T007 — Documentation update

Update `docs/architecture.md` (cross-link from spec.md, plan.md, research.md) to document the cross-cluster wiring.

## Acceptance Criteria

- [ ] `kubectl --context apps get svc -n cicd-system` shows 4 ExternalName Services
- [ ] `kubectl --context apps exec <test-pod> -- nslookup gitlab.intranet 10.0.0.3` returns 10.0.0.30
- [ ] `kubectl --context apps exec <test-pod> -- curl -sf http://gitlab.cicd-system.svc.cluster.local` returns 200/302 within 5 s
- [ ] `kubectl --context apps delete namespace cicd-system` removes the 4 Services cleanly
- [ ] apps CoreDNS upstream includes `10.0.0.3`
- [ ] `python tools/bootstrap_cluster.py --cluster apps --phase all` brings up the apps cluster end-to-end (k3s, all Helm releases, ExternalName manifest applied)
- [ ] `docs/architecture.md` is updated with a cross-cluster wiring section

## Technical context

- **Kubernetes**: standard ExternalName + CoreDNS
- **apps CoreDNS**: inherits upstream nameservers from the host's `/etc/resolv.conf`, which uses 10.0.0.3 (per FR-034)

## How to run

```bash
python tools/bootstrap_cluster.py --cluster apps --phase all
```