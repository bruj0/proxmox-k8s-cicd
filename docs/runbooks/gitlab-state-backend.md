# GitLab Terraform state backend

Live as of 2026-07-06. All four tofu stacks in this repo now
store their state in a GitLab-managed Terraform state backend.

## Project layout

- **Project**: `infra-state/bigbertha` at <https://gitlab.com/infra-state/bigbertha>
- **Project ID**: `84156476`
- **Per-stack state name**:

  | Stack name                       | Stack dir                              | State present? |
  |----------------------------------|----------------------------------------|----------------|
  | `infra-tokens`                   | `infra/tokens/`                        | yes            |
  | `proxmox-k3s-cluster-module`     | `infra/modules/proxmox-k3s-cluster/`   | no (module)    |
  | `cluster-cicd`                   | `infra/clusters/cicd/`                 | yes            |
  | `cluster-apps`                   | `infra/clusters/apps/`                 | yes            |

The `proxmox-k3s-cluster-module` name is reserved even though
the module itself never carries state (see the
"Module backend block" note in `infra/modules/proxmox-k3s-cluster/versions.tf`).

## Required environment

- **`GITLAB_ACCESS_TOKEN`**: a GitLab personal access token (PAT)
  with the `api` scope. Create at
  <https://gitlab.com/-/user_settings/personal_access_tokens>.
- The glab CLI's cached OAuth token is NOT suitable (it expires
  daily; rotating it via `glab auth login` does not give you a
  PAT suitable for the state API).
- PAT expires 2027-07-01 in our setup; rotate BEFORE that.

## Initialize / migrate state

Use the helper:

```bash
export GITLAB_ACCESS_TOKEN=$(awk -F= '$1=="GITLAB_PAT"{print $2; exit}' .env)

# First-time migration (local -> GitLab):
./scripts/gitlab_backend.sh init infra-tokens     --migrate-state
./scripts/gitlab_backend.sh init cluster-cicd     --migrate-state
./scripts/gitlab_backend.sh init cluster-apps     --migrate-state
./scripts/gitlab_backend.sh init proxmox-k3s-cluster-module   # plain init, no state to migrate

# Subsequent runs (after a config change):
./scripts/gitlab_backend.sh init cluster-cicd

# Audit the flags the helper would use (passwords masked):
./scripts/gitlab_backend.sh show cluster-cicd
```

The helper passes `-input=false -force-copy` so it never blocks
on an interactive "Approve state migration?" prompt.

## Force-unlock a stuck state

If a `tofu plan` or `tofu apply` is killed (SIGKILL, laptop
suspend, network drop) the lock leaks. The HTTP backend has no
auto-expiry. To unlock:

```bash
GITLAB_PAT=$(awk -F= '$1=="GITLAB_PAT"{print $2; exit}' .env)
curl -X DELETE -H "PRIVATE-TOKEN: $GITLAB_PAT" \
  "https://gitlab.com/api/v4/projects/84156476/terraform/state/<state-name>/lock"
```

Safe to re-run; if the lock is no longer held, GitLab returns
204 silently.

## Expected plan shape right after migration

On the first `tofu plan` against the cluster roots after
migration, you will see **in-place refresh-only updates** (zero
destroys):

- `cluster-cicd`: `Plan: 0 to add, 5 to change, 0 to destroy`
- `cluster-apps`: `Plan: 0 to add, 2 to change, 0 to destroy`

The 5 + 2 "changes" are:

- `terraform_data.cluster_name_unique` / `vmid_overlap` re-evaluating
  preconditions against the new state lineage.
- `proxmox_virtual_environment_hosts.node[*]` /
  `proxmox_virtual_environment_hosts.vip_reservation` re-writing
  SDN hosts entries to match the current role's effective
  `Sys.Modify` privilege.

No resources are created or destroyed. `tofu apply` to roll these
refresh-only updates through is safe and idempotent.

## Backup

Before the first migration, the helper script prompts you to
back up the local state files into a directory like
`.local-state-backup-<timestamp>/`. This directory is
git-ignored; preserve it offline if you need a recovery point.
