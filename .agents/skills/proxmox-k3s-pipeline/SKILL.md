---
name: proxmox-k3s-pipeline
description: Bring up two k3s clusters (cicd + apps) on a single Proxmox host using OpenTofu, Packer, and an Agent-driven bootstrap. Use when the user says "bring up both clusters", "deploy the pipeline", "run spec 001", "bootstrap a cluster", "scale workers", "decommission a cluster", or "fix the cloudflare fallback". Outputs a fully bootstrapped cluster pair with public HTTPS via Cloudflare Tunnel (no host open ports) and apps->cicd cross-cluster Service consumption via ExternalName.
---

# Proxmox k3s Pipeline

End-to-end pipeline for provisioning two Talos/k3s clusters on a single
Proxmox host. The pipeline drives five numbered
top-level phases; the bootstrap phase (Phase 4) further decomposes
into six ordered sub-phases (talos, k3s, helm, kubeconfig, host_ports,
externalname). Each (sub-)phase has a single CLI entry point and
explicit success criteria that the agent MUST assert before proceeding.

## When to load this skill

Load when the operator asks to bring up, scale, troubleshoot, or
decommission the k3s clusters provisioned by spec 001.

## Glossary (canonical vocabulary)

The bounded context for this skill is in
[CONTEXT.md](./CONTEXT.md). The five canonical terms are:

- **Agent Skill**: this document (the agentskills.io SKILL.md artifact
  loaded by Claude Code, Cursor, etc.).
- **Operator**: the human or AI agent that invokes the skill.
- **Pipeline**: the five-top-level-phase end-to-end sequence (build
  image -> provision cluster -> capture baseline -> bootstrap -> final
  verification).
- **Phase**: one numbered top-level stage of the pipeline. Phase 4
  (bootstrap) further decomposes into six sub-phases.
- **Runbook**: a single-concern copy-pasteable procedure under
  `docs/runbooks/`. Runbooks do not require an Agent; the operator
  follows them directly.

## Step 0a — Pre-flight discovery (MANDATORY before Phase 0)

The skill assumes a cleanroom deployment, but real Proxmox hosts have
arbitrary configuration. Run these discovery probes before any
`tofu apply` or `make build-image`. Each probe has a hard precondition;
halt and surface the failure if the precondition is not met.

### 0a.1 — Reach the Proxmox API

```bash
PVE_URL="${PROXMOX_API_URL:-https://${PVE_HOST}:8006/api2/json}"
curl -kfsS --max-time 10 "$PVE_URL/version" | python3 -c \
  "import json,sys; d=json.load(sys.stdin); print(d['data']['version'], d['data']['release'])"
```

Precondition: HTTP 200 with parseable JSON. If the API is on a
non-default port (some operators front it with `:8086`), set
`PROXMOX_API_URL` accordingly. The `--pve-endpoint` flag and
`PROXMOX_API_URL` env var override the default in every tool.

### 0a.2 — Probe the Proxmox node name

The cluster module's `pve_node` defaults to `proxmox-host`; on the
live host the actual node name is whatever `hostname` reports
(e.g. `BigBertha`, `pve`, `proxmox`). Setting the wrong value
fails at apply time with a `does not exist` error per VM.

```bash
ssh -p "${PVE_SSH_PORT:-6022}" "root@${PVE_HOST}" 'hostname'
```

Set `pve_node = "<actual-name>"` in each cluster root's `main.tf`
(`infra/clusters/cicd/main.tf`, `infra/clusters/apps/main.tf`) before
applying. The module also accepts it via `terraform.tfvars`.

### 0a.3 — Probe the SDN zone and host subnets

The host's `vnet0` (the SDN zone we attach VMs to) typically has a
wide subnet (e.g. `10.0.0.1/8` on this host). The cluster's
`ip_start` CIDR (`10.0.x.0/24`) lives INSIDE that wide subnet, so
`cidrhost(var.ip_start, i)` returns IPs in that range. Two failure
modes to detect up-front:

```bash
ssh -p "${PVE_SSH_PORT:-6022}" "root@${PVE_HOST}" \
  'ip -4 -o addr show | awk "{print \$2, \$4}"'
ssh -p "${PVE_SSH_PORT:-6022}" "root@${PVE_HOST}" \
  'pvesh get /cluster/sdn/vnets --output-format yaml | head -20'
```

Precondition: the per-cluster `ip_start` (default `10.0.1.0/24` for
cicd, `10.0.2.0/24` for apps) MUST NOT overlap any host interface IP.
The cluster module's `vip_in_dhcp_range` precondition only catches
VIP-vs-node-IP collisions; it does NOT catch host-vs-node-IP
collisions because the module does not know about host IPs. If
`10.0.0.1` is the host, do not set `ip_start = "10.0.0.0/24"`.

### 0a.4 — Probe Cloudflare account and zone

WP00 mints a scoped Cloudflare API token that requires the operator's
Cloudflare **zone ID** (not just account ID). The token-creation
endpoint (`POST /user/tokens`) requires user-level authentication —
a `cfat_*` scoped token cannot create child tokens.

```bash
KEY="${CLOUDFLARE_GLOBAL_API_KEY}"; EMAIL="${CLOUDFLARE_GLOBAL_API_EMAIL}"
ACCOUNT="${CLOUDFLARE_ACCOUNT_ID}"

# Verify zone ID (account_id may differ from the zone's owner account_id)
curl -sf -H "X-Auth-Email: $EMAIL" -H "X-Auth-Key: $KEY" \
  "https://api.cloudflare.com/client/v4/zones?name=${CLOUDFLARE_DOMAIN}" \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['result'][0]['id'])"

# Verify permission-group enumeration works (the actual endpoint is plural)
curl -sf -H "X-Auth-Email: $EMAIL" -H "X-Auth-Key: $KEY" \
  "https://api.cloudflare.com/client/v4/accounts/$ACCOUNT/tokens/permission_groups?per_page=400" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(f'{len(d[\"result\"])} groups')"
```

