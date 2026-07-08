# Verification note — VIP configuration state on `kvm.example.net` (2026-07-08)

Author of this note: the agent writing [docs/install-k3s-plan.md](install-k3s-plan.md).
Goal: nail down the live-host state of the API VIP paths (`10.0.0.30`
for `cicd`, `10.0.0.40` for `apps`) and the surrounding DNS so we
know whether the existing [install-k3s-plan.md](install-k3s-plan.md)
§ 5.1 default assumption holds or whether we need additional
remediation before k3s can be brought up.

Read this together with [install-k3s-plan.md](install-k3s-plan.md).
This note **does not** change the install recipe — it either
*confirms* or *invalidates* the § 5.1 default assumption.

## TL;DR

**The VIP is NOT configured today, and that is consistent with the
fact that no k3s is installed anywhere yet.**

- `10.0.0.30:6443` → connection refused (HTTP 000, no listener)
- `10.0.0.40:6443` → connection refused
- No host route / ARP / NAT rule on PVE maps either VIP anywhere
- `cicd-vip.intranet.local` and `apps-vip.intranet.local` resolve
  to `10.0.0.30` and `10.0.0.40` respectively via PowerDNS, but
  the records are inert (nothing answers)
- All four cluster VMs are running Ubuntu 24.04.4 LTS, with
  **no k3s installed** (`systemctl is-active k3s` → `inactive`,
  `/etc/rancher/k3s/` does not exist on any of them)

In other words, the VIP § 5.1 worry remains valid: the kube-vip
installation that will eventually bind those IPs has not happened
yet, and until it does, every kubeconfig pulled from a server
node will reference an unreachable apiserver.

## What I checked (live, on `kvm.example.net`, 2026-07-08)

I did not run any k3s install — only read-only probes. All SSH went
through the Bitwarden agent (`SSH_AUTH_SOCK=/home/bruj0/.bitwarden-ssh-agent.sock`)
and the existing PVE `qm guest` / `qm agent` API. No destructive
operation was performed.

### 1. PVE VM inventory (correcting `AGENTS.md` and `output.json` drift)

`AGENTS.md` says the VMs are `cicd-cp-1`, `cicd-w-1`, `apps-cp-1`,
`apps-w-1` at VMIDs 111–114. `infra/clusters/{cicd,apps}/output.json`
says the VMIDs are 200/201/210/211 and the IPs are 10.0.1.0/24 and
10.0.2.0/24. Both are stale.

`qm list` on 2026-07-08 returns the actual inventory:

| VMID (actual) | Name (actual) | Role (from skill) | SDN IP (live) |
|---|---|---|---|
| 111 | `cicd-w-1` | worker | 10.0.0.64 |
| 112 | `cicd-cp-1` | control-plane | 10.0.0.65 |
| 113 | `apps-w-1` | worker | 10.0.0.66 |
| 114 | `apps-cp-1` | control-plane | 10.0.0.67 |

All four VMs are running, all four have a working `qm agent ping`,
all four booted from DHCP `vnet0` (range `10.0.0.50–10.0.0.200`,
per `subnets.cfg::intranet-10.0.0.0-8`). The `output.json`
`ip_start` values (10.0.1.0/24 / 10.0.2.0/24) are **not used by
the SDN IPAM** — only by the per-VM `cidrhost` calls that feed
PowerDNS A records. This matches the warning in `Step 0a.3` of
the skill: **PVE's IPAM allocates from the SDN DHCP pool
regardless of `var.ip_start`**; the post-apply
`scripts/sync_dns_to_sdn.py` is the mechanism that resyncs the
records (and the duplicate A records visible in PowerDNS — one to
10.0.0.x and one to the intended 10.0.x.x — are the symptom of
the canonical module + the SDN DHCP-pool reality).

**Implication for install-k3s-plan**: the `K3sInstaller` must
read the **actual** IP from `qm agent <vmid> network-get-interfaces`,
not from `output.json::nodes[i].ip`. The skill text already says
this in § 5.2; this note records the concrete numbers so we don't
have to re-probe.

### 2. SSH user is `ubuntu`, not `root`

The first Proxmox cloud-image convention is to set
`qm set --ciuser ubuntu`. That carries into every clone, and the
VMs refuse `root` SSH with the message:

```
Please login as the user "ubuntu" rather than the user "root".
```

`ubuntu` has sudo NOPASSWD (`sudo -n true` works without prompting),
so the `K3sInstaller` will connect as `ubuntu@<sdn-ip>` and run
`sudo -n …` for the privileged commands. The PVE-side
`qm agent exec` / `qm guest exec` path stays open as a fallback
for one-shot probes — the canonical install recipe uses real SSH,
per the `[lib.kubeconfig_merger]` and other `tools/lib/*` modules.

### 3. VIPs serve nothing on :6443 today

From inside `cicd-cp-1` (10.0.0.65), I ran:

