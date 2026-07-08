# Plan — Envoy Gateway API + smoke tests (GitLab-readiness)

> **Status**: APPROVED 2026-07-08 — proceeding to implementation.
> **Captured**: 2026-07-08.
> **Author**: agent (read-only design pass; no code touched).
> **Owner**: operator.
> **Reference blueprint**: `/home/bruj0/projects/k8s-cicd/blueprint/docs/` —
> `index.md` + `prereqs.md` + `phase-2.md` (13-step pipeline).
>
> **Operator decisions (received 2026-07-08)**:
> 1. **Both clusters in scope** — `cicd` and `apps` both install
>    Envoy Gateway + run smoke tests.
> 2. **GatewayClass name = `envoy`** (chart default; no override).
> 3. **Pin the upstream standard Gateway API CRDs** as a separate
>    bootstrap phase (not as a chart dependency).
>
> **WP00 (context7-auto-research) — completed 2026-07-08**:
> full evidence in
> [`specs/001-build-a-kubernetes-k3s-cluster-on-proxmo/research-log-v8.json`](../specs/001-build-a-kubernetes-k3s-cluster-on-proxmo/research-log-v8.json).
> Key pins: chart `oci://docker.io/envoyproxy/gateway-helm` v1.8.2;
> standard CRDs URL `v1.6.0/standard-install.yaml`.

## 1. Why this exists

The two clusters (`cicd`, `apps`) currently have:

- **Cilium 1.16.1** with `gatewayAPI.enabled=true` — Cilium's
  GAMMA support is wired up, but no Gateway API **implementation**
  (Envoy Gateway, Contour, NGINX Gateway Fabric, …) is installed.
  Cilium alone provides the *CRD awareness* (so a `GatewayClass`
  admission-controller webhook will recognise the standard CRDs),
  but it does not implement a `GatewayClass`.
- **Traefik** as the in-cluster `IngressClass` default (installed
  via the k3s `HelmChartConfig` and then re-applied through the
  bootstrap). Traefik is the canonical ingress but does **not**
  speak the Gateway API (`Gateway`, `HTTPRoute`,
  `BackendTrafficPolicy`) — only `Ingress`.
- **proxmox-csi-plugin 0.5.9** installed, but its `lvm-thin`
  StorageClass has **never been exercised end-to-end** — no
  test-phase has ever issued a `kubectl create -f pvc.yaml` and
  asserted that it `Bound` against a `proxmox://bigbertha/<vmid>`
  volume handle. The current "success criteria" for the helm
  phase (`kubectl get pods -n kube-system`) does not include a
  real storage round-trip.

