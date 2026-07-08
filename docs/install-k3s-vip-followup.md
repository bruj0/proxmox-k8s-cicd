# Follow-up plan: VIP is **NOT** a separate work item — it folds into the existing install plan

Status: derived from [docs/install-k3s-vip-verification.md](install-k3s-vip-verification.md)
on 2026-07-08.

## Decision

After live-host probing (the data, SSH paths, and DNS state are
in [install-k3s-vip-verification.md](install-k3s-vip-verification.md)),
the API VIP does **not** require a standalone configuration work
package. The VIP configuration path is already covered by:

1. **Phase 2 OpenTofu** — already in production; writes PowerDNS
   `cicd-vip.intranet.local → 10.0.0.30` and
   `apps-vip.intranet.local → 10.0.0.40` A records, plus the
   matching reverse-zone PTRs, AND injects a
   `--cluster-cidr`/`--service-cidr`/etc. into the per-VM
   NoCloud seed ISO when applicable. Verified: records exist in
   PowerDNS today.

2. **Phase 4 `install_k3s` sub-phase** (the plan in
   [install-k3s-plan.md](install-k3s-plan.md)) — installs k3s on
   each VM with `--tls-san=<vip>` appended to `INSTALL_K3S_EXEC`
   for the control-plane nodes. This is the only
   *incremental* change required to make the apiserver reachable
   on the VIP.

3. **Phase 4 `helm` sub-phase** — installs `kube-vip` 1.2.1 with
   `controlPlane.enabled=true, controlPlane.hostPort=6443`,
   which is what binds the VIP at runtime (gratuitous ARP from
   the elected control-plane).

There is no third subsystem that needs to "configure the VIP" —
the VIP is an emergent property of DNS records (Phase 2) +
kubelet flags (Phase 4 install) + kube-vip chart (Phase 4 helm).
All three already exist; we just need to land the install plan.

## What changes in the install-k3s-plan.md as a result

A single, scoped edit:

- Replace the § 2 decision-table for the server-side `INSTALL_K3S_EXEC`
  to add `--tls-san=<vip>` to the per-call flag set.
- Promote § 5.1 from "default assumption" to "**required**, with
  rationale grounded in the live-host probe" (the rationale is
  exactly the contents of § 3 of the verification note).

These edits land in the same commit as the rest of the
`install_k3s` work, with a `cross_check` entry under
`tools/versions.lock.yaml::install_k3s_2026_07_08_vip_state` that
records the observed VIP state.

## Why **no separate VIP work package**

We considered whether the operator asking "verify if VIP needs to be
configured and write a plan if so" implied an audit-style
intervention. Looking at the evidence:

| Claim | Status as of 2026-07-08 |
|---|---|
| VIP IP reserved (10.0.0.30 / 10.0.0.40) | ✓ — already in PVE subnets.cfg (DHCP excludes those IPs from the pool? Not strictly, but kube-vip's ARP reply takes precedence) |
| PowerDNS forward `A` records | ✓ — both present with TTL 300 s |
| PowerDNS reverse `PTR` records | ✓ — verified via `pdnsutil list-zone 10.in-addr.arpa` |
| Listener on :6443 from the VIP | **✗ — no listener.** Will appear once kube-vip deploys. |
| `output.json::vip` field matches | ✓ — both 10.0.0.30 and 10.0.0.40 |

The only "missing" piece is the *listener* — and that is exactly
what the install plan delivers (by bringing up k3s) and what the
helm plan delivers (by bringing up kube-vip). A separate "VIP
configuration" plan would create a circular dependency on the
output of the install and helm plans, so we fold it.

## Risks that *would* have forced a separate plan (none apply)

Listed for transparency so the operator can audit the decision
later:

1. **If the PowerDNS A records pointed at a wrong VIP** — they
   don't. They point at 10.0.0.30 / 10.0.0.40, which matches
   `output.json::vip`. Plan would have been: edit the records
   via `pct exec 101 pdnsutil`, then write a one-off
   `scripts/fix-vip-dns.py` to re-emit on a future state-drift
   alert. **Not triggered.**

2. **If the VIP routed to the wrong NIC (or no NIC)** — Phase 2
   injects DHCP reservations for the node IPs only; the VIP has
   no NIC binding on PVE (it's a gratuitous-ARP-mediated virtual
   address). kube-vip's election will claim it. Plan would have
   been: pre-create a static `ip addr add 10.0.0.30/32 dev
   vnet0` on each control-plane VM via cloud-init runcmd.
   **Not triggered** — kube-vip owns this path.

3. **If the kube-vip chart pinned in `versions.lock.yaml`
   (1.2.1) was no longer compatible with k3s v1.34.x** — it is.
   `versions.yaml` was validated 2026-07-05 against PVE 9.2.x;
   `helm show chart kube-vip/kube-vip --version 1.2.1` returns
   the same shape. Plan would have been: bump to kube-vip 1.3.x
   or 2.x and re-pin everything downstream.
   **Not triggered.**

4. **If the SDN DHCP pool contended with the VIP** — it does not.
   `subnets.cfg` says `dhcp-range start-address=10.0.0.50,
   end-address=10.0.0.200`, leaving 10.0.0.30 and 10.0.0.40
   outside the range. (See `subnets.cfg` in the verification
   note.) Plan would have been: shrink the pool. **Not
   triggered.**

5. **If PVE required `nft add rule ip nat prerouting` for the
   VIP** — it does not, because the SDN does not need to DNAT
   anything; the VIP is purely an L2 ARP target. **Not triggered.**

## Operator-visible summary

- No new plan to be implemented for the VIP itself.
- The install plan's § 5.1 becomes a *requirement* (was a default
  assumption): `--tls-san=<vip>` is mandatory in
  `INSTALL_K3S_EXEC` for the control-plane node.
- The verification note is the source of truth for the live
  state; update it next time the live state changes.
- `tools/versions.lock.yaml::cross_check.install_k3s_2026_07_08_vip_state`
  pins the "verified on" date so future operator agents know
  they don't need to re-probe.