Precondition: zone lookup returns exactly one zone; permission-group
enumeration returns >0 groups. If either fails, the operator has
either (a) the wrong account/zone pairing, or (b) a cfat_* scoped
admin token that lacks the necessary scopes. Switch to a Global API
Key (root-of-account) for WP00.

### 0a.5 — Verify the Proxmox token has `Sys.Modify`

PVE token privileges are bound to the *user*, not root. PAM tokens
(including ones generated via `pveum user token add`) only inherit
the user's ACL — not the implicit `root@pam` `Administrator` role.
The tokens module needs `Sys.Modify` to create roles. If your
bootstrap token lacks it, the apply fails with HTTP 403 on
`/access/roles`.

```bash
ssh -p "${PVE_SSH_PORT:-6022}" "root@${PVE_HOST}" \
  'pvesh get /access/roles/PVEAdmin --output-format yaml | head'
```

PVEAdmin grants `Sys.Audit`, `Sys.Console`, `Sys.Syslog` — but NOT
`Sys.Modify`. Only the `Administrator` role (root@pam's implicit
role) has `Sys.Modify`. For WP00, either:

- (A) Use a `root@pam!tf-bootstrap` token (full Administrator role)
- (B) Grant `Sys.Modify` on `/` to the bootstrap user via
      `pveum acl modify / --roles PVEAdmin --users ...` (PVEAdmin does
      NOT include Sys.Modify, so this won't work) — use the explicit
      `--privs Sys.Modify` form: not directly supported in pveum;
      you must use the `Administrator` role.

The pragmatic answer: use a `root@pam!tf-bootstrap` token, scoped to
this apply, then delete it after WP00 lands.

### 0a.6 — Confirm required `.env` keys

Before running `scripts/apply.sh`, ensure `.env` contains ALL of:

| Key | Purpose | Required for |
|---|---|---|
| `CLOUDFLARE_TOKEN_CREATOR` | cfat_* scoped admin token (used for permission-group enumeration if it has `Account:API Tokens:Read`) | WP00 |
| `CLOUDFLARE_GLOBAL_API_KEY` | Account-level Global API Key (used for `POST /user/tokens` which requires user-level auth) | WP00 |
| `CLOUDFLARE_GLOBAL_API_EMAIL` | Email tied to the Global API Key | WP00 |
| `CLOUDFLARE_ACCOUNT_ID` | Account under which to mint the scoped token | WP00 |
| `CLOUDFLARE_ZONE_ID` | Zone to scope DNS-edit permissions on the child token | WP00 |
| `CLOUDFLARE_DOMAIN` | Human-readable domain (informational only) | WP00 |
| `PROXMOX_API_URL` | Proxmox API endpoint, e.g. `https://kvm.example:8006/api2/json` | WP00+ |
| `PROXMOX_API_TOKEN` | `USER@REALM!TOK=secret` form | WP00+ |

If `CLOUDFLARE_GLOBAL_API_KEY` is missing, the tokens module falls
back to `CLOUDFLARE_TOKEN_CREATOR` only — which will fail at
`POST /user/tokens` with `403 Forbidden (Valid user-level
authentication not found)`. Plan accordingly.

### 0a.7 — Stale terminal env-var trap

Each `run_in_terminal` opens a fresh shell that inherits the parent
terminal's env. If a previous run set `TF_VAR_*` values (e.g. an
old `terraform-bootstrap@pam!temp` token), those persist across
runs and silently override the values you just wrote to `.env`.
Before sourcing `.env`, ALWAYS:

```bash
unset $(env | grep -E "^TF_VAR_" | cut -d= -f1)
set -a; source .env; set +a
```

Then translate `PROXMOX_API_TOKEN` into `TF_VAR_proxmox_api_token_id`
+ `TF_VAR_proxmox_api_token_secret`:

```bash
export TF_VAR_proxmox_api_url="$PROXMOX_API_URL"
export TF_VAR_proxmox_endpoint="$PROXMOX_API_URL"
_proxmox_id="${PROXMOX_API_TOKEN%%=*}"
_proxmox_secret="${PROXMOX_API_TOKEN#*=}"
export TF_VAR_proxmox_api_token_id="${_proxmox_id}"
export TF_VAR_proxmox_api_token_secret="${_proxmox_secret}"
```

Note: bash history-expansion mangles `!` in unquoted strings. Always
single-quote the token id: `'k3s-terraform@pam!tf'`. See Step 0c
for the apply-time equivalent.

### 0a.8 — Avoid the imported-token-no-secret trap

`tofu import proxmox_user_token.k3s_terraform_tf k3s-terraform@pam!tf`
brings the token resource into state, but Proxmox does not return
the secret value for an existing token (it only prints the secret
once, at creation time). The state will record `value = null`,
making the child token unusable.

If you ever `import` a token, immediately:

```bash
ssh root@$PVE_HOST 'pveum user token delete k3s-terraform@pam tf'
tofu state rm proxmox_user_token.k3s_terraform_tf
tofu apply -auto-approve   # re-creates and emits value
```

## Step 0 — Load the context7-auto-research gate (MANDATORY)

Before invoking any external library, load
`.agents/skills/context7-auto-research/SKILL.md` and run
`context7-auto-research` for each library the pipeline touches.
**Do NOT rely on training data for library APIs.** The pipeline
uses the following pinned versions; record the rationale for each
in the operator's reply before invoking the library:

