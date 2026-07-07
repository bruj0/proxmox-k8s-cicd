---
name: proxmox-k3s-pipeline
description: Bring up two k3s clusters (cicd + apps) on a single Proxmox host using OpenTofu, the Talos Image Factory, and an Agent-driven bootstrap. Use when the user says "bring up both clusters", "deploy the pipeline", "run spec 001", "bootstrap a cluster", "scale workers", "decommission a cluster", "fix the cloudflare fallback", "rebuild the talos image", or "sync DNS to SDN". Outputs a fully bootstrapped cluster pair with public HTTPS via Cloudflare Tunnel (no host open ports) and apps->cicd cross-cluster Service consumption via ExternalName.
---

# Proxmox k3s Pipeline

End-to-end pipeline for provisioning two Talos/k3s clusters on a single
Proxmox host. The pipeline drives five numbered top-level phases; the
bootstrap phase (Phase 4) further decomposes into six ordered
sub-phases (talos, k3s, helm, kubeconfig, host_ports, externalname).
Each (sub-)phase has a single CLI entry point and explicit success
criteria that the agent MUST assert before proceeding.

**Pipeline state: 2026-07-07 -- Phases 0-2 verified end-to-end on
kvm.bruj0.net (BigBertha, PVE 9.2.3, Talos v1.13.5, k3s pending
bootstrap).** Phases 3-5 are documented from the original spec and
have NOT been re-run since the Phase-1 refactor (Talos v1.10 -> v1.13,
Packer -> Python/Image-Factory).

## When to load this skill

Load when the operator asks to bring up, scale, troubleshoot, rebuild,
decommission, or otherwise touch the k3s clusters provisioned by spec
001 on this host. **This is the live-host skill, not the cleanroom
spec** -- every step has been hit by at least one BigBertha-specific
gotcha that the operator must work around.

## Glossary (canonical vocabulary)

The bounded context for this skill is in
[CONTEXT.md](./CONTEXT.md). The five canonical terms are:

- **Agent Skill**: this document (the agentskills.io SKILL.md artifact
  loaded by Claude Code, Cursor, etc.).
- **Operator**: the human or AI agent that invokes the skill.
- **Pipeline**: the five-top-level-phase end-to-end sequence
  (build image -> provision cluster -> capture baseline -> bootstrap
  -> final verification).
- **Phase**: one numbered top-level stage of the pipeline. Phase 4
  (bootstrap) further decomposes into six sub-phases.
- **Runbook**: a single-concern copy-pasteable procedure under
  `docs/runbooks/`. Runbooks do not require an Agent; the operator
  follows them directly.

## Step 0a -- Pre-flight discovery (MANDATORY before Phase 0)

The skill assumes the live host `kvm.bruj0.net` running PVE
9.2.3 on kernel `7.0.6-2-pve`, with a single node named
`BigBertha` and three storage pools (`data1`, `data2` lvmthin;
`local` dir). For a different host, run the discovery probes below
and adjust accordingly. Each probe has a hard precondition; halt
and surface the failure if the precondition is not met.

### 0a.1 -- Reach the Proxmox API

```bash
PVE_URL="${PROXMOX_API_URL:-https://${PVE_HOST}:8006/api2/json}"
curl -kfsS --max-time 10 "$PVE_URL/version" | python3 -c \
  "import json,sys; d=json.load(sys.stdin); print(d['data']['version'], d['data']['release'])"
```

Precondition: HTTP 200 with parseable JSON. If the API is on a
non-default port (some operators front it with `:8086`), set
`PROXMOX_API_URL` accordingly. The `--pve-endpoint` flag and
`PROXMOX_API_URL` env var override the default in every tool.

### 0a.2 -- Probe the Proxmox node name

The cluster module's `pve_node` defaults to `proxmox-host`; on the
live host the actual node name is whatever `hostname` reports
(`BigBertha` here, but other operators have `pve` or `proxmox`).
Setting the wrong value fails at apply time with a `does not exist`
error per VM.

```bash
ssh -p "${PVE_SSH_PORT:-6022}" "root@${PVE_HOST}" 'hostname'
```

Set `pve_node = "<actual-name>"` in each cluster root's `main.tf`
(`infra/clusters/cicd/main.tf`, `infra/clusters/apps/main.tf`) before
applying. The module also accepts it via `terraform.tfvars`.

### 0a.3 -- Probe the SDN zone and host subnets

The host's `vnet0` (the SDN zone we attach VMs to) has subnet
`10.0.0.0/8` on this host with DHCP range `10.0.0.50-10.0.0.200`.
Two failure modes to detect up-front:

```bash
ssh -p "${PVE_SSH_PORT:-6022}" "root@${PVE_HOST}" \
  'ip -4 -o addr show | awk "{print \$2, \$4}"'
ssh -p "${PVE_SSH_PORT:-6022}" "root@${PVE_HOST}" \
  'cat /etc/pve/sdn/subnets.cfg; cat /etc/pve/sdn/vnets.cfg; cat /etc/pve/sdn/zones.cfg'
```

Precondition: the per-cluster `ip_start` (default `10.0.1.0/24` for
cicd, `10.0.2.0/24` for apps) MUST NOT overlap any host interface IP.
Concrete example: if the host's `vnet0` is on `10.0.0.1/8` (BigBertha's
case), do NOT set `ip_start = "10.0.0.0/24"` -- the first `cidrhost`
result `10.0.0.0` would alias the SDN's network address and confuse
IPAM. The default `10.0.1.0/24` / `10.0.2.0/24` are safe.
**Important: these are NOT the IPs the SDN actually assigns to
VMs.** PVE's IPAM allocates from the SDN DHCP pool (10.0.0.50-200);
the module's `cidrhost(var.ip_start, i)` only feeds the
**PowerDNS records** that the module writes. Real VM IPs come from
the SDN DHCP range and are recovered via the qemu-guest-agent after
boot. See `Step 2.3` for the post-apply PowerDNS sync.

