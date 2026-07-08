# Cilium installation on the k3s clusters

> **Status**: APPROVED 2026-07-08.
> **Canonical upstream reference**: <https://docs.cilium.io/en/stable/installation/k3s/>
> **Live cluster state**: cilium 1.16.1 + cilium-operator 1/1 on every node of both cicd and apps clusters.
>
> This document is the operator-facing distillation of the canonical
> Cilium-on-k3s recipe, **adapted for the proxmox-k8s-cicd pipeline**
> (Ubuntu 24.04 LTS, single-PVE-host, single-CP clusters, no kube-vip).

## Why this exists

The pipeline installs Cilium as the cluster's CNI, network-policy
enforcer, kube-proxy replacement, and Gateway API GAMMA layer. We
follow the canonical Cilium-on-k3s recipe with three
pipeline-specific overrides that are NOT in the upstream guide:

1. **No `kube-vip`** — the cluster runs single control-plane, so
   agents join on the CP host IP directly. The canonical recipe
   assumes a multi-node HA cluster with a kube-vip VIP; we have
   neither.
2. **Non-overlapping pod/svc/dns CIDRs** — `k3s-io/k3s#4627`. The
   canonical recipe uses k3s defaults (10.42/10.43) which overlap
   our 10.0.0.0/8 host LAN. We pin 172.16/172.17 (cicd) and
   172.20/172.21 (apps).
3. **Cilium cgroup host root is `/sys/fs/cgroup`** — the canonical
   recipe assumes a systemd cgroup layout where the cilium-agent
   Pod can mount a virtualised `/run/cilium/cgroupv2` and attach
   BPF cgroup hooks. On a k3s host running systemd v255 the
   virtualised mount doesn't reach the kubepods cgroup; we set
   `cgroup.hostRoot=/sys/fs/cgroup` so the BPF hooks land at the
   actual host cgroup root where k3s pods live.

If you re-bootstrap a cluster from scratch, the recipe below is
what `tools/lib/helm_client.py::first_two_releases` (for cilium)
and `tools/lib/k3s_installer.py::_SERVER_BASE_FLAGS` (for k3s)
together encode.

## 1. The canonical recipe (verbatim from upstream)

The upstream recipe, for reference, is:

```bash
# Master node:
curl -sfL https://get.k3s.io | \
  INSTALL_K3S_EXEC='--flannel-backend=none --disable-network-policy' \
  sh -s -

# Agent nodes (joined to the master):
curl -sfL https://get.k3s.io | \
  K3S_URL='https://${MASTER_IP}:6443' \
  K3S_TOKEN=${NODE_TOKEN} \
  sh -s -

export KUBECONFIG=/etc/rancher/k3s/k3s.yaml

# Install Cilium:
cilium install --version 1.16.x \
  --set=ipam.operator.clusterPoolIPv4PodCIDRList="10.42.0.0/16"
```

The upstream recipe's two load-bearing flags:

- `--flannel-backend=none` disables k3s's embedded Flannel CNI
  (Cilium takes over).
- `--disable-network-policy` disables k3s's built-in
  NetworkPolicy enforcer (Cilium's L7-aware NetworkPolicy
  supersedes it).

The upstream "Install Cilium without kube-proxy" addendum is the
single sentence:

> If running Cilium in Kubernetes Without kube-proxy mode, add
> option `--disable-kube-proxy`

We take that addendum (see §3 below).

## 2. What the pipeline does instead