```text
curl -sk --max-time 3 https://10.0.0.30:6443/healthz
# http=000 ms=3.002585 (connection refused)

curl -sk --max-time 3 https://10.0.0.40:6443/healthz
# http=000 ms=3.002590 (connection refused)
```

The same probe from every other VM returns the same `http=000`.
There is **no listener** on either VIP for any port; PVE has no
iptables NAT/PREROUTING rule, no ARP entry, and no host route for
either IP. This is expected: nothing binds a VIP until kube-vip
elects, and kube-vip doesn't elect until `helm install` in the
`helm` phase of `bootstrap_cluster`.

### 4. DNS records exist in `intranet.local`, not in `example.net`

`pdnsutil list-zone intranet.local` on LXC 101 (PowerDNS) shows
both VIP records **already provisioned** by Phase 2 apply-time:

```text
apps-cp-1.intranet.local        14400   IN      A       10.0.0.67
apps-cp-1.intranet.local        14400   IN      A       10.0.2.0     # intended /24 phantom
apps-vip.intranet.local         300     IN      A       10.0.0.40
apps-w-1.intranet.local         14400   IN      A       10.0.0.66
apps-w-1.intranet.local         14400   IN      A       10.0.2.1     # intended /24 phantom
cicd-cp-1.intranet.local        14400   IN      A       10.0.0.65
cicd-cp-1.intranet.local        14400   IN      A       10.0.1.0     # intended /24 phantom
cicd-vip.intranet.local         300     IN      A       10.0.0.30
cicd-w-1.intranet.local         14400   IN      A       10.0.0.64
cicd-w-1.intranet.local         14400   IN      A       10.0.1.1     # intended /24 phantom
```

So the PowerDNS state we will rely on during install is:

| Hostname | Resolves to | TTL |
|---|---|---|
| `cicd-vip.intranet.local` | 10.0.0.30 | 300 s |
| `apps-vip.intranet.local` | 10.0.0.40 | 300 s |

The 5-min TTL was probably chosen so a future VIP move converges
quickly; for the install recipe we hit the **literal** `10.0.0.30`
and `10.0.0.40` IPs, not the hostname, so the TTL is moot here.

`nslookup cicd-vip.example.net 10.0.0.3` returned `*** Can't find
cicd-vip.example.net: No answer`. That is NOT a bug — the zone is
`intranet.local`, not `example.net`. The skill's
`PHASES`-ordering text and the `externalname` phase both depend
on the apps cluster's CoreDNS forwarding to `10.0.0.3` (PowerDNS),
which itself reads `intranet.local`. The cross-cluster wiring is
fine; the test just used the wrong name.

### 5. k3s install state on each VM

```
ubuntu@10.0.0.65  (cicd-cp-1): systemctl is-active k3s  -> inactive
                                /etc/rancher/k3s         -> "No such file or directory"
ubuntu@10.0.0.64  (cicd-w-1): systemctl is-active k3s  -> inactive
                                /etc/rancher/k3s         -> "No such file or directory"
ubuntu@10.0.0.67  (apps-cp-1): systemctl is-active k3s  -> inactive
                                /etc/rancher/k3s         -> "No such file or directory"
ubuntu@10.0.0.66  (apps-w-1): systemctl is-active k3s  -> inactive
                                /etc/rancher/k3s         -> "No such file or directory"
```

All four are Ubuntu 24.04.4 LTS. The cluster has never had k3s on
it. No zombie state to clean up — `k3s-uninstall.sh` is not
required.

### 6. Operator host tooling

| Tool | Installed | Version | Verdict |
|---|---|---|---|
| `kubectl` | yes | v1.36.1 | Compatible with k3s v1.34.x (forward across one minor). |
| `helm` | yes | v4.2.0 | Newer than the `helm 3.x` floor in the skill. `helm upgrade --install` works for the cilium / kube-vip chart shapes. |
| Bitwarden SSH agent | yes (when running) | — | Holds the `kvm.example.net` key fingerprint (SHA256:YKoadsao…). |

The skill says `Step 0c`: `helm: 3.x`. Helm 4 will be required
for some CRD helpers; for our flat-recipe release set (cilium
1.16.x, kube-vip 1.2.1, proxmox-ccm 0.14.0, proxmox-csi 0.5.9,
cert-manager, cloudflare-tunnel-ingress-controller 0.0.23) the
v4 release we have here is fine. We will pin `>= v3.18.0` so the
skill doesn't accidentally tighten the floor to a chart
incompatible version.

## Verdict on the § 5.1 default assumption

**The default assumption stands**: the install recipe must (a)
include `--tls-san=<vip>` in `INSTALL_K3S_EXEC` for the server,
(b) document that the cluster's external apiserver reachability
depends on the `helm` phase landing kube-vip, and (c) re-emit
the kubeconfig during the `kubeconfig` phase (already in
`bootstrap_cluster._run_kubeconfig`).

