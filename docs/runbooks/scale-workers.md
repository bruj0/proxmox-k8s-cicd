# Runbook: Scale workers up or down

This runbook grows or shrinks the worker pool of a cluster. Both
directions are idempotent (rerunning with the same value is a no-op).

## Prerequisites

- The cluster is currently in a healthy state
  (`kubectl --context <cluster> get nodes` shows all nodes Ready).
- The cluster module is already applied
  (`cd clusters/<cluster> && tofu state list` shows
  `module.proxmox_k3s_cluster`).

## Scale up

1. Edit `clusters/<cluster>/terraform.tfvars`:

   ```hcl
   workers = {
     count = 3   # change from 1 (or whatever the current value is)
   }
   ```

2. Run the apply:

   ```bash
   cd clusters/<cluster>
   tofu apply -auto-approve
   ```

3. Wait ~5 minutes per new worker; check readiness:

   ```bash
   kubectl --context <cluster> get nodes -w
   ```

   The new workers must transition to `Ready` within 5 minutes
   (NFR-014). If a worker does not reach Ready, see the
   `decommission-cluster.md` runbook for cleanup; do not retry apply
   repeatedly.

## Scale down

1. Edit `workers.count` back down (e.g. 3 -> 1):

   ```hcl
   workers = {
     count = 1
   }
   ```

2. Run the apply:

   ```bash
   cd clusters/<cluster>
   tofu apply -auto-approve
   ```

   The module cordons + drains the surplus workers in order of
   **highest VMID first** (so the most recently created workers are
   destroyed first). This avoids accidentally destroying the node
   that runs the most recent workloads.

3. The module respects PodDisruptionBudgets: any PDB that pins
   `minAvailable: 1` will block the drain. Check for blocking PDBs:

   ```bash
   kubectl --context <cluster> get pdb -A
   ```

   If a PDB is blocking, either:
   - Reduce the PDB's `minAvailable` to `0` (for stateless
     workloads), or
   - Manually `kubectl delete pod --force --grace-period=0` the
     blocking replicas (destructive; coordinate with the operator).

4. Minimum 60-second grace period before destroy. The module passes
   `--grace-period=60` to `kubectl drain`.

## Verify

After scaling up or down:

```bash
kubectl --context <cluster> get nodes
```

The number of worker nodes must equal `workers.count` and all nodes
must be Ready.

## Idempotency

Rerunning `tofu apply` with the same `workers.count` is a no-op (no
VM lifecycle events). This is the canonical "convergence from
partial state" contract.

## Rollback

Scale-down mistakes can be undone by setting `workers.count` back to
the higher value and re-running `tofu apply`. The module recreates
the destroyed VMs in order (lowest VMID first).