| Step | Upstream recipe | Pipeline adaptation |
|---|---|---|
| Master k3s install | `curl -sfL https://get.k3s.io \| INSTALL_K3S_EXEC='--flannel-backend=none --disable-network-policy' sh -s -` | Same flags PLUS `--disable-kube-proxy`, `--disable=traefik`, `--disable=servicelb`, `--disable=local-storage`, `--disable=metrics-server`, `--kubelet-arg=cloud-provider=external`, `--node-ip=<cp_ip>`, `--node-external-ip=<cp_ip>`, `--tls-san=<cp_ip>`, `--tls-san=<svc_gateway>`, `--tls-san=kubernetes.default.svc`, `--cluster-cidr=172.16.0.0/16` (cicd) / `172.20.0.0/16` (apps), `--service-cidr=172.17.0.0/16` (cicd) / `172.21.0.0/16` (apps), `--cluster-dns=172.17.0.10` (cicd) / `172.21.0.10` (apps). See `tools/lib/k3s_installer.py::_SERVER_BASE_FLAGS` and `tools/lib/k3s_installer.py::plan_server`. |
| Agent k3s install | `curl -sfL https://get.k3s.io \| K3S_URL=https://${MASTER_IP}:6443 K3S_TOKEN=${NODE_TOKEN} sh -s -` | `${MASTER_IP}` is the CP host IP (no kube-vip; no VIP). Agent receives `--kubelet-arg=cloud-provider=external`. See `tools/lib/k3s_installer.py::plan_agent`. |
| Cilium install | `cilium install --version 1.16.x --set=ipam.operator.clusterPoolIPv4PodCIDRList="10.42.0.0/16"` | Done via `helm upgrade --install` with the values pinned in `tools/lib/helm_client.py::first_two_releases`. The full value set (and why each flag is needed) is in §3 below. |
| Pod CIDR | k3s default `10.42.0.0/16` (the `clusterPoolIPv4PodCIDRList` flag must match) | Pinned to `172.16.0.0/16` (cicd) / `172.20.0.0/16` (apps) at k3s install time; cilium auto-detects via `clusterPoolIPv4PodCIDRList` derived from `cluster_dict["pod_cidr"]`. |
| Service CIDR | k3s default `10.43.0.0/16` | Pinned to `172.17.0.0/16` (cicd) / `172.21.0.0/16` (apps) at k3s install time. The cilium chart does not consume the service CIDR; in-cluster ClusterIP routing is auto-detected. |
| Cilium k8sServiceHost | N/A (canonical recipe assumes k3s defaults work) | The cilium-agent must reach the apiserver during its own startup, before its own eBPF ClusterIP routing is installed. We pin `k8sServiceHost=<cp_ip>` so cilium-agent connects via the kernel routing table on eth0, not via the not-yet-routable `<svc_cidr>.0.1`. See `tools/lib/helm_client.py::first_two_releases` + WP08 in `.agents/skills/proxmox-k3s-pipeline/SKILL.md`. |
| Cgroup root | Default (assumes systemd-init host) | `cgroup.hostRoot=/sys/fs/cgroup` + `cgroup.autoMount.enabled=false`. Without these, cilium-agent attaches its BPF cgroup hooks at `/run/cilium/cgroupv2` (its initContainer mount), which is NOT where k3s pods live — every pod's `connect(2)` to the apiserver ClusterIP bypasses the socket-LB intercept and times out with "no route to host" even though the BPF map has the right entry. |
| `cgroup.autoMount` | Default `true` | `false` — we mount `/sys/fs/cgroup` ourselves via the systemd-managed host. The cilium-agent must NOT try to mount a virtualised `/run/cilium/cgroupv2` over it. |

## 3. The pinned Cilium Helm values

Live cluster values as of 2026-07-08 (cicd + apps, identical
apart from `k8sServiceHost` which is per-cluster). Verbatim from
`tools/lib/helm_client.py::first_two_releases` after the WP08
cleanup; do not change without re-running the WP08 validation
chain.

| Helm value | Pinned to | Why |
|---|---|---|
| `kubeProxyReplacement` | `true` | Required for cilium to fully own ClusterIP routing (eBPF maps + socket-level acceleration). k3s is started with `--disable-kube-proxy` (see `tools/lib/k3s_installer.py::_SERVER_BASE_FLAGS`); without `kubeProxyReplacement=true` the cilium-agent refuses to load (the two flags are a coupled contract). |
| `k8sServiceHost` | `<cp_ip>` (cicd=10.0.0.65, apps=10.0.0.67) | The cilium-agent must reach the apiserver during its own startup. The `<svc_cidr>.0.1` ClusterIP is not routable until cilium is up (chicken-and-egg), so we point cilium at the CP host's actual IP, which is reachable via the kernel routing table on eth0 regardless of CNI state. |
| `k8sServicePort` | `6443` | apiserver port; unchanged from upstream. |
| `mtu` | `1450` | vxlan adds 50 bytes of overhead to the underlying eth0 MTU 1500. Without this, large TLS ServerHello responses from the apiserver get fragmented at the vxlan encap and the conntrack return-path drops them. |
| `gatewayAPI.enabled` | `true` | cilium implements the GAMMA support (Gateway API L4 awareness + CRD validation). The actual `GatewayClass=envoy` implementation is provided by Envoy Gateway (see `docs/plan-envoy-gateway-and-smoke-tests.md`); cilium just validates that the CRDs are well-formed. |
| `ipam.mode` | `cluster-pool` | The upstream recipe's `cilium install` sets this implicitly via the `--set=ipam.operator.clusterPoolIPv4PodCIDRList=...` flag; we set it explicitly so the helm-rendered manifest is auditable. |
| `cgroup.hostRoot` | `/sys/fs/cgroup` | Required for k3s pods. Without this, cilium-agent attaches BPF cgroup hooks at the default `/run/cilium/cgroupv2` mountpoint, which is NOT where k3s pods live — every pod's `connect(2)` to the apiserver ClusterIP bypasses the socket-LB intercept. |
| `cgroup.autoMount.enabled` | `false` | We mount `/sys/fs/cgroup` ourselves via the systemd-managed host. The cilium-agent must NOT try to mount a virtualised `/run/cilium/cgroupv2` over it. |
| `hubble.enabled` | `false` | Hubbell is not used by the pipeline; the audit log lives in `logs/`. |
| `ipv4NativeRoutingCIDR` | NOT SET (omitted) | Earlier attempts to set this to the pod_cidr (172.16.0.0/16) caused cilium to delegate the svc CIDR (172.17.0.0/16) to the kernel routing table, which has no route to a pod-CIDR-shaped SVC IP — "no route to host" for every pod talking to the apiserver. The canonical cilium-on-k3s recipe leaves this auto-detected; we follow the same recipe. |

