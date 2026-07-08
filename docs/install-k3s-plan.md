# Plan: Install k3s on the four cluster VMs (idempotent, scripted)

Status: draft, awaiting verification of the open questions in [§5](#5-open-questions--verify-before-implementing)
before implementation work begins. Web-researched 2026-07-08 against the
official Rancher k3s documentation and release notes.

## 1. Goal and scope

We have four Ubuntu 24.04 VMs on `kvm.example.net` (`cicd-cp-1`,
`cicd-w-1`, `apps-cp-1`, `apps-w-1`), each with
`qemu-guest-agent` alive and DHCP'd on `vnet0`. None of them
currently runs k3s.

**Goal**: bring up two single-server k3s clusters (`cicd`,
`apps`) by calling one Python command per cluster, with the
recipe reproducible from the agent skill at
`.agents/skills/proxmox-k3s-pipeline/SKILL.md`.

**Out of scope** (handled by other phases):
- VM cloning and DNS (Phase 2 / `infra/clusters/*`).
- Cilium, kube-vip, proxmox-ccm, proxmox-csi, cert-manager
  (already installed by the existing `helm` phase of
  `bootstrap_cluster`).
- Cloudflare Tunnel ingress and the cross-cluster
  ExternalName services (`externalname` phase).

## 2. Decisions (from research)

| Decision | Pin / source | Rationale |
|---|---|---|
| k3s version | `v1.34.9+k3s1` (channel `stable`) | Latest 1.34.x per https://github.com/k3s-io/k3s/releases/tag/v1.34.9%2Bk3s1 (2026-06-24). The `k3s_max: v1.34.x` slot in `versions.yaml` already targets it. v1.36 exists but is outside the channel matrix. |
| Install mechanism | `curl -sfL https://get.k3s.io` piped to `sh -` **over SSH**, executed by Python orchestration | docs.k3s.io/quick-start. The upstream `install.sh` is **hash-checked idempotent** (`No change detected so skipping service start`); re-running with identical env is a no-op. |
| Server flag set | `--flannel-backend=none --disable=traefik --disable=servicelb --disable=local-storage --disable=metrics-server --kubelet-arg=cloud-provider=external --node-ip=<intf-ip> --node-external-ip=<intf-ip>` | Already declared in `versions.yaml::k3s::v1.34.x::install_args_default.control_plane`. Cilium (in the existing `helm` phase) replaces Flannel + kube-proxy. proxmox-ccm needs `cloud-provider=external`. |
| Agent flag set | `K3S_URL=https://<vip>:6443` plus `--flannel-backend=none --node-ip=<intf-ip> --node-external-ip=<intf-ip>` | Agents join via the kube-vip VIP, **not** a control-plane's eth0 IP, so they survive control-plane failover. |
| `--node-ip` source | The SDN-DHCP-allocated IPv4 reported by `qm agent <vmid> network-get-interfaces` | Avoids hard-coding IPs; survives SDN DHCP reassignment. |
| Join token | Read from the first server with `cat /var/lib/rancher/k3s/server/node-token` over SSH | Single-token per cluster, rotated by k3s. |
| Idempotency check before install | `systemctl is-active --quiet k3s` + `test -f /etc/rancher/k3s/k3s.yaml` | Belt-and-braces: if the unit is active AND the kubeconfig exists, the Python wrapper refuses to re-invoke the installer even if the operator called `--force`. |
| `--node-external-ip` vs `--node-ip` | Pass both with the same value | k3s uses the kubelet's `NodeExternalIP` for any cloud-controller-manager (proxmox-ccm) that reads it; mismatches cause `providerID` lookups to fail. |
| Uninstall | Not exposed in this plan | Decommission is a separate runbook. |

### 2.1 Why not bake k3s into the template?

The Phase 1 build recipe (in `Step 1.2` of the skill) installs
**only** `qemu-guest-agent`, `openssh-server`, and `cloud-init`.
Adding k3s to the template would force every new cluster to
re-k3s-install the template every time the binary or service
args change. The current per-VM approach is one SSH call per
node and the install.sh hash check makes re-runs cheap.

## 3. Files touched

| File | Change |
|---|---|
| `tools/lib/k3s_installer.py` | **NEW**. `K3sInstaller` class with `install_server`, `install_agent`, `verify`, `read_node_token`. All subprocess-over-SSH. Versions read from `tools/versions.lock.yaml`. `StructuredLogger` everywhere, secrets redacted. |
| `tools/bootstrap_cluster.py` | Insert a new `_run_install_k3s` phase between `cloudinit` and the existing `k3s` verify-phase; add `install_k3s` to `PHASES`; keep the existing `k3s` phase as a pure `/healthz` check. Update state-file migration to add the new key. |
| `tools/versions.lock.yaml` | Pin `k3s_stable_version: v1.34.9+k3s1`; add `kubectl: >= v1.34.0` (installed on the operator host for verify); add a `cross_check` entry for the live-host install. |
| `infra/clusters/cicd/versions.lock.yaml` and `infra/clusters/apps/versions.lock.yaml` | Mirror the k3s pin. |
| `versions.yaml` | Update `k3s::v1.34.x::install_url` rationale; ensure `install_args_default.agent` has the same flags as `control_plane` minus `--disable`/`--kubelet-arg=cloud-provider=external`. |
| `tools/tests/test_k3s_installer.py` | **NEW**. Red→green: server vs agent branching, idempotency skip, env-var formatting, version-pin enforcement, node-token reader error path. |
| `tools/tests/test_bootstrap_cluster.py` | Add `install_k3s` to phase ordering test, idempotent re-run test. |
| `tools/tests/test_agent_skill.py` | Pin skill text: `install_k3s` appears between `cloudinit` and `k3s`; `INSTALL_K3S_VERSION=v1.34.9+k3s1` is documented; idempotency call-out exists. |
| `.agents/skills/proxmox-k3s-pipeline/SKILL.md` | New `Step 4a -- install_k3s sub-phase` section with the recipe + a `4a.x` gotcha block; bump the Phase-4 phase list to `cloudinit, install_k3s, k3s, helm, kubeconfig, host_ports, externalname`. |
| `tools/lib/versions.py` *(if it doesn't exist as a reader, add a tiny one)* | Single-reader pattern for `tools/versions.lock.yaml` so we don't `yaml.safe_load` it from two places. |

**Not touched**: `infra/modules/proxmox-k3s-cluster/*.tf` (the
cluster root already renders node IPs that k3s binds to), any
shell scripts (the gateway stays in Python; the only shell we
run is the official `install.sh` over SSH), PowerDNS records
(written at Phase 2 apply-time, unchanged).

## 4. Implementation order

Locked-in, TDD-driven:

1. Write `tools/tests/test_k3s_installer.py` (red).
2. Implement `tools/lib/k3s_installer.py` (green).
3. Wire `install_k3s` into `tools.bootstrap_cluster`; update
   `PHASES` and state migration.
4. Update `tools/versions.lock.yaml` and the per-cluster pins.
5. Update `tools/tests/test_agent_skill.py` and the skill body
   (now phase-list assertion will go green because the skill
   says exactly what we ship).
6. `python -m pytest tools/tests/` then `ruff check` then
   `mypy --strict --explicit-package-bases` on `tools/`.
7. Conventional Commits commit (no `.env` / `.tfstate*` /
   secrets); push.

### 4.1 Reproducibility check (operator workflow)

```bash
# one-time per cluster, after Phase 2 lands:
python -m tools.bootstrap_cluster --cluster cicd --phases install_k3s
# ^ installs k3s on cicd-cp-1 (server) then cicd-w-1 (agent)

python -m tools.bootstrap_cluster --cluster apps --phases install_k3s
# ^ installs k3s on apps-cp-1 (server) then apps-w-1 (agent)
```

Reruns of either call converge to a no-op (`No change detected
so skipping service start` from the upstream installer, plus
the Python wrapper's belt-and-braces `systemctl is-active`
short-circuit).

## 5. Open questions — verify before implementing

These need answers from the operator OR live verification on
`kvm.example.net` before we can say the recipe is wired. Each
one is a small task and I have written **what to check** and
**what the default assumption would be** if the operator
opts not to chase it.

### 5.1 Is the kube-vip VIP actually serving :6443 today?

**Why I'm asking.** The plan assumes agents join on
`https://<vip>:6443`. The VIP (10.0.0.30 for `cicd`, 10.0.0.40
for `apps`) is reserved in the vnet0 dnsmasq ethers file
(`infra/modules/proxmox-k3s-cluster/dnsmasq.tf`) and the
`kube-vip` Helm chart installs in the `helm` phase of
bootstrap_cluster. But between `cloudinit` and `helm` there
is **no path** that proves `10.0.0.30:6443` answers — the VIP
exists only after the kube-vip DaemonSet rolls out, which is
itself the *next* phase.

Two failure modes I want to nail down:

1. **Order-of-operations race.** If we install k3s on the
   server with `--flannel-backend=none` and `--tls-san=<vip>`
   correctly, the server binds `10.0.0.30:6443` *itself* on
   its own NIC (because kube-vip is not yet elected). That
   works fine for *first* bootstrap. But for **subsequent HA
   bootstrap** (not a current goal but the spec mentions it),
   the server binds the VIP only after kube-vip elects, and
   agents that try to join before that election will hang.

2. **Missing `--tls-san=<vip>` argument.** k3s generates a
   serving cert whose SANs include only `--node-ip` /
   `--node-external-ip` by default. If the kubeconfig on the
   server uses `https://10.0.0.30:6443`, `kubectl get nodes`
   from the server-host's perspective still works (no TLS
   verification on loopback) but **any external client
   pulls a kubeconfig with `server: https://10.0.0.30:6443`
   and gets `x509: certificate is not valid for 10.0.0.30`**.

**Default assumption** (if the operator can't verify now):
add `--tls-san=<vip>` to the `INSTALL_K3S_EXEC` for the
server, AND document in the skill that **the cluster is not
usable until the `helm` phase lands kube-vip** (the
`install_k3s` phase will pass locally even though external
clients can't reach the apiserver until kube-vip elects).

**What to verify**:
```bash
# After Phase 4 install_k3s runs, but BEFORE helm runs:
SSH_AUTH_SOCK=/home/bruj0/.bitwarden-ssh-agent.sock \
  ssh root@10.0.0.30 'systemctl is-active k3s; \
   systemctl show k3s -p ExecStart'
# Expect: "active"
# Expect: ExecStart contains --tls-san=10.0.0.30 (or whatever VIP)
```

If `ExecStart` does NOT contain `--tls-san=<vip>`, the
v1.34.x install is using defaults and the kubeconfig SAN
list will be wrong. **Then we add `--tls-san` to the install
recipe AND we re-emit the kubeconfig** in the new `kubeconfig`
phase (currently the script reads from
`/etc/rancher/k3s/k3s.yaml` which is fine — we just add a
sanity-assert that the file's `server:` URL SAN matches the
VIP).

### 5.2 Which `--node-ip` value does install.sh actually pick up?

**Why I'm asking.** `versions.yaml::k3s::v1.34.x::install_args_default`
currently lists `--node-ip` and `--node-external-ip` but does
not pin their value — they're meant to be filled in at
install time per-node. I plan to populate them from the SDN
DHCP lease (`qm agent network-get-interfaces` output).

The risk: if the operator's NIC is `ens18` but the install
script's `--node-ip` is the lease from a *different*
interface (e.g. a leftover `lo` route), k3s's kubelet will
advertise the wrong `InternalIP`. Cilium IPAM uses
`NodeInternalIP`; the mis-match breaks the `cilium-health`
checks and the proxmox-ccm `providerID` lookup.

**What to verify** (run on the template before Phase 2
clones, or on a clone after first boot):
```bash
qm agent 111 network-get-interfaces   # cicd-cp-1
# Note: the PVE API returns IPs on ens18 (10.0.1.0/24 by
# module default).
qm agent 111 network-get-interfaces | jq '[.[].ip-addresses[]?.address] | flatten'
# Expect: one 10.0.1.x address.
```

If the template / VM has additional IPs (e.g. a `tailscale0`
or a `192.168.x.x` link-local), we need to filter the
qm-agent response to the interface that maps to the PVE
bridge (typically the first non-loopback IPv4).

### 5.3 Is `k3s` already partially installed on any of the four VMs?

**Why I'm asking.** A previous Phase 4 attempt may have
landed k3s on one or two nodes and failed on the others. The
agent's idempotency check (`systemctl is-active k3s`) will
report "active" on the already-installed node and refuse to
touch it, which is what we want — but the operator needs to
know the **expected state** before clicking "go".

**What to verify**, before any new install:
```bash
SSH_AUTH_SOCK=/home/bruj0/.bitwarden-ssh-agent.sock \
  ssh root@10.0.1.<node-ip> 'systemctl is-active k3s; \
   test -f /etc/rancher/k3s/k3s.yaml && echo HAS_KUBECONFIG || echo NO_KUBECONFIG'
```

If `k3s` is already active and the cluster's apiserver is
healthy on the VIP, this whole plan is moot — go straight to
the `k3s` verify phase. If `k3s` is active but the cluster
is broken, call `/usr/local/bin/k3s-uninstall.sh` first and
then re-run `install_k3s`.

### 5.4 Do we have `kubectl` on the operator host?

The `k3s` verify-phase calls `kubectl --kubeconfig <file>
get --raw /healthz`. The operator host needs `kubectl`
matching the cluster's apiserver minor version (v1.34.x).
If it's missing the phase fails before k3s is even blamed.

**What to verify**:
```bash
kubectl version --client=true --output=yaml | grep -E 'major:|minor:'
# Expect: minor: "34" or higher
```

If missing, `apt-get install kubectl` per k8s.io docs and
re-pin in `tools/versions.lock.yaml` as `kubectl: >= v1.34.0`.

### 5.5 Will the operator grant SSH access from the agent to each VM's root?

**Why I'm asking.** The plan needs root SSH into each of the
four VMs. The current skill uses Bitwarden SSH agent
(`/home/bruj0/.bitwarden-ssh-agent.sock`) exclusively — and
that works against PVE on port 6022. But the four cluster
VMs accept SSH on the SDN DHCP-allocated IP, not on the PVE
host, so the **same Bitwarden key** must be authorized in
`/root/.ssh/authorized_keys` *inside* each VM (the cluster
module's `--sshkeys` writes the operator's public key on
`qm set`; we just want to confirm the file landed).

**What to verify**:
```bash
SSH_AUTH_SOCK=/home/bruj0/.bitwarden-ssh-agent.sock \
  ssh -o BatchMode=yes root@10.0.1.<x> 'echo ok'
```

If this fails with `Permission denied (publickey)`, the
cluster module's `--sshkeys` argument didn't take. Need to
re-set it (Phase 2 step 6 in the skill) before any
Phase-4 work proceeds.

---

## 6. What this plan will NOT do

- Will NOT upgrade k3s past v1.34.x. The version matrix
  (`versions.yaml::k3s::v1.34.x`) targets v1.34.x; v1.36 is a
  separate spec-bridge change.
- Will NOT bake k3s into the template. Per-VM install + the
  upstream installer's hash-check idempotency gives us
  reproducibility without copying 100 MB of binary into the
  template image.
- Will NOT generate new SSH keys, new Cloudflare tokens, or
  any new secret material. The Bitwarden SSH agent and the
  tokens from WP00 are the only credentials in scope.
- Will NOT make any kubectl-apply calls. The plan only
  installs k3s on the four VMs. Helm releases happen in the
  next phase.

## 7. Success criteria (assert ALL before declaring done)

1. `python -m pytest tools/tests/` → 92 + new tests pass.
2. `python -m ruff check tools/` → all checks passed.
3. `python -m mypy tools/` (strict, explicit bases) → no
   issues found.
4. `python -m tools.bootstrap_cluster --cluster cicd --phases install_k3s`
   exits 0 on a fresh cluster.
5. The same command exits 0 again immediately after (idempotent).
6. `python -m tools.bootstrap_cluster --cluster cicd --phases k3s`
   reports `apiserver /healthz ok`.
7. SKILL.md contains the words `install_k3s` in the phase
   list **and** in the new `Step 4a` body, and
   `INSTALL_K3S_VERSION=v1.34.9+k3s1` is present.
8. `tools/versions.lock.yaml::cross_check.install_k3s_2026_07_08`
   carries the live-host note (date, hash, success criteria).