### 0a.4 -- Probe Cloudflare account and zone

WP00 mints a scoped Cloudflare API token that requires the operator's
Cloudflare **zone ID** (not just account ID). The token-creation
endpoint (`POST /user/tokens`) requires user-level authentication --
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

### 0a.5 -- Verify the Proxmox token has `Sys.Modify`

PVE token privileges are bound to the *user*, not root. PAM tokens
(including ones generated via `pveum user token add`) only inherit
the user's ACL -- not the implicit `root@pam` `Administrator` role.
The tokens module needs `Sys.Modify` to create roles. If your
bootstrap token lacks it, the apply fails with HTTP 403 on
`/access/roles`.

```bash
ssh -p "${PVE_SSH_PORT:-6022}" "root@${PVE_HOST}" \
  'pvesh get /access/roles/PVEAdmin --output-format yaml | head'
```

PVEAdmin grants `Sys.Audit`, `Sys.Console`, `Sys.Syslog` -- but NOT
`Sys.Modify`. Only the `Administrator` role (root@pam's implicit
role) has `Sys.Modify`. For WP00, either:

- (A) Use a `root@pam!tf-bootstrap` token (full Administrator role)
- (B) Grant `Sys.Modify` on `/` to the bootstrap user via the
      explicit form, which still requires the `Administrator` role.

The pragmatic answer: use a `root@pam!tf-bootstrap` token, scoped to
this apply, then delete it after WP00 lands.

### 0a.6 -- Confirm required `.env` keys

Before running `scripts/apply_tofu.py`, ensure `.env` contains ALL
of:

| Key | Purpose | Required for |
|---|---|---|
| `CLOUDFLARE_TOKEN_CREATOR` | cfat_* scoped admin token (used for permission-group enumeration if it has `Account:API Tokens:Read`) | WP00 |
| `CLOUDFLARE_GLOBAL_API_KEY` | Account-level Global API Key (used for `POST /user/tokens` which requires user-level auth) | WP00 |
| `CLOUDFLARE_GLOBAL_API_EMAIL` | Email tied to the Global API Key | WP00 |
| `CLOUDFLARE_ACCOUNT_ID` | Account under which to mint the scoped token | WP00 |
| `CLOUDFLARE_ZONE_ID` | Zone to scope DNS-edit permissions on the child token | WP00 |
| `CLOUDFLARE_DOMAIN` | Human-readable domain (informational only) | WP00 |
| `PROXMOX_API_URL` | Proxmox API endpoint, e.g. `https://kvm.bruj0.net:8006/api2/json` | WP00+ |
| `PROXMOX_API_TOKEN` | `USER@REALM!TOK=secret` form | WP00+ |
| `GITLAB_PAT` | GitLab personal access token with `api` scope (project Owner on infra-state/bigbertha) | WP00+ |
| `POWERDNS_API_KEY` | PowerDNS API key (used by `scripts/sync_dns_to_sdn.py`) | Phase 2 sync |

If `CLOUDFLARE_GLOBAL_API_KEY` is missing, the tokens module falls
back to `CLOUDFLARE_TOKEN_CREATOR` only -- which will fail at
`POST /user/tokens` with `403 Forbidden (Valid user-level
authentication not found)`. Plan accordingly.

### 0a.7 -- Stale terminal env-var trap

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
+ `TF_VAR_proxmox_api_token_secret` (the `apply_tofu.py` script does
this automatically; only do it manually if running `tofu` outside
the helper).

Note: bash history-expansion mangles `!` in unquoted strings. Always
single-quote the token id: `'k3s-terraform@pam!tf'`. See Step 0c
for the apply-time equivalent.

### 0a.8 -- Avoid the imported-token-no-secret trap

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

### 0a.9 -- Use the Bitwarden SSH agent for PVE access

The operator's `~/.ssh/agent/` is a **Bitwarden SSH agent socket**,
not the standard OpenSSH agent. SSH commands that target PVE
**must** export `SSH_AUTH_SOCK=/home/bruj0/.bitwarden-ssh-agent.sock`
or the key will be rejected with `Permission denied (publickey)`.
The agent holds a per-host key fingerprint
(`SHA256:YKoadsaoGPiscSmBy15Nc+Bl+YCThvTFefe8pHKlygo` for
`kvm.bruj0.net`).

```bash
SSH_AUTH_SOCK=/home/bruj0/.bitwarden-ssh-agent.sock \
  ssh -o BatchMode=yes -p 6022 root@kvm.bruj0.net 'echo ok'
```

If `SSH_AUTH_SOCK` is unset, OpenSSH falls back to the wrong agent
and the Github key (fingerprint `...NM9k`) is offered instead,
which PVE doesn't have authorized for `root`. The fix is always
`SSH_AUTH_SOCK=/home/bruj0/.bitwarden-ssh-agent.sock`.

## Step 0 -- Load the context7-auto-research gate (MANDATORY)

Before invoking any external library, load
`.agents/skills/context7-auto-research/SKILL.md` and run
`context7-auto-research` for each library the pipeline touches.
**Do NOT rely on training data for library APIs.** The pipeline
uses the following pinned versions; record the rationale for each
in the operator's reply before invoking the library:

| Library | Version | Rationale (context7) |
|---|---|---|
| `bpg/proxmox` (OpenTofu provider) | `0.111.1` | rationale: latest stable that exposes `proxmox_cloned_vm`; v0.111.1 introduces the `host` attribute that the WP02 module uses |
| `pan-net/powerdns` (OpenTofu provider) | `1.5.0` | rationale: pan-net 1.5.x is the first version that supports the `rrsets` PATCH endpoint with `changetype: REPLACE`, which we use for idempotent record updates |
| `STRRL/cloudflare-tunnel-ingress-controller` (Helm chart) | `0.0.23` | rationale: only stable version on the strrl chart repo as of 2026-07; pinned because the upstream CRDs are still alpha |
| `cilium` (Helm chart) | `1.16.x` | rationale: matches the Talos 1.13.x kernel constraint and supports `gatewayAPI.enabled` plus eBPF host routing |
| `sergelogvinov/proxmox-cloud-controller-manager` (Helm chart) | `0.14.0` | rationale: latest stable; required for `topology.kubernetes.io/region` + `zone` labels on the apps cluster nodes |
| `sergelogvinov/proxmox-csi-plugin` (Helm chart) | `0.5.9` | rationale: chart 0.5.9 supports PVE 9.x and lvm-thin on `data1/data2` |
| `talosctl` | `1.13.x` | rationale: matches the Talos image baked by SS1 (v1.13.5); required for `talosctl apply-config --install-image` and `talosctl kubeconfig` |
| `k3s` | `1.34.x` | rationale: matches the Cilium + kube-vip versions; no known CVEs |
| `helm` | `3.x` | rationale: required for `helm upgrade --install`; matches what k3s 1.34 ships |

Document each library's rationale in the operator's reply **before**
calling the library.

## Step 0b -- WP00 apply-time gotchas (live-host lessons)

These are deployment-environment issues that surfaced only when
applying WP00 against a real PVE 9.2.3 + Cloudflare account. Read
this before `scripts/apply_tofu.py tokens`.

### 0b.1 -- Cloudflare provider auth (ExactlyOneOf)

The Cloudflare provider v5 schema enforces
`ExactlyOneOf(api_key, api_token)`. Passing both is a schema
violation; passing neither fails with "Valid user-level
authentication not found". The tokens provider block must pick
exactly one based on what's available:

- `CLOUDFLARE_GLOBAL_API_KEY` set -> use `api_key + email`
  (Global API Key has full account scope, can mint child tokens)
- `CLOUDFLARE_GLOBAL_API_KEY` NOT set -> use `api_token` (cfat_*)
  (lacks child-token mint; abort and tell the operator)

Don't try to satisfy both fields. Pick one auth method.

### 0b.2 -- Cloudflare resource key format

Cloudflare's API token `resources` field is a JSON-encoded object
whose keys are scope expressions:

- Zone-scoped (DNS, Zone settings): `com.cloudflare.api.account.zone.<zone_id>`
- Account-scoped (Tunnel, R2): `com.cloudflare.api.account.<account_id>`

NOT `account.id` (this is a key in the API **response**, not the
resource key in the policy). NOT `account.*` (literal glob is
rejected). The provider will forward whatever you set; Cloudflare
will reject malformed keys with `"X is not a valid match-all
object expression"` or `"X is not a valid resource name"`.

### 0b.3 -- Cloudflare permission-group ID format

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
  are stable per Cloudflare's registry -- verified 2026-07-06.

The three UUIDs you can hardcode as fallback (Cloudflare registry,
2024+):

| Label | UUID |
|---|---|
| Zone Read | `c8fed203ed3043cba015a93ad1616f1f` |
| DNS Write | `4755a26eedb94da69e1066d98aa820be` |
| Cloudflare Tunnel Write | `c07321b023e944ff818fec44d8203567` |

If Cloudflare ever rotates a UUID, re-fetch the list with the
global key and update.

### 0b.4 -- IP-lock condition format

Cloudflare rejects `0.0.0.0/0` in `condition.request_ip.in` with
`"invalid CIDR"`. Two safe patterns:

- (A) If `cloudflare_runner_cidr` is unset AND the apply-runner IP
  lookup fails, OMIT the `condition` block entirely (the token is
  unrestricted by IP -- acceptable for minimal-permission tokens).
- (B) If `cloudflare_runner_cidr` is set (e.g. CI runner), use it
  as-is.

### 0b.5 -- Proxmox role-creation privilege