The `/home/bruj0/projects/k8s-cicd/blueprint` (the sibling project
at `k8s-cicd/blueprint`, not this repo) uses **Envoy Gateway as
the Gateway API implementation** for everything in `phase-2.md`:
the GitLab chart's `gateway-helm` sub-chart installs it, and the
chart's `Gateway` + four `HTTPRoute` resources (`gitlab`,
`registry`, `kas`, `minio`) are routed through it. Storage is
`local-path` for the kind cluster but the GitLab chart (which is
what we'd run here) also tolerates a real CSI provisioner — which
is exactly what `proxmox-csi-plugin` is.

So the two pieces that bridge us to "ready for GitLab" are:

1. **Envoy Gateway** — the Gateway API implementation the GitLab
   chart talks to. Without it, `kubectl get gatewayclass` returns
   `cilium` (or empty) and the GitLab chart's `Gateway` resource
   will not be programmed.
2. **Real smoke tests** — one that proves Envoy Gateway is
   routing (L7 round-trip through a temporary `HTTPRoute`), and
   one that proves proxmox-csi-plugin can provision a `PersistentVolumeClaim`
   and survive a `kubectl delete pod` (so we know a later
   GitLab stateful workload — Gitaly, PostgreSQL — can rely on
   it).

## 2. Goals & non-goals

### Goals

1. Add **Envoy Gateway** as a new helm release in
   `tools/lib/helm_client.py::remaining_releases`, pinned to a
   chart version that supports k8s 1.34 (the live cluster is
   1.36.2+k3s1, so we have headroom).
2. Wire Envoy Gateway into the **PHASES** tuple as a new
   post-helm sub-phase (`gateway_smoke`) so a re-runnable
   bootstrap can land it independently.
3. Add **two real smoke tests** as new bootstrap phases:
   - `gateway_smoke` — deploys a temporary `GatewayClass` (if
     Cilium GAMMA does not own it), a `Gateway`, a tiny `HTTPRoute`
     pointing at a `curl`-able echo pod, then asserts
     `kubectl get httproute` reports `Accepted=True,
     ResolvedRefs=True` and a `curl` against the Gateway's
     `status.addresses[0]` returns the echo body.
   - `csi_smoke` — creates a `PersistentVolumeClaim` against
     `proxmox-lvm-thin`, asserts it `Bound`, writes a marker
     file, deletes the test pod, re-creates it, asserts the file
     is still there (proves CSI `NodePublishVolume` survived the
     pod churn). Deletes the PVC at the end so the cluster is
     clean for GitLab.
4. Update the skill (`SKILL.md`) to document both additions,
   including live-host gotchas discovered when we first apply
   them.
5. Update `tools/versions.lock.yaml` with the new chart pins and
   a `cross_check` entry recording the live-host verification.
6. Pin all of the above with **tests** (helm shape, phase
   ordering, smoke-test markers in the skill) so a regression
   fails CI.
7. After successful live-host run, the cluster is ready for the
   `phase-2.md` GitLab chart install — the chart's
   `Gateway`/`HTTPRoute` resources will find a
   `GatewayClass=envoy` and the chart's `persistence.size` values
   will bind against `proxmox-lvm-thin`.

### Non-goals

- **Do NOT install the GitLab chart.** That is a separate,
   larger task (the chart pulls 30+ sub-charts and needs its own
   runbook). This plan ends at "cluster is GitLab-ready" — the
   GitLab install itself happens in a follow-up plan.
- **Do NOT switch the default `IngressClass` from Traefik to
  Envoy.** Traefik stays the default; Envoy Gateway is an
  additional `GatewayClass` (`envoy`). The GitLab chart's
  `gatewayClass` value can be set to `envoy` at chart-install
  time without affecting Traefik ingress consumers.
- **Do NOT remove `proxmox-cloud-controller-manager`.** It is
  still required for `providerID` + topology labels (the CSI
  plugin depends on them). The known `ContainerCreating` issue
  (credentials URL `10.0.0.1:8006` unreachable from inside the
  cluster — see `cluster-state.md §14.1`) is **out of scope** for
  this plan; it is a separate work item.
- **Do NOT change the k3s version pin or the Cilium values.**
  Those are stable.

## 3. Design — what changes

### 3.1 The chart pick

| Concern | Choice | Why |
|---|---|---|
| Gateway API implementation | **Envoy Gateway** | What `phase-2.md` uses; what the GitLab chart's `gateway-helm` sub-chart installs. |
| Chart repo | `oci://docker.io/envoyproxy/gateway-helm` | Canonical upstream OCI ref per WP00 context7 snippet. **Corrected** from the original plan (which had `gateway-helm-charts/gateway-envoy`, a non-existent path). |
| Chart version | **v1.8.2** (latest stable, released 2026-07-01) | Live cluster runs k3s 1.36.2+k3s1; Envoy Gateway v1.8.x supports k8s 1.28+ (well within range). |
| Standard CRDs | **Pinned separately at v1.6.0** (operator decision: "pin them") via `kubectl apply --server-side -f https://github.com/kubernetes-sigs/gateway-api/releases/download/v1.6.0/standard-install.yaml`. | Chart ships them too, but the bootstrap **disables** the chart's CRD install (`crds.enabled=false` + `crds.gatewayAPI.safeUpgradePolicy.enabled=false`) and applies the pinned URL itself — that way a CRD drift surfaces as a `kubectl diff`, not as a silent helm upgrade. |
| Cilium GAMMA | Keep `gatewayAPI.enabled=true` on Cilium; Envoy Gateway is the implementation, Cilium is the GAMMA L4 awareness. | Already pinned; WP00 confirms they coexist (Cilium GAMMA only validates the standard CRDs, doesn't compete for GatewayClass admission). |
| GatewayClass name | `envoy` (default chart value; operator decision: "use envoy"). | Matches what the GitLab chart expects (`gatewayClassName=envoy` in its templates). |
| Service type | `ClusterIP` (chart default) | No LoadBalancer provisioner on Proxmox+k3s; same constraint as blueprint phase-2.md §14 (kind NodePort gotcha). The smoke test curls the in-cluster ClusterIP, not a public hostname. |

### 3.2 The new PHASES

The 7-phase tuple becomes **8 phases**. The new phase slots in
between `helm` and `kubeconfig` because it needs the apiserver
tunnel that the `helm` phase already opens:

```python
PHASES: tuple[str, ...] = (
    "cloudinit",
    "install_k3s",
    "k3s",
    "gateway_crds",     # NEW: apply pinned standard Gateway API CRDs
    "helm",             # extended to also install envoy-gateway
    "gateway_smoke",    # NEW: envoy-gateway L7 round-trip
    "kubeconfig",
    "csi_smoke",        # NEW: proxmox-csi lvm-thin round-trip
    "host_ports",
    "externalname",
)
```

→ **Final shape: 10 phases**. Both new smoke phases (`gateway_smoke`,
`csi_smoke`) run on BOTH `cicd` and `apps` (operator decision).

The `--phases` flag accepts them explicitly:

```bash
python -m tools.bootstrap_cluster --cluster cicd \
  --phases cloudinit,install_k3s,k3s,gateway_crds,helm,gateway_smoke,kubeconfig,csi_smoke,host_ports
```

### 3.3 Code changes (file-by-file)

#### `tools/lib/helm_client.py`

Add a new module-level function `gateway_releases()` that returns
the Envoy Gateway Helm release. Why a separate function (not
appending to `remaining_releases`): Envoy Gateway installs
**CRDs** in its own namespace and the operator must
explicitly want it. Keeping it opt-in via a separate function
matches the pattern of `first_two_releases` (required) vs
`remaining_releases` (also required, but logically separate).

```python
def gateway_releases() -> list[HelmRelease]:
    """WP07: Envoy Gateway as the GatewayClass=envoy implementation.

    Pinned to v1.8.2 in tools/versions.lock.yaml (see cross_check
    entry 'envoy_gateway_install_2026_07_08' added after the first
    live-host apply). Chart OCI ref + values verified via
    context7-auto-research (research-log-v8.json).
    """
    return [
        HelmRelease(
            name="envoy-gateway",
            chart="oci://docker.io/envoyproxy/gateway-helm",
            namespace="envoy-gateway-system",
            version="v1.8.2",
            values={
                # The chart installs the standard Gateway API CRDs
                # by default. The bootstrap installs them itself
                # (with --server-side, against a pinned URL) so we
                # disable the chart's install path and disable the
                # safe-upgrade policy to avoid conflicts.
                "crds.enabled": "false",
                "crds.gatewayAPI.safeUpgradePolicy.enabled": "false",
                # Pin the controller name explicitly so a chart
                # default change surfaces as a contract-test
                # failure.
                "config.envoyGateway.gateway.controllerName": (
                    "gateway.envoyproxy.io/gatewayclass-controller"
                ),
                # ClusterIP (default; explicit because we don't
                # have a LoadBalancer provisioner).
                "service.type": "ClusterIP",
                # Single replica is correct for a 1-CP cluster;
                # HPA is off by default.
                "deployment.replicas": "1",
            },
        ),
    ]
```

#### `tools/bootstrap_cluster.py`

Three edits:

1. Import `gateway_releases` from `helm_client`.
2. Add a new phase `gateway_crds` that runs BEFORE the helm phase
   and applies the pinned standard CRDs (idempotent
   `kubectl apply --server-side`).
3. Extend `_run_helm` to call `client.install_or_upgrade(gateway_releases())`
   after `remaining_releases` (so a single `helm` invocation
   lands everything atomically).
4. Add two new `_run_*` smoke functions and wire them into the
   dispatcher:

```python
def _run_gateway_smoke(
    state: State, cluster_dir: Path, topo: ClusterTopology
) -> None:
    """WP07: real smoke test for Envoy Gateway.

    Deploys a tiny Gateway + HTTPRoute + echo pod via inline YAML
    written to cluster_dir/manifests/_smoke/envoy.yaml, then curls
    the Gateway's status.addresses[0] and asserts the echo body
    comes back. Cleans up at the end.

    Idempotency: re-running detects the existing test namespace
    + echo pod and only re-asserts the curl. Skips re-apply.

    Requires:
      - Envoy Gateway installed (helm phase ran).
      - Cilium gatewayAPI.enabled=true (already true).
      - A reachable apiserver via the existing PveSshProxy tunnel.
    """
    ...
    state.phases_done.add("gateway_smoke")


def _run_csi_smoke(
    state: State, cluster_dir: Path, topo: ClusterTopology
) -> None:
    """WP07: real smoke test for proxmox-csi-plugin.

    Creates a PVC against storageclass=proxmox-lvm-thin, asserts
    Bound, creates a pod that writes a marker file into the PV,
    deletes the pod, re-creates it, asserts the file survived.
    Cleans up at the end.

    Requires:
      - proxmox-csi-plugin installed + StorageClass=proxmox-lvm-thin
        default (already true on the live cluster).
      - A reachable apiserver via ~/.kube/config (kubeconfig
        phase must have run).
    """
    ...
    state.phases_done.add("csi_smoke")
```

The dispatcher in `bootstrap()` maps phase name → function via a
small dict (or the existing `if/elif` chain, kept identical for
risk minimisation).

#### `tools/versions.lock.yaml`

Add:

```yaml
additional_dependencies:
  - name: gateway-envoy
    version: "<pinned during WP00>"
    rationale: >
      Gateway API implementation for the GitLab chart (which
      sub-installs the same chart as gateway-helm). Pinned so
      a fresh cluster has a known GatewayClass=envoy.
    source: "context7-auto-research on 2026-07-08"
  - name: gateway-api-crds
    version: "v1.2.x"
    rationale: >
      Standard-channel Gateway API CRDs (the upstream set, not
      the chart-shipped Envoy extensions). Envoy Gateway chart
      ships its own CRDs but the standard-channel set is required
      for kubectl to recognise Gateway/HTTPRoute kinds cluster-wide.
    source: "context7-auto-research on 2026-07-08"

cross_check:
  envoy_gateway_install_2026_07_0X: >
    TBD after live-host run.
  csi_smoke_roundtrip_2026_07_0X: >
    TBD after live-host run.
```

#### `tools/tests/test_bootstrap_cluster.py`

Update `test_list_phases_returns_all_seven` →
`test_list_phases_returns_all_nine` (new name + new expected
list). Add `test_gateway_smoke_phase_is_after_helm` and
`test_csi_smoke_phase_is_after_kubeconfig` to lock the ordering.

#### `tools/tests/test_remaining_releases.py`

Add `test_gateway_releases_returns_envoy_gateway` that asserts:
- `gateway_releases()` returns exactly one release.
- The chart is the OCI one (`oci://gateway-helm-charts/gateway-envoy`).
- The namespace is `envoy-gateway-system`.
- The version string is present (not None, not empty).

#### `tools/tests/test_agent_skill.py`

Add two new tests:
- `test_skill_documents_envoy_gateway_phase` — pins that the
  skill lists Envoy Gateway as a release + mentions the chart
  version pin location.
- `test_skill_documents_csi_smoke_phase` — pins that the skill
  describes the PVC round-trip pattern.

### 3.4 Doc changes

- **`SKILL.md`** — append `Step 4b` (envoy-gateway + gateway
  smoke) and `Step 4c` (csi smoke) sections, each with the same
  shape as `Step 4a` (recipe + idempotency + live-host gotchas).
  Update `Step 4.0` (single command) to mention the two new
  phases; update `Step 4.3` (success criteria) to require
  `kubectl get gatewayclass` shows `envoy` and a fresh PVC binds
  against `proxmox-lvm-thin`.
- **`docs/cluster-state.md`** — add `§6.2 Envoy Gateway API` (chart
  version, GatewayClass name, namespace), and add a new
  `§14.3 Envoy Gateway CRD install order` (live-host gotcha
  reserved for the first-apply lesson). Update `§15 How to re-run
  any phase` with the two new phase names.
- **`docs/architecture.md`** — if the SS3 subsystem boundary
  expanded (it did — two new phases), update the table.

## 4. Implementation order (the WP00 → WP07 contract)

1. **WP00 — context7-auto-research** (the gate from SKILL.md
   Step 0). Confirm the live chart version + CRD shape. The
   `tool context7-auto-research` script does this. Output: a
   committed `research-log-v1.json` next to `spec.md`.
2. **Code first** — write the helm release + new phases +
   tests. Don't run on the live cluster yet.
3. **Lint + tests** — `python -m ruff check tools/`,
   `mypy --strict --explicit-package-bases tools/`,
   `python -m pytest tools/tests/ -v`. All green before any
   live-host touch.
4. **Live-host apply** — `python -m tools.bootstrap_cluster
   --cluster cicd --phases helm,gateway_smoke,kubeconfig,csi_smoke`.
   Watch for the gotcha we don't know yet; capture it.
5. **Capture the live-host gotcha** — if the first apply
   surfaced a failure mode (chart needs `--set
   deployment.envoyGateway.gatewayClass.enabled=true`,
   Cilium GAMMA conflict with Cilium L7 policy, etc.), add a
   `Step 4b.X` gotcha + the pytest that pins the fix.
6. **Apply to `apps` cluster** — same recipe, different cluster
   name. apps cluster runs `csi_smoke` only if the user
   intends to deploy stateful apps (the GitLab chart's PG/Redis
   go on cicd, not apps).
7. **`cross_check` entries in `versions.lock.yaml`** — record
   both `envoy_gateway_install_<date>` and
   `csi_smoke_roundtrip_<date>`.
8. **Commit + push** — Conventional Commits format.
   Suggested message: `feat(ss3): add envoy gateway + smoke
   tests for envoy + csi (gitlab-readiness)`.

## 5. Risks & mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| Envoy Gateway chart conflicts with Cilium's GAMMA awareness (both register `GatewayClass` admission webhooks) | Low | Cilium GAMMA only validates CRDs; Envoy Gateway is the implementation. They do not overlap. If we hit a conflict, disable Cilium GAMMA (`gatewayAPI.enabled=false`) and rely on the upstream CRDs only. |
| proxmox-csi-plugin StorageClass is not actually default on one of the clusters (operator-side drift) | Low | The phase asserts `kubectl get sc proxmox-lvm-thin -o jsonpath='{.metadata.annotations.storageclass\.kubernetes\.io/is-default-class}'` is `true` before creating the PVC. If false, fail fast with a clear BootstrapError pointing at `tools/lib/helm_client.py::remaining_releases`. |
| The Gateway's `status.addresses[0]` is a `LoadBalancer` IP that isn't reachable from inside the cluster (the kind-cluster `phase-2.md` gotcha) | Medium | The Proxmox+k3s clusters do NOT have a LoadBalancer provisioner; Envoy Gateway defaults to a `ClusterIP` `Service` for the data plane. The smoke test should NOT rely on a LoadBalancer IP — curl from the same node (or via `kubectl port-forward` to the data-plane Service) instead. WP00 will pin this exact pattern. |
| The ephemeral smoke pods survive a re-run and clutter the cluster | Low | Each smoke phase names its resources with the `proxmox-k8s-cicd-smoke-` prefix and checks for existing resources before re-applying; cleanup is always best-effort with `kubectl delete -f` of the YAML manifest at phase exit. |
| The `gateway_smoke` phase runs on a cluster where the helm phase hasn't installed Envoy Gateway yet (operator runs phases out of order) | Medium | The phase asserts `kubectl get ns envoy-gateway-system` exists before deploying the test Gateway; otherwise fails fast. |
| Cilium 1.16.1's `gatewayAPI.enabled=true` flag also requires the Cilium operator to be running with `--set operator.replicas=1` for the GAMMA webhook to be reachable | Low | The cilium chart already runs as a DaemonSet with an operator; no additional flag needed for our topology (1 CP + 1 worker). |

## 6. Acceptance criteria

The plan is "done" when **all** of the following are true on
the live `cicd` cluster:

1. `kubectl --context cicd get gatewayclass` lists `envoy`.
2. `kubectl --context cicd -n envoy-gateway-system get pods` shows
   the envoy controller + data plane `Running`.
3. A temporary `HTTPRoute` pointing at an echo pod is accepted
   (`kubectl get httproute -n proxmox-k8s-cicd-smoke -o
   jsonpath='{.status.parents[0].conditions[?(@.type==
   "Accepted")].status}'` returns `True`).
4. `curl http://<gateway-data-plane-clusterip>/` returns the
   echo body.
5. A `PersistentVolumeClaim` against `proxmox-lvm-thin` reaches
   `Bound` status within 60 seconds.
6. After `kubectl delete pod` on the test pod, a re-created
   pod can read the marker file written before the delete.
7. `python -m tools.bootstrap_cluster --cluster cicd --phases
   cloudinit,install_k3s,k3s,helm,gateway_smoke,kubeconfig,csi_smoke,host_ports`
   exits 0.
8. `python -m tools.bootstrap_cluster --cluster cicd --phases
   all` exits 0 in <60 seconds (idempotent re-run).
9. `ruff check tools/` + `mypy --strict --explicit-package-bases
   tools/` + `pytest tools/tests/ -v` all green.
10. `tools/versions.lock.yaml::cross_check` has fresh entries
    for `envoy_gateway_install_<date>` and
    `csi_smoke_roundtrip_<date>`.

## 7. Out of scope (explicitly)

- The GitLab chart install itself (Phase 11 in `phase-2.md`).
  That is a 30+ sub-chart operation with its own PG/Redis/MinIO
  stack and deserves its own plan.
- Removing the in-cluster `Traefik` `IngressClass`. The GitLab
  chart can declare its own `GatewayClass` (`envoy`) without
  affecting Traefik consumers; removing Traefik would also
  remove the bootstrap's "default ingress" for any pre-GitLab
  workloads.
- Fixing the known `proxmox-cloud-controller-manager`
  `ContainerCreating` issue from `cluster-state.md §14.1`. The
  CSI plugin works without CCM being healthy (we tested on
  2026-07-08 with CCM pods unready — see the existing cluster
  state).
- Bumping Cilium past 1.16.1. The current pin works.
- Bumping Envoy Gateway past the WP00-determined version. The
  current pin works once we land it.

## 8. Open questions — RESOLVED 2026-07-08

1. **apps cluster smoke scope**: **YES, both clusters**
   (operator confirmed).
2. **GatewayClass naming**: **`envoy`** (operator confirmed;
   matches chart default + GitLab chart's expectation).
3. **Pin upstream Gateway API CRDs separately**: **YES, pin
   them** (operator confirmed; landed as a new
   `gateway_crds` phase).

Implementation proceeds per §4.