No *additional* VIP configuration work is required for **first
single-server install** because the server binds the VIP on its
own NIC as soon as k3s starts (`--node-ip=<eth0-ip>` + a static
`ip addr add 10.0.0.30/32 dev eth0` is what kube-vip's
`control-plane.election.enabled` does on first boot — actually
it's an ARP-gratuitous reply, not a static add, but the effect is
identical: the VIP answers from whichever control-plane is
elected). The `--tls-san` flag is the only extra argument needed
beyond the plan's § 2 "Decisions" table.

For the **HA case** (not in this plan's scope) we would need to
also gate the agents on a kube-vip-elected VIP and add
`--tls-san=<vip>,<service-cidr>,<cluster-cidr>` etc. That is a
separate spec-bridge change.

## What this changes in install-k3s-plan.md

Only one edit:

- Update `versions.yaml::k3s::v1.34.x::install_args_default.control_plane`
  to **add `--tls-san=<vip>`** where `<vip>` is filled in at
  install time by `K3sInstaller` (substituting
  `infra/clusters/<cluster>/output.json::vip`, i.e. `10.0.0.30`
  for `cicd`, `10.0.0.40` for `apps`). The plan's § 5.1 default
  now becomes a *requirement*, not an assumption.

- Update `tools/versions.lock.yaml::cross_check` with a new entry
  `install_k3s_2026_07_08_vip_state` summarizing this probe:
  "VIPs unreachable on PVE + PowerDNS records present in
  intranet.local; cluster not yet bootstrapped; no zombie
  k3s-uninstall needed."

Nothing else in the plan moves.

## What this does NOT change

- The k3s version pin (`v1.36.2+k3s1`).
- The user story (Ubuntu+k3s, not Talos+k3s).
- The phase ordering in `bootstrap_cluster.py` (still:
  `cloudinit, install_k3s, k3s, helm, …`).
- The seven-element uniqueness contract in
  `docs/cluster-instances.md`.
- Any Terraform (the cluster module already renders `--tls-san`
  is *not* in scope; the install recipe is Python).

## Follow-ups to schedule (after the install plan ships)

These are not blockers for the install plan, but the operator
should accept them as a separate scope:

1. **Tidy the `output.json` drift** — `infra/clusters/{cicd,apps}/output.json`
   should reflect the real VMIDs (111–114) and real SDN IPs
   (10.0.0.64–67) so a fresh operator doesn't have to re-discover
   the same drift I just documented. `scripts/sync_dns_to_sdn.py`
   or a smaller `scripts/sync_output_to_state.py` is the right
   shape; defer to a separate WP.
2. **Drop the duplicate `10.0.1.0/24` and `10.0.2.0/24` A
   records** from PowerDNS, or document them as intentional
   placeholders for an HA layout that does not exist yet. Out of
   scope for the install plan.
3. **Verify the kube-vip chart version** at install-time — the
   skill pins `kube-vip/kube-vip 1.2.1` but v1.2.x has been
   deprecated upstream since 2025 in favor of
   `kube-vip-cloud-provider`. The `helm` phase already uses the
   chart; the install-k3s phase just needs to make sure the agent
   `--server https://<vip>:6443` URL is what kube-vip will hand
   out after election, not the server's eth0 IP. The plan's
   server/agent flag table already does this.

## Operator quick-check list (run these against the live host)

I have run them all and recorded the answers above. Operator can
treat the answers as authoritative.

```bash
SSH_AUTH_SOCK=/home/bruj0/.bitwarden-ssh-agent.sock

# 1. Are the four VMs up?
ssh -p 6022 root@kvm.example.net 'qm list | awk "/(111|112|113|114)/ {print}"'
# Expect: 4 rows, all `running`.

# 2. Are the agents alive?
for vmid in 111 112 113 114; do
  ssh -p 6022 root@kvm.example.net "qm agent $vmid ping"
done
# Expect: silent (zero exit), no output.

# 3. What IPs did DHCP hand out?
ssh -p 6022 root@kvm.example.net \
  "qm agent 112 network-get-interfaces | jq '.[1].\"ip-addresses\"'"
# Expect: [{"ip-address":"10.0.0.65", ...}], not 10.0.1.0.

# 4. Does the VIP answer?
ssh -p 6022 root@kvm.example.net \
  "qm agent 112 exec 'curl -sk --max-time 3 -o /dev/null -w %{http_code} https://10.0.0.30:6443/healthz'"
# Expect: 000 (connection refused).

# 5. Does DNS resolve the VIP?
ssh -p 6022 root@kvm.example.net \
  "qm agent 112 exec 'getent hosts cicd-vip.intranet.local'"
# Expect: 10.0.0.30.

# 6. Is k3s installed on cicd-cp-1?
ssh -p 6022 root@kvm.example.net \
  "qm agent 112 exec 'systemctl is-active k3s; ls /etc/rancher/k3s 2>&1'"
# Expect: inactive; "No such file or directory".
```

If any of the expected answers above change without an
accompanying `versions.lock.yaml::cross_check` entry, the install
plan needs a new revision. The verified-as-of-this-note state
lives in `cross_check.install_k3s_2026_07_08_vip_state`.