`PVEAdmin` does NOT include `Sys.Modify`. Only `Administrator`
(root@pam's implicit role) has it. WP00 needs `Sys.Modify` to
create the `k3s-cluster` role. Two paths:

- (A) Bootstrap with a `root@pam!tf-bootstrap` token (recommended;
  one-shot, delete after WP00 lands).
- (B) Pre-create the role manually with `pvesh`/`pveum` and
  `tofu import` it -- but see Step 0a.8 for the imported-no-secret
  trap.

### 0b.6 -- OpenTofu `-chdir=` vs Terraform `-C`

OpenTofu uses `-chdir=DIR` for the subcommand-level directory
switch. Terraform `-C` is **not** supported. Makefile recipes
that loop over modules must use `tofu -chdir=$$d ...`, not
`tofu -C $$d ...`.

### 0b.7 -- Bootstrap user TTL

The `terraform-bootstrap@pam` user (or any user created solely to
mint WP00 tokens) should be deleted after WP00 lands. The
bootstrap token (`root@pam!tf-bootstrap`) should also be deleted
to leave only the scoped child tokens (`k3s-terraform@pam!tf`)
in production.

```bash
SSH_AUTH_SOCK=/home/bruj0/.bitwarden-ssh-agent.sock \
  ssh -p 6022 root@kvm.bruj0.net 'pveum user token delete root@pam tf-bootstrap'
# root@pam itself stays -- you need it for any future admin operations
```

## Step 0c -- Library-version pins for environment tooling

In addition to the pipeline libraries (Step 0 table), the
**deployment environment** requires these pinned tools. Mismatched
versions cause silent API differences.

| Tool | Required | Notes |
|---|---|---|
| `tofu` (OpenTofu) | `>= 1.6.0` | `-chdir=` syntax requires 1.6+ |
| `talosctl` | `1.13.x` | Matches the Talos image baked by SS1 (v1.13.5) |
| `helm` | `3.x` | Matches what k3s 1.34 ships |
| `kubectl` | `>= 1.30` | For bootstrap phase verification |
| `packer` | NOT USED | The pipeline does NOT use Packer -- see `Step 1` for the canonical Sidero factory flow |

## Step 0d -- Phase 0: Token provisioning (SS0 / WP00)

WP00 runs **once** before any cluster provisioning. It mints a
scoped Proxmox API token and a scoped Cloudflare API token, then
writes both to `infra/tokens/output.json` (mode `0600`). All
downstream phases read from this file.

**Pre-flight**: complete Step 0a.1 through 0a.9 BEFORE running.
The most common failure is missing `CLOUDFLARE_GLOBAL_API_KEY`
(Step 0a.6).

**Apply**

```bash
SSH_AUTH_SOCK=/home/bruj0/.bitwarden-ssh-agent.sock \
  python scripts/apply_tofu.py tokens
```

The wrapper reads `.env`, translates `PROXMOX_API_TOKEN` into
`TF_VAR_proxmox_api_token_id` + `TF_VAR_proxmox_api_token_secret`,
opens the PowerDNS SSH tunnel (no-op for tokens target), and runs
`tofu init -backend=false && tofu apply -auto-approve`.

If you see `403 Forbidden (Valid user-level authentication not
found)`, the Cloudflare provider is using the cfat_* token instead
of the global key. See Step 0b.1.

Success criteria (assert ALL before proceeding):
1. `cat infra/tokens/output.json` exits 0; file mode is `0600`.
2. `jq '.proxmox_token_secret, .cloudflare_scoped_token' infra/tokens/output.json`
   returns non-null for both keys.
3. `tofu test` in `infra/tokens/` exits 0 (6/6).
4. `SSH_AUTH_SOCK=... ssh root@$PVE_HOST 'pvesh get /access/users/k3s-terraform@pam'`
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
SSH_AUTH_SOCK=/home/bruj0/.bitwarden-ssh-agent.sock \
  ssh -p 6022 root@kvm.bruj0.net 'pveum user token delete root@pam tf-bootstrap'
# root@pam itself stays -- needed for future admin operations
```

## Step 0e -- State backend (GitLab HTTP, 2026-07-06)

All four tofu stacks (`infra/tokens`, `infra/modules/proxmox-k3s-cluster`,
`infra/clusters/cicd`, `infra/clusters/apps`) now store state in a
GitLab-managed Terraform state backend instead of local files.

### 0e.1 -- Why GitLab HTTP and not local?

Local `terraform.tfstate` was acceptable during the one-operator
dev loop, but for the multi-developer / CI-driven phase we need:
- A single source of truth for the live cluster topology.
- State locking that doesn't depend on the operator's laptop disk.
- A versioned history (`serial` + `lineage`) visible in the
  GitLab UI alongside the source code.

### 0e.2 -- Project layout

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

### 0e.3 -- Required env: `GITLAB_PAT`

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

### 0e.4 -- Init / migrate via `scripts/gitlab_backend.sh`

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

### 0e.5 -- Force-unlock recipe

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

### 0e.6 -- Gotcha: `backend` block in modules is ignored with warning

Per OpenTofu docs, a `terraform { backend "http" {} }` block in
a module is silently ignored with a warning ("Any selected backend
applies to the entire configuration, so OpenTofu expects provider
configurations only in the root module"). State for instances of
the module lives in the calling root. We therefore omit the
`backend "http" {}` block from
`infra/modules/proxmox-k3s-cluster/versions.tf` and document the
design intent in that file's comments.

### 0e.7 -- Path-drift expected after migration

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

### 0e.8 -- In-place refresh-only updates after migration

`tofu plan` against the cicd and apps roots after migration shows
`Plan: 0 to add, 2 to change, 0 to destroy` (cicd) and
`0 to add, 2 to change, 0 to destroy` (apps). The "changes" are
in-place refreshes of `terraform_data.image_id_present` and
`terraform_data.vmid_overlap` (re-evaluate preconditions). No
resources are created or destroyed. Apply these refresh-only
updates once to align state with the live host's current state.

## Step 1 -- Phase 1: Build the VM image (SS1)

Goal: bake a Talos Linux golden image into a Proxmox template
(VMID 900). One-shot; idempotent on rerun (image-id.txt already
exists -> no-op).

**Important: the pipeline does NOT use Packer.** The 2026-07-07
build flow uses the canonical Sidero Image Factory + qm importdisk
+ talosctl apply-config --install-image path. Any Packer recipes
left in the tree (none after the 2026-07-07 refactor) are
obsolete and MUST NOT be re-enabled.

### 1.1 -- Sidero Image Factory schematic

We use a fixed, operator-curated schematic ID rather than letting
the agent compose one at runtime. This pins the exact set of
system extensions baked into the rootfs:

```
ab5430f4aef7985d19988502c97f5a15d309963d664456d8ba5394156dbe524a
```

Extensions: `siderolabs/cloudflared`, `siderolabs/ctr`,
`siderolabs/fuse3`, `siderolabs/glibc`, `siderolabs/iscsi-tools`,
`siderolabs/qemu-guest-agent`, `siderolabs/util-linux-tools`.

Image URLs (auto-resolved by `build_image.py`):

- ISO: `https://factory.talos.dev/image/<schematic>/<version>/metal-amd64.iso`
- Installer image: `factory.talos.dev/installer/<schematic>:<version>`

### 1.2 -- What `build_image.py` does (SS1 orchestrator)

The orchestrator at `tools/build_image/__init__.py` is a single
Python entry point. It drives the full flow end-to-end:

1. Download the Talos ISO from the Image Factory if not already
   present in `/var/lib/vz/template/iso/talos-<ver>-custom.iso`.
2. Create a non-template VM 900 (`qm create`) with:
   - `q35` machine, OVMF BIOS, `efitype=4m` (no `pre-enrolled-keys=1`
     -- see `Step 1.5.1` for why)
   - `boot: order=ide2` (boot the ISO first)
   - `agent: enabled=1` (qemu-guest-agent channel)
   - `serial0: socket`, `vga: serial0` (Talos console + serial log
     capture)
3. Attach the Talos ISO to `ide2`, start the VM, and wait for the
   Talos `apid` (port 50000) to come up on the SDN-allocated IP.
4. Generate a minimal controlplane machineconfig via
   `talosctl gen config` (with `--force` so re-runs work), push it
   to PVE over SSH stdin (`cat > /tmp/controlplane.yaml`), and
   `talosctl apply-config --insecure --install-image <factory-url>`.
   The `--install-image` flag is the load-bearing piece: it tells
   Talos to install itself to `/dev/sda` (scsi0) using the
   factory URL on the next reboot, baking the schematic
   extensions into the rootfs.
5. `qm shutdown 900` with a 10s grace, then `wait_for_vm_stopped`
   with a 30s timeout. If the graceful shutdown does not finish
   (Talos installer mode can ignore ACPI), fall back to
   `qm stop` (hard kill) with another 10s wait. The
   `Popen.communicate(timeout=...)` refactor in
   `tools/lib/pve_client.py` captures partial stderr on the
   timeout so the operator can see *why* PVE hung (lock contention,
   etc.).
6. **`qm set 900 --boot order=scsi0`** (this is the load-bearing
   step that was missing in the v1.10.0 build -- see `Step 1.5.2`).
7. `qm template 900` -- convert to a PVE template.
8. Write `build/image-id.txt` containing `900\n`. The
   `terraform_data.image_id_present` precondition in
   `infra/clusters/{cicd,apps}/main.tf` reads this file.

### 1.3 -- Apply

```bash
SSH_AUTH_SOCK=/home/bruj0/.bitwarden-ssh-agent.sock \
PVE_TOKEN_ID='k3s-terraform@pam!tf' \
PVE_TOKEN_SECRET='<secret-uuid>' \
python -m tools.build_image \
  --talos-version v1.13.5 \
  --pve-endpoint https://kvm.bruj0.net:8006/api2/json \
  --pve-node BigBertha \
  --pve-ssh-host kvm.bruj0.net --pve-ssh-port 6022 \
  --log-dir ./logs
```

Equivalent shortcut once `.env` is set up:

```bash
make build-image TALOS_VERSION=v1.13.5
```

Success criteria (assert ALL before proceeding):
1. `qm list | grep -w 900` returns a single row with `template`
   column equal to `1`, `boot: order=scsi0`, NO `ide2` line,
   `scsi0: data1:base-900-disk-1,...,size=32G`,
   `efidisk0: data1:base-900-disk-0,efitype=4m` (NO
   `pre-enrolled-keys=1`).
2. `cat build/image-id.txt` returns exactly `900` followed by
   newline.
3. `qm start 900 && sleep 5 && qm guest cmd 900 ping` returns
   success within 30s of boot. This proves the qemu-guest-agent
   from the schematic is alive and Talos is installed to disk.
4. `qm guest cmd 900 network-get-interfaces` reports `ens18`
   with a `10.0.0.x` IP -- the SDN DHCP allocation worked.

Failure handling: halt the pipeline and surface the structured
error (`error`, `resolution` keys) to the operator. Do NOT proceed
to Phase 2.

### 1.4 -- Logs

The build writes two streams under `--log-dir` (default `./logs/`):

- `build-image_<UTC>.log` -- JSONL audit log, one event per line.
- `vm-900-serial-<UTC>.log` -- serial console capture, streamed
  via `ssh ... timeout N cat /var/run/qemu-server/900.serial0`.

A `latest-build-image.log` symlink points at the most recent
audit log. A failed build leaves the VM 900 in a known state --
either stopped (good, retry-safe) or running (agent can be
re-applied with a fresh config; see `Step 1.5.2` recovery).

### 1.5 -- Phase 1 apply-time gotchas (live host 2026-07-07)

These are the deployment-environment issues that surfaced ONLY
when running the SS1 build against PVE 9.2.3 with Talos v1.13.5.
Each was a hard blocker; all are now resolved and pinned below.

#### 1.5.1 -- `pre-enrolled-keys=1` blocks Talos v1.13.5 Secure Boot

Talos v1.13.5 ships `systemd-boot 259.5` whose UKI signature
is NOT in the OVMF `OVMF_CODE_4M.secboot.fd` pre-enrolled DB.
PVE 9.2.3's default `efidisk0 data1:1,efitype=4m,pre-enrolled-keys=1`
turns on Secure Boot, which then rejects the Talos UKI with
`Access Denied` and the VM hangs in the OVMF Boot Manager.

**Fix**: drop `pre-enrolled-keys=1` from the
`create_template_shell` call. The resulting
`efidisk0 data1:1,efitype=4m` is non-Secure-Boot but Talos boots
fine. Verified 2026-07-07 against PVE 9.2.3 + Talos v1.13.5.

#### 1.5.2 -- Template boot order must be `scsi0`, not `ide2`

`create_template_shell` sets `boot: order=ide2` so the build
itself can boot the ISO. After Talos is installed to `scsi0`,
the template MUST be flipped to `boot: order=scsi0` BEFORE
`qm template 900` -- otherwise every clone boots the ISO
indefinitely and Talos never starts from the installed disk.
The build_image step at index 6 (`Step 1.2`) handles this.

**Recovery if you forgot**: the template and existing clones can
be patched in place without a full rebuild:

```bash
SSH_AUTH_SOCK=/home/bruj0/.bitwarden-ssh-agent.sock \
  ssh -p 6022 root@kvm.bruj0.net 'for vmid in 900 111 112 113 114; do
    qm set $vmid --boot order=scsi0; done'
# then reboot each clone so the new boot order takes effect
```

#### 1.5.3 -- `qm shutdown` does not work in Talos installer mode

Talos running in installer mode (booted from ISO) does not
respond to ACPI shutdown. The `qm shutdown 900` call returns
immediately but the VM keeps running until the call times out.
The build flow uses `stop_vm` (graceful) -> `wait_for_vm_stopped`
(30s) -> `stop_vm_forcible` (hard `qm stop`, 10s) as a fail-safe.
The `Popen.communicate(timeout=...)` refactor in
`tools/lib/pve_client.py` captures partial stderr on timeout,
so the operator can see whether PVE hung on a lock or on
`qemu-img` cleanup.

#### 1.5.4 -- `--force` on `talosctl gen config`

`talosctl gen config` refuses to overwrite existing
`talos/controlplane.yaml` and `talos/worker.yaml` files. The
build uses `talosctl gen config ... --force` so re-runs (and
the `image-id.txt` shortcut path) work idempotently.

#### 1.5.5 -- Pushing the machineconfig over SSH stdin

`build_image.py` writes the rendered controlplane YAML to
PVE via `ssh ... cat > /tmp/controlplane.yaml` (no scp). The
helper method `ssh_run_stdin` was added to
`tools/lib/pve_client.py` to feed stdin directly to a remote
command. `--install-image <factory-url>` is passed to
`talosctl apply-config` so Talos downloads and installs the
schematic-baked image to `scsi0` on the next reboot.

#### 1.5.6 -- OVMF `loader/entries/*.conf` is missing in v1.13 ISO

Talos v1.13.5 ISO's EFI partition contains
`EFI/BOOT/BOOTX64.EFI` (systemd-boot) and
`EFI/Linux/Talos-v1.13.5.efi` (UKI), but NO
`loader/entries/*.conf`. The UKI is the boot entry, picked
up automatically. Do NOT add a `loader/entries/` directory.

## Step 2 -- Phase 2: Provision the clusters (SS2)

Apply OpenTofu against `infra/clusters/cicd/` and
`infra/clusters/apps/` to create the Talos VMs and render the
manifests.

### 2.1 -- Apply

```bash
SSH_AUTH_SOCK=/home/bruj0/.bitwarden-ssh-agent.sock \
  python scripts/apply_tofu.py cicd
SSH_AUTH_SOCK=/home/bruj0/.bitwarden-ssh-agent.sock \
  python scripts/apply_tofu.py apps
```

The wrapper reads `.env`, sets `TF_VAR_proxmox_*`, opens the
PowerDNS SSH tunnel (LXC 101, `10.0.0.3:8081` tunneled to
`127.0.0.1:8081`), and runs `tofu init -backend=false && tofu
plan && tofu apply -auto-approve`.

Success criteria (assert ALL before proceeding):
1. Both `tofu apply` calls exit 0. `qm list` shows 4 new VMs
   (111, 112, 113, 114) with `template=0`, 32GB scsi0, RAM
   4096 (cp) / 8192 (w), running, `boot: order=scsi0`.
2. `infra/clusters/cicd/output.json` and
   `infra/clusters/apps/output.json` both exist and parse as
   JSON with `cluster_name`, `vip`, `pod_cidr`, `svc_cidr`,
   and `nodes[]` keys.
3. `infra/clusters/cicd/manifests/traefik-helmchartconfig.yaml`
   exists and parses as YAML.
4. `qm agent <vmid> ping` returns success for all 4 VMs within
   30s of boot. This proves the qemu-guest-agent baked into
   the schematic is alive.
5. `qm agent <vmid> network-get-interfaces` reports `ens18`
   with a `10.0.0.x` IP per VM.

### 2.2 -- Phase 2 apply-time gotchas (live host 2026-07-07)

#### 2.2.1 -- `output.json` must carry the spec-T007 cluster-root keys

The cluster root (`infra/clusters/cicd/main.tf`) reads
`cf_api_token` and `cf_account_id` from
`infra/tokens/output.json` (per `specs/.../tasks.md:106`). The
post-refactor `infra/tokens/output_json.tf` emits
`cloudflare_scoped_token` / `cloudflare_account_id` AND the
canonical `cf_*` aliases so `jsondecode(...)` returns
non-null. Verified 2026-07-06.

#### 2.2.2 -- `bpg/proxmox` proxmox_cloned_vm needs explicit `target_datastore`

`disk.scsi0.datastore_id = "local-lvm"` is hardcoded in the
upstream module template. BigBertha has no `local-lvm` lvmthin
pool, and the bpg/proxmox v0.111.x provider stored `local-lvm`
in plan but the cloned VM ended up on `data1` (cloned-from
storage), causing `Provider produced inconsistent result after
apply`. Two-fold fix:
- module `main.tf`: pin the clone `target_datastore` to a new
  `var.disk_storage_pool` (default `data1`).
- cluster root: pass `disk_storage_pool = "data1"`.

#### 2.2.3 -- `k3s-cluster` role needs 7 extra privs for VM lifecycle

The spec T005 12-priv set is **insufficient** for Phase 2. Each
of these returns `403 Permission check failed` on BigBertha
without the priv:

| Priv | Needed for |
|---|---|
| `Sys.Audit` | `/access` namespace reads |
| `VM.Audit` | Read VM cfg / qemu list |
| `VM.Clone` | Clone VMID 900 to 111-114 |
| `VM.Migrate` | Cleanup moved/half-baked templates |
| `VM.Config.CDROM` | Attach/detach Talos ISO during build |
| `VM.Config.HWType` | Set `machine=q35` (UEFI boot) |
| `VM.Snapshot.Rollback` | Restore after template-bake failure |

Total: **19 privs** (12 spec + 7 above). Verify with
`ssh root@$PVE_HOST 'pvesh get /access/roles/k3s-cluster'`.

#### 2.2.4 -- `output.json` `proxmox_token_secret` is a BARE secret

bpg/proxmox v0.111.x's `proxmox_user_token.<...>.value`
attribute is the **FULL api-token string** in
`USER@REALM!TOKENID=secret` form -- not just the secret. The
tokens module splits `value` on `=` and writes only the
second part to `output.json.proxmox_token_secret`. Verified
2026-07-06 -- the secret is a 36-char UUID.

#### 2.2.5 -- `output.json` MUST expose `pod_cidr` and `svc_cidr`

The cluster root's spec says `output.json` keys include
`pod_cidr` and `svc_cidr` (consumed by
`tools/lib/talos_client.py` to wire `--skip-rbac=false
--network-cidr=` for the per-node Talos configs). The module
writes them via `jsonencode` in
`infra/modules/proxmox-k3s-cluster/outputs.tf`. Verified
2026-07-06.

#### 2.2.6 -- `manifests/` subdirectory required by `tools/lib/helm_client.py`

`tools/lib/helm_client.py:208` reads
`infra/clusters/<name>/manifests/traefik-helmchartconfig.yaml`,
not the cluster-root file. The module writes under
`${path.module}/../../clusters/<name>/manifests/traefik-...`.
The operator must pre-create `infra/clusters/<name>/manifests/`
or tofu errors with "parent directory does not exist".

#### 2.2.7 -- SDN assigns IPs from DHCP, NOT from `var.ip_start`

This is the load-bearing gotcha of Phase 2. The module's
`var.ip_start` (`10.0.1.0/24` for cicd, `10.0.2.0/24` for
apps) is fed to `cidrhost()` to produce the IPs that
**PowerDNS records** should point at. But PVE's SDN IPAM
auto-allocates from the DHCP pool (`10.0.0.50-200`) and
gives the VMs addresses like `10.0.0.61-64`. The module's
records are therefore WRONG on first apply:

| Host | Record (wrong) | Actual VM IP |
|---|---|---|
| `cicd-cp-1.intranet.local` | `10.0.1.0` | `10.0.0.61` |
| `cicd-w-1.intranet.local` | `10.0.1.1` | `10.0.0.62` |
| `apps-cp-1.intranet.local` | `10.0.0.63` | `10.0.0.63` (lucky) |
| `apps-w-1.intranet.local` | `10.0.2.1` | `10.0.0.64` |

**Fix**: after every successful `apply_tofu.py cicd` and
`apply_tofu.py apps`, run `scripts/sync_dns_to_sdn.py` to
read the actual VM IPs via the qemu-guest-agent and
PATCH the PowerDNS records. Then manually delete the
stale PTRs left over from the wrong-IP records (the script
will create new PTRs at the correct names; the stale ones
have to be cleaned by hand because PowerDNS does not
auto-deduplicate by FQDN-vs-relative-name).

## Step 2.3 -- Sync DNS to SDN

The DNS sync is a small operational tool, not a phase. Run it
immediately after every successful `apply_tofu.py cicd|apps` and
after every scale-up / scale-down runbook. The tool:

1. Opens the SSH tunnel to PowerDNS LXC 101 (`10.0.0.3:8081` ->
   `127.0.0.1:18081`).
2. For each `--vmid/--name` pair, queries
   `qm agent <vmid> network-get-interfaces` and picks the first
   non-loopback IPv4.
3. PATCHes the matching A record in `intranet.local.` and the
   matching PTR in `10.in-addr.arpa.` with `changetype: REPLACE`
   (idempotent).

```bash
SSH_AUTH_SOCK=/home/bruj0/.bitwarden-ssh-agent.sock \
  POWERDNS_API_KEY=$(awk -F= '$1=="POWERDNS_API_KEY"{print $2; exit}' .env) \
  python scripts/sync_dns_to_sdn.py \
    --vmid 111 --name cicd-cp-1 \
    --vmid 112 --name cicd-w-1 \
    --vmid 113 --name apps-cp-1 \
    --vmid 114 --name apps-w-1
```

After the script finishes, manually delete any stale PTRs left
over from prior wrong-IP applies (PowerDNS allows both a full
FQDN and a zone-relative name for the same rdata, so the wrong
ones will linger):

```bash
SSH_AUTH_SOCK=/home/bruj0/.bitwarden-ssh-agent.sock \
  ssh -p 6022 root@kvm.bruj0.net \
    'pct exec 101 -- bash -c "for n in 0.1.0.10.10.in-addr.arpa. \
    1.1.0.10.10.in-addr.arpa. 0.2.0.10.10.in-addr.arpa. \
    1.2.0.10.10.in-addr.arpa. 61.0.0.10.in-addr.arpa. \
    62.0.0.10.in-addr.arpa. 63.0.0.10.in-addr.arpa. \
    64.0.0.10.in-addr.arpa. 30.0.0.10.in-addr.arpa. \
    40.0.0.10.in-addr.arpa.; do
      curl -s -X PATCH -H \"X-API-Key: \$API_KEY\" \
        -H \"Content-Type: application/json\" \
        http://127.0.0.1:8081/api/v1/servers/localhost/zones/10.in-addr.arpa. \
        -d \"{\\\"rrsets\\\":[{\\\"name\\\":\\\"\$n\\\",\\\"type\\\":\\\"PTR\\\",\\\"changetype\\\":\\\"DELETE\\\"}]}\"; \
    done"'
```

Verify with `dig @10.0.0.3 <name>.intranet.local A` (forward)
and `dig @10.0.0.3 <reversed>.10.in-addr.arpa PTR` (reverse).

**Why not fix the module?** Proxmox SDN IPAM does not let you
bind a specific /24 host to a VM without writing a
`pvesh set /sdn/.../dhcp/hosts/<mac>` reservation per VM, and
even then the IPAM still walks the configured range -- you
can't make it start at a non-zero host. Adjusting the records
post-hoc is the smaller, reversible change.

## Step 3 -- Phase 3: Capture host-ports baseline (M2 setup)

This is a one-shot baseline capture. Run BEFORE the first cluster
bootstrap, then never again unless the operator is decommissioning and
recreating the cluster from scratch.

```bash
SSH_AUTH_SOCK=/home/bruj0/.bitwarden-ssh-agent.sock \
  PVE_SSH_PORT=6022 \
  ./scripts/capture_host_ports_baseline.sh infra/clusters/cicd
```

Success criteria: `infra/clusters/cicd/host_ports_baseline.txt` exists
and contains the literal substring `chain prerouting`.

## Step 4 -- Phase 4: Bootstrap (SS3)

Runs the six-phase bootstrap. Order is enforced by `PHASES` in
`tools/bootstrap_cluster.py`. Each phase records its success in
`infra/clusters/<name>/bootstrap_state.json`; rerunning is a no-op for
completed phases.

**Prerequisite: Phase 2 must be complete AND `sync_dns_to_sdn.py` must
have run successfully.** Bootstrap reads the cluster VIP / node FQDNs
from PowerDNS; if the records are wrong, `talosctl apply-config`
will target the wrong IPs.

For the cicd cluster:

```bash
python tools/bootstrap_cluster.py --cluster cicd
```

For the apps cluster (after cicd is healthy AND `infra/clusters/apps/`
has been provisioned by Phase 2):

```bash
python tools/bootstrap_cluster.py --cluster apps
```

The six phases, in order:

1. `talos` -- `talosctl apply-config` on every node, wait for
   healthy, bootstrap k3s.
2. `k3s` -- verify `/healthz` returns `ok`.
3. `helm` -- install Cilium + kube-vip (WP04) and the remaining four
   releases (proxmox-ccm, proxmox-csi, cloudflare-tunnel,
   cert-manager, WP05) + apply the rendered Traefik HelmChartConfig.
4. `kubeconfig` -- pull admin kubeconfig, merge into
   `~/.kube/config`.
5. `host_ports` -- assert no new DNAT rules have been added to the
   PVE nft prerouting chain (M2 misfit verifier).
6. `externalname` -- apps-cluster only: apply the cross-cluster
   ExternalName Services kustomization (WP06).

Idempotency: on a rerun, the script reads
`infra/clusters/<name>/bootstrap_state.json` and skips phases whose
name appears in `phases_done`. This is the canonical "convergence from
partial state" path required by NFR-011. **Idempotency is the
contract; the operator may safely rerun the bootstrap at any point.**

Success criteria (assert ALL before proceeding):
1. `kubectl --context cicd get nodes` shows all control-plane +
   worker nodes in `Ready` state.
2. `kubectl --context cicd -n kube-system get pods --all-namespaces`
   shows Cilium + kube-vip + proxmox-ccm + proxmox-csi +
   cloudflare-tunnel + cert-manager pods `Running`.
3. `python tools/bootstrap_cluster.py --cluster cicd --phases all`
   exits 0 in <60 seconds (idempotent rerun).

## Step 5 -- Phase 5: Final verification (SC-001..SC-006)

Run the verification matrix in `docs/verification.md`:

- **SC-001**: clean-room end-to-end bring-up completes in <=60 min.
- **SC-002**: PVC + Deployment succeeds on both clusters.
- **SC-003**: Ingress of class `cloudflare-tunnel` resolves via
  Cloudflare within 60 s.
- **SC-004**: `nft list chain ip nat prerouting` shows zero new DNAT
  rules.
- **SC-005**: rerun idempotency -- tofu apply + bootstrap_cluster.py
  on a fully-bootstrapped cluster converges to no-op in <60 s.
- **SC-006**: `tofu destroy` cleanly removes all VMs.

NFRs verified at this phase:
- **NFR-010**: this SKILL.md has YAML frontmatter with `name` and
  non-empty `description`.
- **NFR-011**: rerun idempotency (covered above).
- **NFR-012**: every external library mentioned with version pin and
  rationale (Step 0 table).
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
body as the skill payload.