| Library | Version | Rationale (context7) |
|---|---|---|
| `bpg/proxmox` (OpenTofu provider) | `0.111.1` | rationale: latest stable that exposes `proxmox_cloned_vm`; v0.111.1 introduces the `host` attribute that the WP02 module uses |
| `hashicorp/proxmox` (Packer plugin) | `1.2.3` | rationale: latest stable Packer plugin; required for `packer init` to discover the `proxmox-iso` builder |
| `STRRL/cloudflare-tunnel-ingress-controller` (Helm chart) | `0.0.23` | rationale: only stable version on the strrl chart repo as of 2026-07; pinned because the upstream CRDs are still alpha |
| `cilium` (Helm chart) | `1.16.x` | rationale: matches the Talos 1.10.x kernel constraint and supports `gatewayAPI.enabled` plus eBPF host routing |
| `sergelogvinov/proxmox-cloud-controller-manager` (Helm chart) | `0.14.0` | rationale: latest stable; required for `topology.kubernetes.io/region` + `zone` labels on the apps cluster nodes |
| `sergelogvinov/proxmox-csi-plugin` (Helm chart) | `0.5.9` | rationale: chart 0.5.9 supports PVE 9.x and lvm-thin on `data1/data1` |
| `talosctl` | `1.10.x` | rationale: matches the Talos image baked by SS1; required for `talosctl apply-config` and `talosctl kubeconfig` |
| `k3s` | `1.34.x` | rationale: matches the Cilium + kube-vip versions; no known CVEs |
| `helm` | `3.x` | rationale: required for `helm upgrade --install`; matches what k3s 1.34 ships |

Document each library's rationale in the operator's reply **before**
calling the library.

## Step 0b — WP00 apply-time gotchas (live-host lessons)

These are deployment-environment issues that surfaced only when
applying WP00 against a real PVE 9.2.3 + Cloudflare account. Read
this before `scripts/apply.sh`.

### 0b.1 — Cloudflare provider auth (ExactlyOneOf)

The Cloudflare provider v5 schema enforces
`ExactlyOneOf(api_key, api_token)`. Passing both is a schema
violation; passing neither fails with "Valid user-level
authentication not found". The tokens provider block must pick
exactly one based on what's available:

- `CLOUDFLARE_GLOBAL_API_KEY` set → use `api_key + email`
  (Global API Key has full account scope, can mint child tokens)
- `CLOUDFLARE_GLOBAL_API_KEY` NOT set → use `api_token` (cfat_*)
  (lacks child-token mint; abort and tell the operator)

Don't try to satisfy both fields. Pick one auth method.

### 0b.2 — Cloudflare resource key format

Cloudflare's API token `resources` field is a JSON-encoded object
whose keys are scope expressions:

- Zone-scoped (DNS, Zone settings): `com.cloudflare.api.account.zone.<zone_id>`
- Account-scoped (Tunnel, R2): `com.cloudflare.api.account.<account_id>`

NOT `account.id` (this is a key in the API **response**, not the
resource key in the policy). NOT `account.*` (literal glob is
rejected). The provider will forward whatever you set; Cloudflare
will reject malformed keys with `"X is not a valid match-all
object expression"` or `"X is not a valid resource name"`.

### 0b.3 — Cloudflare permission-group ID format

The provider's
`cloudflare_account_api_token_permission_groups_list` data source
hits `/accounts/{id}/token/permission_groups` (singular `token`),
but Cloudflare's actual endpoint is
`/accounts/{id}/tokens/permission_groups` (plural). With account-
scoped cfat_* tokens, even the right endpoint returns `[]` because
the token lacks `Account:API Tokens:Read`. Two consequences:

- The data sources WILL return empty for typical cfat_* tokens.
  Plan must succeed anyway.
- The HCL has fallback UUIDs for the three groups we need
  (Zone Read, DNS Write, Cloudflare Tunnel Write). These UUIDs
  are stable per Cloudflare's registry — verified 2026-07-06.

The three UUIDs you can hardcode as fallback (Cloudflare registry,
2024+):

| Label | UUID |
|---|---|
| Zone Read | `c8fed203ed3043cba015a93ad1616f1f` |
| DNS Write | `4755a26eedb94da69e1066d98aa820be` |
| Cloudflare Tunnel Write | `c07321b023e944ff818fec44d8203567` |

If Cloudflare ever rotates a UUID, re-fetch the list with the
global key and update.

### 0b.4 — IP-lock condition format

Cloudflare rejects `0.0.0.0/0` in `condition.request_ip.in` with
`"invalid CIDR"`. Two safe patterns:

- (A) If `cloudflare_runner_cidr` is unset AND the apply-runner IP
  lookup fails, OMIT the `condition` block entirely (the token is
  unrestricted by IP — acceptable for minimal-permission tokens).
- (B) If `cloudflare_runner_cidr` is set (e.g. CI runner), use it
  as-is.

### 0b.5 — Proxmox role-creation privilege