## 4. The Cilium k8sServiceHost gotcha (WP08, 2026-07-08)

**Symptom**: cilium-agent pod is `CrashLoopBackOff` with
`Failed to setup iptables rules for cilium_host` or
`Failed to determine service IP for kubernetes.default`.

**Cause**: cilium-agent's first apiserver call happens during
the cilium operator's reconciliation loop. The agent uses
`kubernetes.default.svc` to resolve the apiserver ClusterIP, but
in `kubeProxyReplacement=true` mode without cilium itself fully
up, the kernel has no socket-LB intercept to DNAT the ClusterIP
to the CP host. The connection hangs (or times out).

**Fix**: pin `k8sServiceHost=<cp_ip>` in the helm values, as in
§3. This bypasses the ClusterIP and connects to the CP host IP
directly. Once cilium is fully up, in-pod apiserver clients go
back through the ClusterIP (kubernetes.default.svc →
172.17.0.1) which cilium DNATs to the CP host.

## 5. The cilium cgroup root gotcha (WP08, 2026-07-08)

**Symptom**: cilium-agent pod is `Running`; cilium-operator is
`Running`; `cilium status` shows all green; yet pods cannot
reach the apiserver at the ClusterIP (`dial tcp 172.17.0.1:443:
connect: no route to host`).

**Cause**: cilium 1.16.x defaults to mounting the host `/proc`
inside an initContainer at `/run/cilium/cgroupv2` and attaching
its BPF cgroup connect/post_bind/etc hooks THERE. Pods created
by k3s/kubelet are placed under `/sys/fs/cgroup/kubepods.slice/...`
— which is NOT under `/run/cilium/cgroupv2`, so the socket-LB
intercept never fires on a pod's `connect(2)`. The cilium bpf lb
map has the right entry (e.g. `172.17.0.1:443 -> 10.0.0.65:6443`)
but pod-to-ClusterIP connections still time out because the
connect syscall never hits the cilium program.

**Fix**: pin `cgroup.hostRoot=/sys/fs/cgroup` and
`cgroup.autoMount.enabled=false`. After the fix:

```bash
bpftool cgroup tree /sys/fs/cgroup
# Shows cil_sock4_connect / cil_sock4_post_bind / etc. attached
# at /sys/fs/cgroup (the root), and pod traffic
# 172.16.0.217 -> 172.17.0.1:443 is DNAT'd correctly.
```

## 6. Verification

```bash
# Cilium is healthy on every node:
kubectl --context cicd -n kube-system get pods -l app.kubernetes.io/name=cilium
# NAME           READY   STATUS    RESTARTS   AGE
# cilium-22czw   1/1     Running   0          28m
# cilium-qrsty   1/1     Running   0          28m

# Cilium operator is healthy:
kubectl --context cicd -n kube-system get pods -l app.kubernetes.io/name=cilium-operator
# NAME                              READY   STATUS    RESTARTS   AGE
# cilium-operator-b8d8f9c5d-abcde   1/1     Running   0          28m

# The cilium BPF load-balancer map has the in-cluster ClusterIP -> CP host entry:
kubectl --context cicd exec -n kube-system ds/cilium -- \
  cilium bpf lb list | grep -E '172\.17\.0\.1'
# 172.17.0.1:443 (10.0.0.65:6443) (no-svc)

# Pod-to-apiserver round-trip works:
kubectl --context cicd run -it --rm --restart=Never \
  --image=nicolaka/netshoot:v0.13 \
  -- bash -c 'curl -sk https://kubernetes.default.svc/healthz'
# ok
```

## 7. References

- Canonical upstream recipe: <https://docs.cilium.io/en/stable/installation/k3s/>
- k3s CIDR overlap root cause: <https://github.com/k3s-io/k3s/issues/4627>
- Cilium cgroup root docs: <https://docs.cilium.io/en/stable/installation/k3s/#:~:text=cgroup>
- Code pins: `tools/lib/k3s_installer.py`, `tools/lib/helm_client.py::first_two_releases`
- Test pins: `tools/tests/test_k3s_installer.py`, `tools/tests/test_remaining_releases.py`, `tools/tests/test_agent_skill.py`
- Cluster install recipes: `infra/clusters/cicd/versions.lock.yaml`, `infra/clusters/apps/versions.lock.yaml`