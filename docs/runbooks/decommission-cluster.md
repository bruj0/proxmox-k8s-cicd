# Runbook: Decommission a cluster

Use this runbook when the operator wants to permanently retire one of
the two clusters (cicd or apps). This is the destructive counterpart
to the bring-up procedure documented in
`.agents/skills/proxmox-k3s-pipeline/SKILL.md`.

## Prerequisites

- The cluster's `~/.kube/config` context is still in place
  (`kubectl config get-contexts` lists it).
- The cluster's `clusters/<name>/output.json` exists
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
   cd clusters/<cluster>
   tofu destroy -auto-approve
   ```

   The module removes:

   - All cluster VMs from PVE (`qm destroy` on each VMID).
   - The cluster's VIP reservation from the dnsmasq ethers file on
     BigBertha (via a `null_resource` that runs `pvesh` over SSH).
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
   rm -rf clusters/<cluster>/
   rm -f ~/.kube/config.tmp.<cluster>  # backup that kubeconfig_merger writes
   ```

5. Update `infra/tokens/versions.lock.yaml` to remove the cluster's
   entry from the `cross_cluster_dependencies` block (if any).

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