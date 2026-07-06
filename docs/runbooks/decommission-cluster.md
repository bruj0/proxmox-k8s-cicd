# Runbook: Decommission a cluster

Use this runbook when the operator wants to permanently retire one of
the two clusters (cicd or apps). This is the destructive counterpart
to the bring-up procedure documented in
`.agents/skills/proxmox-k3s-pipeline/SKILL.md`.

## Prerequisites

- The cluster's `~/.kube/config` context is still in place
  (`kubectl config get-contexts` lists it).
- The cluster's `infra/clusters/<name>/output.json` exists
  (otherwise `tofu destroy` cannot resolve the VMIDs).
- The operator has read and acknowledged SC-006
  (cleanup verification).

## Procedure

1. Ensure no workloads need to be preserved:

   ```bash
   kubectl --context <cluster> get deploy,sts,pvc -A
   ```

   For cicd: confirm any GitLab Runner registrations have been
   drained. For apps: confirm no live workload references this
   cluster.

2. Run the destroy:

   ```bash
   cd infra/clusters/<cluster>
   tofu destroy -auto-approve
   ```

   The module removes:

   - All cluster VMs from PVE (`qm destroy` on each VMID).
   - The cluster's VIP reservation from the dnsmasq ethers file on
     the proxmox host (via a `null_resource` that runs `pvesh` over SSH).
   - The cluster's context from `~/.kube/config` (via a `null_resource`
     that runs `kubectl config delete-context <cluster>`).

3. For full PVE cleanup of orphaned resources:

   ```bash
   # Find orphaned tokens (from WP00):
   pvesh get /access/users/<cluster>-user@pam 2>/dev/null && pvesh delete /access/users/<cluster>-user@pam
   pvesh get /access/roles/<cluster>-role 2>/dev/null && pvesh delete /access/roles/<cluster>-role
   pvesh get /access/tokens/<cluster>-user@pam/<cluster>-token 2>/dev/null && pvesh delete /access/tokens/<cluster>-user@pam/<cluster>-token
   ```

4. For full state cleanup:

   ```bash
   rm -rf infra/clusters/<cluster>/
   rm -f ~/.kube/config.tmp.<cluster>  # backup that kubeconfig_merger writes
   ```

5. Update `infra/tokens/versions.lock.yaml` to remove the cluster's
   entry from the `cross_cluster_dependencies` block (if any).

6. **If decommissioning the LAST cluster AND you're done with Phase 1**
   (no future cluster builds will happen on this host), also retire
   the SS1 Phase-0 artefacts:

   ```bash
   # Destroy the PVE phase-1 base + template (see .agents/skills/proxmox-k3s-pipeline/SKILL.md Step 1b.6).
   # VMID 999 is the talos-base (one per Proxmox host).
   # VMID 900 is the cloned-and-promoted `talos-template`.
   ssh root@$PVE_HOST 'qm stop 999 2>/dev/null; qm destroy 999 2>&1 | tail -1'
   ssh root@$PVE_HOST 'qm stop 900 2>/dev/null; qm destroy 900 2>&1 | tail -1'
   rm -f build/image-id.txt
   ```

   Skip this step if you plan to bring another cluster up on this
   host -- the base VMID 999 + template VMID 900 are reused across
   builds (the token-provisioned Packer just re-bakes the template;
   the role+user stay).

## Verify (SC-006)

```bash
# No VMs from this cluster remain:
qm list | grep -E "cluster_name|<cluster>" || echo "no cluster VMs"

# No context remains:
kubectl config get-contexts <cluster> 2>/dev/null && echo "STILL PRESENT" || echo "context gone"

# No PVE tokens remain:
pvesh get /access/tokens 2>/dev/null | grep -E "<cluster>" || echo "no tokens"
```

All three checks must report "no ..." or "context gone".

## Idempotency

`tofu destroy` on an already-destroyed cluster is a no-op (no
resources to remove).

## Rollback

There is no rollback once `tofu destroy` completes. The cluster must
be re-bootstrapped from scratch (Phase 1 -> Phase 5 in the Agent
Skill). For non-destructive scale changes, use the
`scale-workers.md` runbook instead.

## Cross-cluster impact

If decommissioning `cicd`, the `apps` cluster's ExternalName Services
(WP06) become unresolvable: the apps Pods will see
`gitlab.intranet` resolve to a non-existent VIP. Plan to decommission
`apps` first, then `cicd`.