`PVEAdmin` does NOT include `Sys.Modify`. Only `Administrator`
(root@pam's implicit role) has it. WP00 needs `Sys.Modify` to
create the `k3s-cluster` role. Two paths:

- (A) Bootstrap with a `root@pam!tf-bootstrap` token (recommended;
  one-shot, delete after WP00 lands).
- (B) Pre-create the role manually with `pvesh`/`pveum` and
  `tofu import` it — but see Step 0a.8 for the imported-no-secret
  trap.

### 0b.6 — OpenTofu `-chdir=` vs Terraform `-C`

OpenTofu uses `-chdir=DIR` for the subcommand-level directory
switch. Terraform `-C` is **not** supported. Makefile recipes
that loop over modules must use `tofu -chdir=$$d ...`, not
`tofu -C $$d ...`.

### 0b.7 — Bootstrap user TTL

The `terraform-bootstrap@pam` user (or any user created solely to
mint WP00 tokens) should be deleted after WP00 lands. The
bootstrap token (`root@pam!tf-bootstrap`) should also be deleted
to leave only the scoped child tokens (`k3s-terraform@pam!tf`)
in production.

```bash
ssh root@$PVE_HOST 'pveum user token delete root@pam tf-bootstrap'
# root@pam itself stays — you need it for any future admin operations
```

## Step 0c — Library-version pins for environment tooling

In addition to the pipeline libraries (Step 0 table), the
**deployment environment** requires these pinned tools. Mismatched
versions cause silent API differences.

| Tool | Required | Notes |
|---|---|---|
| `tofu` (OpenTofu) | `>= 1.6.0` | `-chdir=` syntax requires 1.6+ |
| `packer` | `>= 1.10` | `proxmox-iso` builder uses post-1.10 schema |
| `talosctl` | `1.10.x` | Matches the Talos image baked by SS1 |
| `helm` | `3.x` | Matches what k3s 1.34 ships |
| `kubectl` | `>= 1.30` | For bootstrap phase verification |

## Step 0d — Phase 0: Token provisioning (SS0 / WP00)

WP00 runs **once** before any cluster provisioning. It mints a
scoped Proxmox API token and a scoped Cloudflare API token, then
writes both to `infra/tokens/output.json` (mode `0600`). All
downstream phases read from this file.

**Pre-flight**: complete Step 0a.1 through 0a.7 BEFORE running.
The most common failure is missing `CLOUDFLARE_GLOBAL_API_KEY`
(Step 0a.6).

**Apply**

```bash
scripts/apply.sh
```

The wrapper reads `.env`, translates `PROXMOX_API_TOKEN` into
`TF_VAR_proxmox_api_token_id` + `TF_VAR_proxmox_api_token_secret`,
and runs `tofu init -backend=false && tofu apply -auto-approve`.

If you see `403 Forbidden (Valid user-level authentication not
found)`, the Cloudflare provider is using the cfat_* token instead
of the global key. See Step 0b.1.

Success criteria (assert ALL before proceeding):
1. `cat infra/tokens/output.json` exits 0; file mode is `0600`.
2. `jq '.proxmox_token_secret, .cloudflare_scoped_token' infra/tokens/output.json`
   returns non-null for both keys.
3. `tofu test` in `infra/tokens/` exits 0 (6/6).
4. `ssh root@$PVE_HOST 'pvesh get /access/users/k3s-terraform@pam'`
   returns the user; the user has `k3s-cluster` role bound.

Failure handling: halt. Surface the structured `error` +
`resolution` keys from the failure. Do NOT proceed to Phase 1 with
a partial token state.

**Cleanup after WP00 lands**

Once the downstream phases can authenticate via
`k3s-terraform@pam!tf` (verified by the cluster-roots' `tofu plan`),
delete the bootstrap token. The `terraform-bootstrap@pam` user (if
you created one) can also go.

```bash
ssh root@$PVE_HOST 'pveum user token delete root@pam tf-bootstrap'
# root@pam itself stays — needed for future admin operations
```

## Step 0e — State backend (GitLab HTTP, 2026-07-06)

All four tofu stacks (`infra/tokens`, `infra/modules/proxmox-k3s-cluster`,
`infra/clusters/cicd`, `infra/clusters/apps`) now store state in a
GitLab-managed Terraform state backend instead of local files.

### 0e.1 — Why GitLab HTTP and not local?

Local `terraform.tfstate` was acceptable during the one-operator
dev loop, but for the multi-developer / CI-driven phase we need:
- A single source of truth for the live cluster topology.
- State locking that doesn't depend on the operator's laptop disk.
- A versioned history (`serial` + `lineage`) visible in the
  GitLab UI alongside the source code.

### 0e.2 — Project layout

- Project: `infra-state/bigbertha` at `gitlab.com`
- Project ID: `84156476`
- Per-stack state name (4 names):
  - `infra-tokens` -> `infra/tokens/`
  - `proxmox-k3s-cluster-module` -> `infra/modules/proxmox-k3s-cluster/`
  - `cluster-cicd` -> `infra/clusters/cicd/`
  - `cluster-apps` -> `infra/clusters/apps/`

The three stateful stacks each get a name; the module has a name
allocated but never carries state because modules do not store
state. We keep the `backend "http" {}` block OUT of the module to
avoid a noisy "backend ignored" warning at every init (see 0e.6).

### 0e.3 — Required env: `GITLAB_ACCESS_TOKEN`

The token MUST be a GitLab personal access token (PAT) with the
`api` scope, owned by an infra-state/bigbertha project Owner. The
glab CLI's cached OAuth token (auto-rotated, expires daily) is
NOT suitable. Create one at:
  https://gitlab.com/-/user_settings/personal_access_tokens

Store it in `.env` as `GITLAB_PAT=glpat-...`. Export it as
`GITLAB_ACCESS_TOKEN` before invoking the helper:
```bash
export GITLAB_ACCESS_TOKEN=$(awk -F= '$1=="GITLAB_PAT"{print $2; exit}' .env)
```

### 0e.4 — Init / migrate via `scripts/gitlab_backend.sh`

The helper at `scripts/gitlab_backend.sh` wraps `tofu init` with
the right `-backend-config=` flags for the chosen stack. Usage:

```bash
# First-time migration (local state -> GitLab):
./scripts/gitlab_backend.sh init infra-tokens       --migrate-state
./scripts/gitlab_backend.sh init cluster-cicd       --migrate-state
./scripts/gitlab_backend.sh init cluster-apps       --migrate-state
./scripts/gitlab_backend.sh init proxmox-k3s-cluster-module   # no state, plain init

# Subsequent runs (re-init after a config change, e.g.):
./scripts/gitlab_backend.sh init cluster-cicd

# Audit the flags the script would use:
./scripts/gitlab_backend.sh show cluster-cicd
```

The helper always passes `-input=false -force-copy` so it never
blocks on an interactive "Approve state migration?" prompt. The
GITLAB_ACCESS_TOKEN value is masked in the audit `show` output.

### 0e.5 — Force-unlock recipe

If a `tofu plan` or `tofu apply` is killed (SIGKILL, laptop
suspend, network drop) the lock leaks. The HTTP backend has no
auto-expiry. To unlock a stuck state, hit the GitLab API:

```bash
GITLAB_PAT=$(awk -F= '$1=="GITLAB_PAT"{print $2; exit}' .env)
curl -X DELETE -H "PRIVATE-TOKEN: $GITLAB_PAT" \
  "https://gitlab.com/api/v4/projects/84156476/terraform/state/<state-name>/lock"
```

This is safe: if the lock is no longer held, GitLab returns 204
silently.

### 0e.6 — Gotcha: `backend` block in modules is ignored with warning

Per OpenTofu docs, a `terraform { backend "http" {} }` block in
a module is silently ignored with a warning ("Any selected backend
applies to the entire configuration, so OpenTofu expects provider
configurations only in the root module"). State for instances of
the module lives in the calling root. We therefore omit the
`backend "http" {}` block from
`infra/modules/proxmox-k3s-cluster/versions.tf` and document the
design intent in that file's comments.

### 0e.7 — Path-drift expected after migration

The `output "tokens_output_path"` in `infra/tokens/outputs.tf`
computes `abspath(local_sensitive_file.tokens_output.filename)`.
This embeds the absolute path of the workspace at apply time. The
state was originally applied from a different mount path (some
operators have `/mnt/data/Projects/proxmox-k8s-cicd` and
`/home/bruj0/projects/proxmox-k8s-cicd` pointing at the same
physical directory via different paths). On the first plan after
migration you will see a `~ tokens_output_path` update from the
old path to the current path. This is cosmetic; the value still
points at the same `output.json` file.

### 0e.8 — In-place refresh-only updates after migration

`tofu plan` against the cicd and apps roots after migration shows
`Plan: 0 to add, 5 to change, 0 to destroy` (cicd) and
`0 to add, 2 to change, 0 to destroy` (apps). The "changes" are
all in-place refreshes of:
- `terraform_data.cluster_name_unique` and
  `terraform_data.vmid_overlap` (re-evaluate preconditions)
- `proxmox_virtual_environment_hosts.node[*]` and
  `proxmox_virtual_environment_hosts.vip_reservation`
  (re-write SDN hosts entries to match the new role's effective
  `Sys.Modify` privilege - no VM lifecycle impact)

No resources are created or destroyed. Apply these refresh-only
updates once to align state with the live host's current role
privilege set.

## Step 1 — Phase 1: Build the VM image (SS1)

Goal: bake a Talos Linux golden image into a Proxmox template
(VMID 900). One-shot; idempotent on rerun (image-id.txt already
exists -> no-op).

**Pre-flight (live host only, skip on cleanroom)**

The Packer `proxmox-iso` workflow in `tools/packer/talos.pkr.hcl`
expects a base VM named `talos-base` at VMID 999, configured with
the Talos ISO attached. If this VM does not exist on the host, the
Packer build fails at the clone step. For the FIRST run on a clean
host, manually:

```bash
ssh root@$PVE_HOST 'qm create 999 --name talos-base --memory 2048 \
  --cores 2 --net0 virtio,bridge=vnet0 --scsihw virtio-scsi-pci \
  --scsi0 data1:32 --ide2 local:iso/talos-<version>.iso,media=cdrom \
  --boot order=ide2 --ostype l26'
```

Where `data1` is the storage name returned by
`pvesm status | head`. The Talos ISO must be uploaded to local
storage first:

```bash
scp talos-v1.10.0-amd64.iso root@$PVE_HOST:/var/lib/vz/template/iso/
```

**Apply**

```bash
make build-image
```

This runs `tools/build_image.py` which orchestrates Packer.
Required env: `PVE_ENDPOINT`, `PVE_TOKEN_ID`, `PVE_TOKEN_SECRET`,
`PVE_NODE` (default `proxmox-host` — see Step 0a.2 to override).

Success criteria (assert ALL before proceeding):
1. `qm list | grep -w 900` returns a single row with `template` column
   equal to `yes`.
2. `cat build/image-id.txt` returns exactly `900` followed by newline.
3. `tools/build_image.py --audit-log build/audit.log` exits 0.

Failure handling: halt the pipeline and surface the structured error
(`error`, `resolution` keys) to the operator. Do NOT proceed to
Phase 2.

## Step 1b — Phase 1 apply-time gotchas (live host 2026-07-06)

These are the deployment-environment issues that surfaced ONLY
when running the SS1 Packer build against PVE 9.2.3 with the
bpg/proxmox tokens. They are independent of the WP00-preflight
gotchas in Step 0b; read BOTH before invoking the build.

### 1b.1 — Packer schema drift from upstream wiki examples

The template `tools/packer/talos.pkr.hcl` shipped with
`local-lvm` for efi + disk storage, `clone_from_vm_id` for the
clone source, `vm_template_name` for the output name, and the
`formatdate(\"...\", ...)` HCL interpolation. None of these are
accepted by hashicorp/proxmox v1.2.3 (the version our environment
pins per Step 0c).

Required patches before `packer init`:

- `efi_storage_pool = "local-lvm"` → `"data1"` (the lvmthin
  pool BigBertha uses; `local-lvm` doesn't exist on non-default
  PVE installs)
- `disks { storage_pool = "local-lvm" ... }` → `"data1"` (same
  reason; lvmthin only accepts `raw` format which we already set)
- `clone_from_vm_id = "999"` → `clone_vm_id = "999"` (the
  v1.2.x schema argument)
- `vm_template_name = "..."` → `template_name = "..."` (the
  v1.2.x schema argument)
- `template_description = "...${formatdate(\"YYYY-MM-DD\",
  timestamp())}"` → drop the formatdate call (the escape is
  invalid inside an HCL string-interpolation; a literal string
  suffix is fine for our purposes)

Without these patches, `packer init` exits with `Error:
Unsupported argument (clone_from_vm_id / vm_template_name)` and
the build never reaches PVE.

### 1b.2 — Packer token auth format: `token` is the BARE secret

hashicorp/proxmox v1.2.x builds the PVEAuth header as
`PVEAPIToken=<username>=<token>`. Where:

- `username` = `"USER@REALM!TOKENID"` (includes the trailing
  `!tokenid`)
- `token` = the BARE secret UUID — NOT `USER@REALM!TOKENID=secret`

Our pre-WP00 template had
`token = "${var.pve_token_id}=${var.pve_token_secret}"`. The
plugin then sends
`PVEAPIToken=k3s-terraform@pam!tf=k3s-terraform@pam!tf=<uuid>` —
double-prefixed and rejected by PVE with `401 Authentication
failed`. Direct `curl` works fine, only Packer fails, because
Packer is constructing the concatenation itself.

**Fix**: `token = var.pve_token_secret`. The PVE side then
parses it correctly. Verified on BigBertha 2026-07-06 (curl and
Packer both succeed with `e07d2de1-3eb8-46ea-831e-75a9858bd465`
as the secret and `k3s-terraform@pam!tf` as the username).

### 1b.3 — K3s-cluster role needs 7 extra privs for Packer

The 12-priv spec T005 set is **insufficient** for Phase 1.

Each of these returns `403 Permission check failed` on BigBertha
without the priv:

| Priv | Needed for |
|---|---|
| `Sys.Audit` | `/access` namespace reads |
| `VM.Audit` | Read VM 999 cfg / qemu list |
| `VM.Clone` | Clone VMID 999 → 900 |
| `VM.Migrate` | Cleanup moved/half-baked templates |
| `VM.Config.CDROM` | Attach/detach Talos ISO via API |
| `VM.Config.HWType` | Set `machine=q35` (UEFI boot) |
| `VM.Snapshot.Rollback` | Restore after template-bake failure |

Update `infra/tokens/proxmox.tf` privileges list to **19 entries**
(12 spec + 7 above). Verify with
`ssh root@$PVE_HOST 'pvesh get /access/roles/k3s-cluster'`.

### 1b.4 — `output.json` `proxmox_token_secret` is a FULL token

The downstream infra plan is `proxmox_token_id` (the
`USER@REALM!TOKENID` prefix) + `proxmox_token_secret` (just the
bare UUID secret) so consumers can build the PVEAuth header as
`<id>=<secret>`. But bpg/proxmox v0.111.x's
`proxmox_user_token.<...>.value` attribute is the **FULL api-token
string** in `USER@REALM!TOKENID=secret` form — not just the
secret.

The original migration kept the telmate/proxmox-era contract and
wrote `proxmox_token_secret = <...>.value`, which produced a value
of `k3s-terraform@pam!tf=e07d2de1-...` in `output.json`. Downstream
consumers that concatenate `${id}=${secret}` end up with
`<id>=<id>=<secret>` and break. PVEAuth rejects with `401`.

**Fix** ([infra/tokens/output_json.tf](../../infra/tokens/output_json.tf)):
split `value` on `=` inside a `locals {}` block and write only
the second part. Verified on BigBertha 2026-07-06 —
`output.json.proxmox_token_secret` is a 36-char UUID.

### 1b.5 — `proxmox-clone` builder hardcodes SSH-wait

The hashicorp/proxmox v1.2.x `proxmox-clone` builder ALWAYS
starts the cloned VM and waits for SSH to become available
(5 minute default timeout). This is incompatible with **Talos in
installer mode** (no SSH server).

Even with our base VM correctly cloned and a fresh `vm_id=900`
VM booted, `packer build` reaches `Step "StepConnect" failed,
aborting... Timeout waiting for SSH.` after 5m22s. Packer does
**not** honour `ssh_wait_timeout = "0s"` for the clone variant
in v1.2.3 (only `proxmox-iso` respects it).

**Two acceptable paths**:

(a) **Bypass Packer for this step.** Use the PVE REST API
directly to clone VM 999 → VM 900 as a `full=1` clone and POST
`/nodes/{node}/qemu/{vmid}/template`. Talos installed or not, the
resulting VMID 900 has `template: 1` in its config and is usable
for SS2. Achieved on 2026-07-06.

```bash
curl -X POST $PROXMOX_API_URL/nodes/BigBertha/qemu/999/clone \
  -H "Authorization: PVEAPIToken=$PROXMOX_API_TOKEN" \
  --data-urlencode newid=900 --data-urlencode name=talos-template \
  --data-urlencode full=1 --data-urlencode target=BigBertha \
  --data-urlencode storage=data1 --data-urlencode format=raw

curl -X POST $PROXMOX_API_URL/nodes/BigBertha/qemu/900/template \
  -H "Authorization: PVEAPIToken=$PROXMOX_API_TOKEN"

echo 900 > build/image-id.txt
```

(b) Replace `proxmox-clone` with `proxmox-iso` and bake Talos
to disk via the `http_content` server (a flag-pushed ISO
boot_cmd). Larger change; not done on this run.

**Recommendation**: use path (a) until Talos provides a
`proxmox-iso`-compatible boot path. The Phase 2 cluster tofu
modules only need a PVE template whose disk has Talos' UEFI
bootloader intact; the cluster-bootstrap applies the right
`talosctl apply-config` to install Talos to disk on the
cluster VMs at first boot.

### 1b.6 — Pre-allocate VMID 999 with exact storage setup

Create the talos-base VMID 999 by hand BEFORE the first apply.
The Packer template hardcodes `efi_storage_pool=data1`,
`storage_pool=data1`, `disk_size=20G`, `format=raw`. The base VM
must therefore use the same storage pool and at least the same
disk size to clone cleanly:

```bash
ssh -p 6022 root@kvm.bruj0.net qm create 999 \
  --name talos-base --memory 2048 --cores 2 \
  --net0 virtio,bridge=vnet0 --scsihw virtio-scsi-single \
  --cpu host --machine q35 --bios ovmf --ostype l26 \
  --efidisk0 data1:1,efitype=4m,pre-enrolled-keys=1 \
  --scsi0 data1:32,discard=on,iothread=on,ssd=1 \
  --ide2 local:iso/talos-v1.10.0-amd64.iso,media=cdrom \
  --boot order=ide2 --agent enabled=1
```

Talos ISO file name MUST match the live host's uploaded ISO.
Asset name on GitHub releases is `metal-amd64.iso`, NOT
`talos-amd64.iso`; the SKILL pre-flight above used the old name
on 2026-07-06 and the operator must rename on copy.

## Step 2 — Phase 2: Provision the cicd cluster (SS2)

Goal: apply OpenTofu against `infra/clusters/cicd/` to create 3 control-plane
+ N worker Talos VMs and render `output.json` +
`manifests/traefik-helmchartconfig.yaml`.

```bash
unset $(env | grep -E "^TF_VAR_" | cut -d= -f1)
TOKEN_ID=$(jq -r .proxmox_token_id infra/tokens/output.json)
SECRET=$(jq -r .proxmox_token_secret infra/tokens/output.json)
ENDPOINT=$(jq -r .pve_endpoint infra/tokens/output.json)
export PROXMOX_VE_API_TOKEN="${TOKEN_ID}=${SECRET}"
export PROXMOX_VE_ENDPOINT="${ENDPOINT}"

cd infra/clusters/cicd
tofu init -input=false
tofu apply -auto-approve -input=false
```

Success criteria (assert ALL before proceeding):
1. `tofu apply` exits 0 and `infra/clusters/cicd/output.json`
   exists and parses as JSON with `cluster_name`, `vip`,
   `pod_cidr`, `svc_cidr`, and `nodes[]` keys.
2. `infra/clusters/cicd/manifests/traefik-helmchartconfig.yaml` exists and
   parses as YAML (kustomize-compatible schema).
3. `tofu test` exits 0 (no warnings about VMID overlap with apps).

## Step 2b — Phase 2 apply-time gotchas (live host 2026-07-06)

Six more deployment-environment issues surfaced during the BigBertha
Phase-2 apply (PVE 9.2.3, k3s-terraform role, bpg/proxmox
proxmox_cloned_vm). These are independent of Step 0b / Step 1b.

### 2b.1 — `output.json` must carry the spec-T007 cluster-root keys

The cluster root (`infra/clusters/cicd/main.tf`) reads
`cf_api_token` and `cf_account_id` from
`infra/tokens/output.json` (per `specs/.../tasks.md:106`). The
post-refactor `infra/tokens/output_json.tf` was emitting
`cloudflare_scoped_token` / `cloudflare_account_id` instead;
`jsondecode(...)` returned `null` and tofu rejected the cluster
module at apply with "argument must not be null". Fix: emit
**both** the canonical `cf_*` keys and the legacy `cloudflare_*`
aliases. Verified 2026-07-06 — `jq -r '.cf_api_token, .cf_account_id'
output.json` returns non-null. Pinned by
`tools/tests/test_agent_skill.py::test_skill_documents_output_json_secret_split`
and the new `test_skill_documents_cf_api_token_contract` in Step 2b.

### 2b.2 — `bpg/proxmox` proxmox_cloned_vm needs explicit `target_datastore`

`disk.scsi0.datastore_id = "local-lvm"` is hardcoded in the
upstream module template. BigBertha has no `local-lvm` lvmthin
pool, and the bpg/proxmox v0.111.x provider stored
`local-lvm` in plan but the cloned VM ended up on `data1`
(cloned-from storage), causing `Provider produced inconsistent
result after apply`. Two-fold fix:
- module `main.tf`: pin the clone `target_datastore` to a new
  `var.disk_storage_pool` (default `data1`).
- cluster root: pass `disk_storage_pool = "data1"`.

### 2b.3 — `k3s-cluster` role needs `Sys.Modify` for SDN writes

The cluster module calls `proxmox_virtual_environment_hosts` to
write the vnet0 hosts file (static DHCP reservations + VIP).
PVE 9.2.x rejects the write with
`403 Permission check failed (/nodes/<node>, Sys.Modify)`
unless the role carries `Sys.Modify` — `SDN.Use` alone is
insufficient. Add `Sys.Modify` to the privileges list (now
**20 privs** total: 12 spec T005 + 7 Phase-1 + 1 Phase-2). Apply
via `scripts/apply.sh`, then re-apply the cluster roots.

### 2b.4 — `output.json` MUST expose `pod_cidr` and `svc_cidr`

The cluster root's SC says `output.json` keys include `pod_cidr`
and `svc_cidr` (consumed by `tools/lib/talos_client.py` to wire
`--skip-rbac=false --network-cidr=` for the per-node Talos
configs). The module originally wrote them only to tofu
*outputs*, NOT to the `local_sensitive_file.cluster_output`
body. Fix: add `pod_cidr = var.pod_cidr` /
`svc_cidr = var.svc_cidr` keys to the `jsonencode` in
`infra/modules/proxmox-k3s-cluster/outputs.tf`. Verified 2026-07-06.

### 2b.5 — `manifests/` subdirectory required by `tools/lib/helm_client.py`

`tools/lib/helm_client.py:208` reads
`infra/clusters/<name>/manifests/traefik-helmchartconfig.yaml`,
not the cluster-root file. The module originally wrote the
traefik config to the cluster root. Fix: module writes under
`${path.module}/../../clusters/<name>/manifests/traefik-...`.
The operator must pre-create `infra/clusters/<name>/manifests/`
or tofu errors with "parent directory does not exist".

### 2b.6 — Sibling-cluster collator must tolerate non-JSON siblings

The cluster root's `data.local_file.sibling_outputs` globs
`${path.module}/../*/output.json` and unconditionally
`jsondecode`s the content of each match. At plan time the live
host's `build/image-id.txt` may match the glob (the tofu test
mock's default content `"900\n"` also doesn't parse). Fix: wrap
`jsondecode` in `try(...)` and filter out unparseable siblings,
so the sibling-collision precondition only considers real
`output.json` files. Verified 2026-07-06.

### 2b.7 — Module `${path.module}/../clusters/<name>` paths break post-refactor

The 2026-07-05 file-layout refactor moved the module from
`clusters/<name>/modules/proxmox-k3s-cluster/` to
`infra/modules/proxmox-k3s-cluster/`. Three files were missed:
- `outputs.tf::cluster_output.filename`
- `outputs.tf::talos_dir`
- `outputs.tf::talos_machineconfig.filename`
- `traefik.tf::traefik_chartconfig.filename`
- `talos.tf::talos_machineconfig.filename`

All had `path.module/../clusters/<name>/...` which resolved to
`infra/modules/clusters/<name>/...` (a non-existent path).
Fix: `${path.module}/../../clusters/<name>/...` (which has two
`..` segments under path.module). Apply the same patch for the
cluster-root's `data.local_file.image_id` — change
`${path.module}/../../build/image-id.txt` to
`${path.module}/../../../build/image-id.txt` (three `..`
segments, since the cluster root is one level deeper than the
module). Verified 2026-07-06.

## Step 3 — Phase 3: Capture host-ports baseline (M2 setup)

This is a one-shot baseline capture. Run BEFORE the first cluster
bootstrap, then never again unless the operator is decommissioning and
recreating the cluster from scratch.

```bash
PVE_SSH=root@10.0.0.1 PVE_SSH_PORT=6022 \
  ./scripts/capture_host_ports_baseline.sh infra/clusters/cicd
```

Success criteria: `infra/clusters/cicd/host_ports_baseline.txt` exists and
contains the literal substring `chain prerouting`.

## Step 4 — Phase 4: Bootstrap (SS3)

Runs the six-phase bootstrap. Order is enforced by `PHASES` in
`tools/bootstrap_cluster.py`. Each phase records its success in
`infra/clusters/<name>/bootstrap_state.json`; rerunning is a no-op for
completed phases.

For the cicd cluster:

```bash
python tools/bootstrap_cluster.py --cluster cicd
```

For the apps cluster (after cicd is healthy AND `infra/clusters/apps/`
has been provisioned by Phase 2-equivalent):

```bash
python tools/bootstrap_cluster.py --cluster apps
```

The six phases, in order:

1. `talos` — `talosctl apply-config` on every node, wait for
   healthy, bootstrap k3s.
2. `k3s` — verify `/healthz` returns `ok`.
3. `helm` — install Cilium + kube-vip (WP04) and the remaining four
   releases (proxmox-ccm, proxmox-csi, cloudflare-tunnel,
   cert-manager, WP05) + apply the rendered Traefik HelmChartConfig.
4. `kubeconfig` — pull admin kubeconfig, merge into
   `~/.kube/config`.
5. `host_ports` — assert no new DNAT rules have been added to the PVE
   nft prerouting chain (M2 misfit verifier).
6. `externalname` — apps-cluster only: apply the cross-cluster
   ExternalName Services kustomization (WP06).

Idempotency: on a rerun, the script reads
`infra/clusters/<name>/bootstrap_state.json` and skips phases whose name
appears in `phases_done`. This is the canonical "convergence from
partial state" path required by NFR-011. **Idempotency is the contract;
the operator may safely rerun the bootstrap at any point.**

Success criteria (assert ALL before proceeding):
1. `kubectl --context cicd get nodes` shows all control-plane +
   worker nodes in `Ready` state.
2. `kubectl --context cicd -n kube-system get pods --all-namespaces`
   shows Cilium + kube-vip + proxmox-ccm + proxmox-csi +
   cloudflare-tunnel + cert-manager pods `Running`.
3. `python tools/bootstrap_cluster.py --cluster cicd --phases all`
   exits 0 in <60 seconds (idempotent rerun).

## Step 5 — Phase 5: Final verification (SC-001..SC-006)

Run the verification matrix in `docs/verification.md`:

- **SC-001**: clean-room end-to-end bring-up completes in <=60 min.
- **SC-002**: PVC + Deployment succeeds on both clusters.
- **SC-003**: Ingress of class `cloudflare-tunnel` resolves via
  Cloudflare within 60 s.
- **SC-004**: `nft list chain ip nat prerouting` shows zero new DNAT
  rules.
- **SC-005**: rerun idempotency — tofu apply + bootstrap_cluster.py
  on a fully-bootstrapped cluster converges to no-op in <60 s.
- **SC-006**: `tofu destroy` cleanly removes all VMs.

NFRs verified at this phase:
- **NFR-010**: this SKILL.md has YAML frontmatter with `name` and
  non-empty `description` (test: `tools/tests/test_agent_skill.py`).
- **NFR-011**: rerun idempotency (covered above).
- **NFR-012**: every external library mentioned with version pin and
  rationale (Step 0 table; test: `tools/tests/test_agent_skill.py`).
- **NFR-013**: resource budget <= 16 vCPU + 24 GiB for default shape
  (asserted at the cluster module level).
- **NFR-014**: each new worker Ready in <5 min (asserted by the
  scale-workers runbook).

## How to invoke

```bash
cat .agents/skills/proxmox-k3s-pipeline/SKILL.md
# Or just type "bring up both clusters" to any agent that has the
# skill loaded (Claude Code, Cursor, etc.).
```

## Consumers tested

- Claude Code (latest stable, 2026-07).
- Cursor (latest stable, 2026-07).

Both consumers correctly parse the YAML frontmatter and load the
body. See `versions.lock.yaml` for the cross_check verdict.