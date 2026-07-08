---
name: proxmox-k3s-pipeline
description: Bring up two k3s clusters (cicd + apps) on a single Proxmox host using OpenTofu, an Ubuntu 24.04 LTS + k3s golden template, and an Agent-driven bootstrap. Use when the user says "bring up both clusters", "deploy the pipeline", "run spec 001", "bootstrap a cluster", "scale workers", "decommission a cluster", "fix the cloudflare fallback", "rebuild the ubuntu template", or "sync DNS to SDN". Outputs a fully bootstrapped cluster pair with public HTTPS via Cloudflare Tunnel (no host open ports) and apps->cicd cross-cluster Service consumption via ExternalName.
---

# Proxmox k3s Pipeline

End-to-end pipeline for provisioning two Ubuntu+k3s clusters on a
single Proxmox host. The pipeline drives five numbered top-level
phases; the bootstrap phase (Phase 4) further decomposes into seven
ordered sub-phases (cloudinit, install_k3s, k3s, helm, kubeconfig, host_ports,
externalname). Each (sub-)phase has a single CLI entry point and
explicit success criteria that the agent MUST assert before
proceeding.

For the live cluster state (what is running, what was installed,
what is broken) see
[docs/cluster-state.md](../../../docs/cluster-state.md). This skill
is the **operator playbook**; that document is the **operator
reference**.

**Pipeline state: 2026-07-08 -- Phases 0-4 verified end-to-end on
kvm.example.net (BigBertha, PVE 9.2.3, Ubuntu 24.04 LTS Noble +
k3s v1.34.9+k3s1).** All four cluster VMs (cicd-cp-1 @ SDN
10.0.0.65, cicd-w-1 @ 10.0.0.64, apps-cp-1 @ 10.0.0.67, apps-w-1 @
10.0.0.66) are cloned, running, and have a working
qemu-guest-agent. cicd-cp-1 runs the k3s server, cicd-w-1 is
joined as a worker, Cilium + kube-vip + proxmox-cloud-controller-
manager (proxmox-ccm 0.2.29 pulled, deployment readiness pending
on a credentials URL fix) + proxmox-csi-plugin 0.5.9 are installed
via Helm, and the operator kubeconfig is merged into
`~/.kube/config`. apps-cp-1 runs the k3s server; apps-w-1 has its
agent installed and `activating`. The Phase-4 sub-phases were
renamed from `talos` to `cloudinit` when the OS pivot landed
(2026-07-07), and `install_k3s` was added between `cloudinit` and
`k3s` when the canonical install plan landed on 2026-07-08 (see
[docs/install-k3s-plan.md](../../../docs/install-k3s-plan.md)).

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

The skill assumes the live host `kvm.example.net` running PVE
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
| `PROXMOX_API_URL` | Proxmox API endpoint, e.g. `https://kvm.example.net:8006/api2/json` | WP00+ |
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
`kvm.example.net`).

```bash
SSH_AUTH_SOCK=/home/bruj0/.bitwarden-ssh-agent.sock \
  ssh -o BatchMode=yes -p 6022 root@kvm.example.net 'echo ok'
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
| `cilium` (Helm chart) | `1.16.x` | rationale: matches the Ubuntu 24.04 HWE kernel (6.8) shipped with the cloud image; supports `gatewayAPI.enabled` plus eBPF host routing |
| `kube-vip` (Helm chart) | `0.9.9` | rationale: latest stable as of 2026-07; values shape is `config.address` + `env.cp_enable` (NOT `controlPlane.enabled`, which was a 1.x-only preview that never shipped) |
| `proxmox-cloud-controller-manager` (OCI Helm chart) | `0.2.29` | rationale: latest stable; OCI ref `oci://ghcr.io/sergelogvinov/charts/proxmox-cloud-controller-manager` (HTTP path 404s). Required for `topology.kubernetes.io/region` + `zone` labels on the apps cluster nodes |
| `proxmox-csi-plugin` (OCI Helm chart) | `0.5.9` | rationale: latest stable; OCI ref `oci://ghcr.io/sergelogvinov/charts/proxmox-csi-plugin`. Supports PVE 9.x and lvm-thin on `data1/data1` |
| `cert-manager` (Helm chart) | `1.20.x` | rationale: latest stable; in-cluster CA only, no ACME solvers |
| `k3s` | `1.34.x` | rationale: matches the Cilium + kube-vip versions; no known CVEs |
| `helm` | `3.x` | rationale: required for `helm upgrade --install`; matches what k3s 1.34 ships |
| Ubuntu cloud image (noble) | `noble-24.04.x` | rationale: LTS through 2029; cloud image ships with `qemu-guest-agent` package (no sideloading required); cloud-init NoCloud datasource auto-discovers seeded ISOs |

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
  ssh -p 6022 root@kvm.example.net 'pveum user token delete root@pam tf-bootstrap'
# root@pam itself stays -- you need it for any future admin operations
```

## Step 0c -- Library-version pins for environment tooling

In addition to the pipeline libraries (Step 0 table), the
**deployment environment** requires these pinned tools. Mismatched
versions cause silent API differences.

| Tool | Required | Notes |
|---|---|---|
| `tofu` (OpenTofu) | `>= 1.6.0` | `-chdir=` syntax requires 1.6+ |
| `helm` | `3.x` | Matches what k3s 1.34 ships |
| `kubectl` | `>= 1.30` | For bootstrap phase verification |
| `qm` (PVE CLI) | shipped | VM lifecycle (`qm clone`, `qm set`, `qm start`) |
| `socat` | shipped on PVE | Used by `scripts/capture_serial.py` to read `/var/run/qemu-server/<vmid>.serial0` |
| `packer` | NOT USED | Pipeline pivoted off Packer 2026-07-07; see `Step 1` for the Python+cloud-image flow |
| `talosctl` | NOT USED | Pipeline pivoted off Talos 2026-07-07; k3s runs under `systemd` on Ubuntu |

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
  ssh -p 6022 root@kvm.example.net 'pveum user token delete root@pam tf-bootstrap'
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

Goal: bake an Ubuntu 24.04 LTS + qemu-guest-agent + cloud-init
golden image into a Proxmox template (current VMID **900**,
written to `build/image-id.txt`). One-shot; idempotent on rerun
(the build's preflight step destroys any pre-existing template at
the target VMID before recreating it).

**Why Ubuntu + k3s and not Talos?** The 2026-07-07 OS pivot
moved the pipeline off Talos Linux. The Talos + Sidero Image
Factory flow was hard to debug on the serial console, and the
cloud-init + systemd story on Ubuntu Noble is well-trodden. The
serial-console capture recipe we needed for Talos is no longer
needed for the build itself (Ubuntu cloud images boot cleanly
through OVMF and report agent-ready within ~10 s), but
`scripts/capture_serial.py` is kept as a debug fallback for
post-mortem diagnostics.

**Important: the pipeline does NOT use Packer, Talos, the
Sidero Image Factory, qemu-nbd, losetup, or a custom NoCloud
seed ISO.** The build flow is now the canonical Proxmox+Ubuntu
recipe (see `docs/proxmox-serial-capture.md`'s neighbors and
the operator-skill runbook at the bottom of this document):

  1. Download the upstream cloud image.
  2. Bake qemu-guest-agent + openssh-server + cloud-init into
     the image with `virt-customize` (libguestfs-tools) on the
     PVE host.
  3. `qm create` an OVMF/q35 VM with the agent channel enabled
     and a serial console.
  4. `qm importdisk` + `qm resize` to attach the cloud image as
     scsi0 and grow it to 32 GB.
  5. `qm set --ide2 data1:cloudinit` -- Proxmox's *native*
     cloud-init drive (NOT a custom ISO).
  6. `qm set --ciuser ubuntu --sshkeys <file> --ipconfig0 ip=dhcp`
     -- Proxmox regenerates the cloud-init drive on every
     `qm start` from these values.
  7. `qm start` + `qm agent ping` + `qm agent network-get-interfaces`
     to verify the VM has DHCP'd.
  8. `qm shutdown` + `qm template`.

The cluster's Phase 2 clones (per-VM cloud-init seeds) inherit
the template's `--ciuser ubuntu --sshkeys <file> --ipconfig0 ip=dhcp`
and re-apply them with per-VM overrides.

### 1.1 -- Image source

| Component | Source | Version pin |
|---|---|---|
| Cloud image | `https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img` | `noble-24.04.x` (SHA256SUMS-pinned daily) |
| OVMF firmware | `/usr/share/pve-edk2-firmware/OVMF_CODE_4M.secboot.fd` (PVE default) | shipped with PVE 9.2.3 |
| k3s | `https://get.k3s.io` (`INSTALL_K3S_CHANNEL=stable`) | `1.34.x` |
| `libguestfs-tools` (virt-customize) | Debian apt on PVE | installed on first build if missing |

The cloud image does NOT ship `qemu-guest-agent` (a known gap
of Ubuntu 24.04 LTS cloud images). The build's `virt-customize`
step installs it BEFORE the VM is created, so the agent is
guaranteed to be up by the time `qm agent ping` returns.

### 1.2 -- What `tools/build_image` does (SS1 orchestrator)

The orchestrator at `tools/build_image/__init__.py` is a single
Python entry point. It drives the full flow end-to-end:

1. **Preflight** -- destroy any pre-existing template at
   `TEMPLATE_VMID` (= 900). Idempotent: re-running with the
   same VMID simply destroys + rebuilds. The build does NOT
   touch VMID 950 (it has a stuck LV from an earlier Packer
   run; pinned in `versions.lock.yaml::vmid_950_stuck_lv`).
2. **Cloud-image acquisition** -- download the Noble cloud image
   to `/tmp/noble-server-cloudimg-amd64.img` (cached on the
   operator host; SHA256-verified against
   `cloud-images.ubuntu.com/noble/current/SHA256SUMS`).
3. **Stage + `virt_customize`** -- scp the image to
   `/var/lib/vz/template/iso/` on PVE, then run
   `virt-customize -a ... --install qemu-guest-agent,openssh-server,cloud-init
   --run-command 'systemctl enable qemu-guest-agent ssh'
   --run-command 'systemctl mask getty@tty1.service'
   --run-command 'update-initramfs -u' --truncate /etc/machine-id`.
   The agent is baked into the image before any VM is created,
   so there is no race between cloud-init and the agent.
4. **`qm create 900`** with the canonical Proxmox recipe:
   - `machine q35`, `bios ovmf`, `efidisk0 data1:1,efitype=4m,pre-enrolled-keys=0`
     (no Secure Boot; Ubuntu's shim works either way but
     `pre-enrolled-keys=0` avoids the OVMF-DB-surprise class of
     bug and matches the canonical Proxmox community guide).
   - `scsihw virtio-scsi-single`, `boot: order=scsi0` (cloud
     image IS the disk; no ISO boot).
   - `agent enabled=1` (qemu-guest-agent channel).
   - `serial0 socket`, `vga serial0` (canonical Proxmox recipe;
     serial console available for diagnostics).
5. **Disk import** -- `qm importdisk 900 <image> data1 -format
   raw -target-disk scsi0`, then `qm resize 900 scsi0 32G` to
   grow the LV from the cloud image's ~3.5 GB to a 32 GB
   thin-LV, then `qm set --scsi0 data1:vm-900-disk-1,discard=on,
   iothread=1` to attach the new disk.
6. **Attach Proxmox's NATIVE cloud-init drive** -- `qm set 900
   --ide2 data1:cloudinit`. The build does NOT generate a
   NoCloud seed ISO; Proxmox owns the cloud-init drive and
   regenerates it on every `qm start` from the stored config.
7. **Configure cloud-init defaults** -- `qm set 900 --ciuser
   ubuntu --sshkeys <file> --ipconfig0 ip=dhcp`. The cluster
   Phase 2 overrides `--ipconfig0` per-VM and (optionally)
   `--sshkeys` with per-cluster or per-VM keys.
8. **First boot + agent verification** -- `qm start 900` then
   poll `qm agent 900 ping` for up to 240 s. Once the agent
   responds, `qm agent 900 network-get-interfaces` returns the
   DHCP-allocated IP (10.0.0.x from the SDN dnsmasq). No
   SSH-into-VM customize is needed -- virt-customize already
   baked the agent in.
9. **Graceful shutdown + template conversion**:
   - `qm shutdown 900` (10 s grace) -> `wait_for_vm_stopped`
     (90 s) -> `qm stop` (10 s) if needed.
   - `qm template 900` -- convert to a PVE template. PVE
     renames `vm-900-disk-1` to `base-900-disk-1` and
     `vm-900-disk-0` to `base-900-disk-0` automatically.
10. **Write `build/image-id.txt`** containing `900\n`. The
    `terraform_data.image_id_present` precondition in
    `infra/clusters/{cicd,apps}/main.tf` reads this file.

The build writes a JSONL audit log to
`logs/build-image_<UTC>.log` (and a `latest-build-image.log`
symlink). Each step logs `step=<name>` plus structured fields
(`vmid`, `size_bytes`, `attempt`, etc.) and a `resolution`
string on failure so the operator can re-run without guessing
the next action.

### 1.3 -- Apply

```bash
SSH_AUTH_SOCK=/home/bruj0/.bitwarden-ssh-agent.sock \
PVE_TOKEN_ID='k3s-terraform@pam!tf' \
PVE_TOKEN_SECRET='<secret-uuid>' \
python -m tools.build_image \
  --pve-endpoint https://kvm.example.net:8006/api2/json \
  --pve-node BigBertha \
  --pve-ssh-host kvm.example.net --pve-ssh-port 6022 \
  --ssh-pubkey-path ~/.ssh/kvm.example.net.pub \
  --ubuntu-image-version noble \
  --k3s-channel stable \
  --log-dir ./logs
```

(`PVE_TOKEN_ID` / `PVE_TOKEN_SECRET` are accepted for parity
with `scripts/apply_tofu.py` but are not used by the build --
everything goes over SSH. They are kept so a single .env works
for both build and apply.)

Equivalent shortcut once `.env` is set up:

```bash
make build-image UBUNTU_VERSION=noble K3S_CHANNEL=stable
```

Success criteria (assert ALL before proceeding):
1. `qm list | grep -w 900` returns a single row with `status
   stopped` and `BOOTDISK(GB) = 32.00`.
2. `grep '^template:' /etc/pve/qemu-server/900.conf` returns
   `template: 1`.
3. `grep '^ide2:' /etc/pve/qemu-server/900.conf` shows
   `ide2: data1:vm-900-cloudinit,media=cdrom`.
4. `cat build/image-id.txt` returns exactly `900` followed by
   newline.
5. `qm start 900 && sleep 8 && qm agent 900 ping` returns
   success within 30 s of boot. This proves the qemu-guest-agent
   is alive and cloud-init finished its first-boot module set.
6. `qm agent 900 network-get-interfaces` reports `eth0` with a
   `10.0.0.x` IP -- the SDN DHCP allocation worked.

Failure handling: halt the pipeline and surface the structured
error (`error`, `resolution` keys) to the operator. Do NOT proceed
to Phase 2.

### 1.4 -- Logs

The build writes one stream under `--log-dir` (default
`./logs/`):

- `build-image_<UTC>.log` -- JSONL audit log, one event per line.
- `latest-build-image.log` -- symlink at the most recent audit log.

A failed build leaves VM 900 in a known state -- either stopped
(good, retry-safe; the preflight destroys + recreates) or running
(rare, the preflight will force-stop and destroy on the next run).

### 1.5 -- Phase 1 apply-time gotchas (live host 2026-07-07)

These are the deployment-environment issues that surfaced while
building the Ubuntu+k3s template against PVE 9.2.3. Each was a
hard blocker; all are now resolved and pinned in
`versions.lock.yaml::cross_check`. The canonical Proxmox+Ubuntu
recipe (virt-customize + native cloudinit drive) avoided the
entire class of "no qemu-guest-agent on first boot" + "serial
console capture" + "initramfs EXT4 journal" issues that plagued
the earlier Packer + NoCloud-seed-ISO flow.

#### 1.5.1 -- Serial console capture is now debug-only (not part of the build)

`scripts/capture_serial.py` is kept as a **standalone debug
helper** for post-mortem diagnostics when a build fails
mysteriously. The build orchestrator does NOT launch it; we
verified on 2026-07-07 that `qm agent <vmid> ping` returns
within ~10 s of `qm start` and the agent channel is the
authoritative signal that the VM is healthy.

If you need to capture the serial console for diagnostics:

```bash
SSH_AUTH_SOCK=... scp scripts/capture_serial.py \
  root@kvm.example.net:/tmp/capture_serial.py
SSH_AUTH_SOCK=... ssh -p 6022 root@kvm.example.net \
  'python3 /tmp/capture_serial.py --vmid 900 --out /tmp/900-serial.log --duration 60'
```

The full pty-wrapped recipe (socat over pty.openpty, with
auto-respawn when PVE recycles the chardev) is documented in
`docs/proxmox-serial-capture.md` for historical reference.

#### 1.5.2 -- VMID 950 has a stuck LV (do NOT use)

VMID 950 has a stuck LV (`vm-950-disk-1`) on `data1` from the
earlier Packer-based Talos build (2026-07-06); the open handle
from `dmeventd`/`kvm-pit` prevents `lvremove` even with
`dmsetup wipe_table`. The canonical Ubuntu+k3s build therefore
uses **VMID 900** and will not collide. If you ever want to
free VMID 950:

```bash
SSH_AUTH_SOCK=... ssh -p 6022 root@kvm.example.net \
  'dmsetup remove /dev/data1/vm--950--disk--1 || true;
   lvremove -f /dev/data1/vm-950-disk-1 || true;
   qm destroy 950'
```

#### 1.5.3 -- `qemu-img resize` after `qm importdisk`

A naive `qm set --scsi0 ... size=32G` does NOT grow the disk --
it only updates the metadata to the disk's current size. The
canonical Proxmox recipe uses an explicit `qm resize <vmid>
scsi0 32G` to grow the LV. The build does this after
`qm importdisk` so the clone rootfs has room for k3s + workloads.

#### 1.5.4 -- EXT4 journal corruption: avoided by `virt-customize`

The first Ubuntu+k3s build (2026-07-07) had a recurring class of
boot failure where the imported cloud image's first boot dropped
to `initramfs` with `LABEL=cloudimg-rootfs doesn't exit` and
`/dev/sda1: UNEXPECTED INCONSISTENCY; RUN fsck MANUALLY`. Root
cause: `qemu-img rebase -b /dev/null` (or any other backing-chain
rewrite) marks the rootfs dirty; without `e2fsck` in the
initramfs the kernel cannot self-heal.

The v2 cleanup (this revision) avoids the class of bug entirely:
`virt-customize --run-command 'update-initramfs -u'` runs
INSIDE the image BEFORE the VM is created, so the initramfs
carries `e2fsck` from the start. No more `chroot $MNT
update-initramfs -u -k all` dance; no more initramfs shell on
first boot. Pinned in
`versions.lock.yaml::initramfs_e2fsck_fix`.

### 1.6 -- Reference docs

- `docs/proxmox-serial-capture.md` -- **historical** write-up of
  the serial-capture recipe. Kept because the underlying PVE
  chardev quirks are non-obvious and may surface again on
  future Talos or non-cloud-image builds.
- `scripts/capture_serial.py` -- standalone debug helper (see
  Step 1.5.1). NOT invoked by `tools/build_image`.

## Step 2 -- Phase 2: Provision the clusters (SS2)

Apply OpenTofu against `infra/clusters/cicd/` and
`infra/clusters/apps/` to clone the Ubuntu+k3s template (VMID
**900**) into per-cluster VMs (1 control-plane + 1 worker per
cluster today; scale up via the runbook).

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
plan && tofu apply -auto-approve`. The `image_id` data source
reads `build/image-id.txt` (currently `900`), so the cluster
roots automatically pick up the new Ubuntu template without any
HCL change.

Success criteria (assert ALL before proceeding):
1. Both `tofu apply` calls exit 0. `qm list` shows 4 new VMs
   (111, 112, 113, 114) with `status running`, 32 GB scsi0,
   RAM 4096 (cp) / 8192 (w), `boot: order=scsi0`.
2. `infra/clusters/cicd/output.json` and
   `infra/clusters/apps/output.json` both exist and parse as
   JSON with `cluster_name`, `vip`, `pod_cidr`, `svc_cidr`,
   and `nodes[]` keys.
3. `infra/clusters/cicd/manifests/traefik-helmchartconfig.yaml`
   exists and parses as YAML.
4. `qm agent <vmid> ping` returns success for all 4 VMs within
   30 s of boot. This proves the qemu-guest-agent baked into the
   cloud image is alive.
5. `qm agent <vmid> network-get-interfaces` reports `eth0`
   with a `10.0.0.x` IP per VM (allocated by the SDN dnsmasq
   from the `10.0.0.50-200` pool).
6. `qm guest exec <vmid> -- cloud-init status` returns
   `status: done` for all 4 VMs within 120 s of first boot.

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

#### 2.2.3 -- `k3s-cluster` role needs the full VM lifecycle priv set

The spec T005 12-priv set is **insufficient** for Phase 2. Each
of these returns `403 Permission check failed` on BigBertha
without the priv:

| Priv | Needed for |
|---|---|
| `Sys.Audit` | `/access` namespace reads |
| `VM.Audit` | Read VM cfg / qemu list |
| `VM.Clone` | Clone VMID 900 to 111-114 |
| `VM.Migrate` | Cleanup moved/half-baked templates |
| `VM.Config.CDROM` | Attach/detach cloud-init drive |
| `VM.Config.HWType` | Set `machine=q35` (UEFI boot) |
| `VM.Snapshot.Rollback` | Restore after template-bake failure |

Total: **20 privs** (12 spec + 8 above). The 20th is
`Sys.Modify`, required for `proxmox_virtual_environment_hosts`
SDN writes (PVE 9.2.x rejects without it even when SDN.Use is
present). Verify with
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
`tools/bootstrap_cluster.py` to wire `--cluster-cidr` for the
per-node k3s systemd unit and to render Cilium's
`clusterPoolIPv4PodCIDR`). The module writes them via
`jsonencode` in
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
gives the VMs addresses like `10.0.0.64-67`. The module's
records are therefore WRONG on first apply. Concrete example
from the 2026-07-07 live-host run:

| Host | PowerDNS A record (wrong) | Actual VM IP |
|---|---|---|
| `cicd-cp-1.intranet.local` | `10.0.1.0` | `10.0.0.65` |
| `cicd-w-1.intranet.local` | `10.0.1.1` | `10.0.0.64` |
| `apps-cp-1.intranet.local` | `10.0.2.0` | `10.0.0.67` |
| `apps-w-1.intranet.local` | `10.0.2.1` | `10.0.0.66` |

**Fix**: after every successful `apply_tofu.py cicd` and
`apply_tofu.py apps`, run `scripts/sync_dns_to_sdn.py` to
read the actual VM IPs via the qemu-guest-agent and
PATCH the PowerDNS records.

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
  ssh -p 6022 root@kvm.example.net \
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

Runs the seven-phase bootstrap. Order is enforced by `PHASES` in
`tools/bootstrap_cluster.py`. Each phase records its success in
`infra/clusters/<name>/bootstrap_state.json` (gitignored); rerunning
is a no-op for completed phases.

**Prerequisites** (must all be true before invoking the bootstrap):

1. Phase 2 has been applied: `infra/clusters/<name>/output.json` exists
   and lists the cluster's control-plane + worker nodes with their
   SDN-allocated IPs.
2. `scripts/sync_dns_to_sdn.py --cluster <name>` has been run (the SDN
   IPAM allocates 10.0.0.50-200 regardless of `var.ip_start`, so the
   post-apply DNS sync is required).
3. `scripts/capture_host_ports_baseline.sh infra/clusters/<name>` has
   produced `infra/clusters/<name>/host_ports_baseline.txt` (Phase 3).

### 4.0 -- The single command

Once the prerequisites are satisfied, deploy k3s and bootstrap the
cluster fully with one command:

```bash
# cicd cluster
SSH_AUTH_SOCK=/home/bruj0/.bitwarden-ssh-agent.sock \
  python -m tools.bootstrap_cluster --cluster cicd

# apps cluster (after cicd is healthy AND infra/clusters/apps/
# has been provisioned by Phase 2)
SSH_AUTH_SOCK=/home/bruj0/.bitwarden-ssh-agent.sock \
  python -m tools.bootstrap_cluster --cluster apps
```

This single invocation runs every sub-phase end-to-end:

1. **`cloudinit`** -- verify every clone VM finished first-boot
   cloud-init (no-op gate; the cluster root's tofu module attached
   the per-VM NoCloud seed ISO during Phase 2 via `qm set --ide2
   data1:cloudinit`).
2. **`install_k3s`** -- SSH into every control-plane + worker VM
   via the PVE jump host and run the upstream installer
   (`curl -sfL https://get.k3s.io | INSTALL_K3S_... sh -`). Server
   nodes get the canonical flags (`--flannel-backend=none
   --disable=traefik --disable=servicelb --disable=local-storage
   --disable=metrics-server --kubelet-arg=cloud-provider=external
   --node-ip=<ip> --node-external-ip=<ip> --tls-san=<vip>`); agents
   join via `K3S_URL=https://<vip>:6443`. Hash-based idempotent: a
   re-run on a healthy cluster is a no-op in <10 s. See Step 4a.
3. **`k3s`** -- probe the apiserver over a PveSshProxy-forwarded
   tunnel: `kubectl --kubeconfig <cluster>/kubeconfig get --raw
   /healthz` must return `ok`. This phase proves the apiserver is
   reachable from the operator host via the same tunnel pattern the
   operator tools use.
4. **`helm`** -- open an apiserver port-forward through PVE and
   install the two critical-path releases first (Cilium 1.16.1 +
   kube-vip 0.9.9), then the remaining four (proxmox-cloud-
   controller-manager 0.2.29, proxmox-csi-plugin 0.5.9, strrl/
   cloudflare-tunnel-ingress-controller 0.0.23, cert-manager
   1.20.x), then `kubectl apply` the pre-rendered Traefik
   `HelmChartConfig` from
   `infra/clusters/<name>/manifests/`. **All apiserver calls in this
   phase route through PveSshProxy** -- the CPs are on SDN
   10.0.0.0/24, the operator is on 10.0.10.0/24, so direct
   `kubectl`/`helm` from the operator host would fail with "no route
   to host".
5. **`kubeconfig`** -- reuses the same PveSshProxy to fetch
   `/etc/rancher/k3s/k3s.yaml` from the first CP, rewrites the
   `server:` URL to `https://127.0.0.1:<local_port>`, writes it to
   `infra/clusters/<name>/kubeconfig` AND merges it into
   `~/.kube/config` (timestamped backup first).
6. **`host_ports`** -- assert no new DNAT rules have been added to
   the PVE nft prerouting chain since the Phase-3 baseline capture
   (M2 misfit verifier).
7. **`externalname`** -- apps-cluster only: apply the cross-cluster
   ExternalName Services kustomization (WP06) that lets apps
   workloads reach cicd Services via
   `<svc>.cicd-system.svc.cluster.local` -> PowerDNS -> cicd VIP.

### 4.1 -- Required environment

The bootstrap reads Proxmox + Cloudflare credentials via the
env-only `SecretLoader`
([tools/lib/secret_loader.py](../../../tools/lib/secret_loader.py)).
The canonical names expected by the loader are:

| Env var | Source | Used for |
|---|---|---|
| `SSH_AUTH_SOCK` | Bitwarden SSH agent (`/home/bruj0/.bitwarden-ssh-agent.sock`) | PveSshProxy jumps |
| `PROXMOX_TOKEN_ID`     | `.env::PROXMOX_API_TOKEN`     | proxmox-ccm + proxmox-csi chart values |
| `PROXMOX_TOKEN_SECRET` | `.env::PROXMOX_API_TOKEN`     | proxmox-ccm + proxmox-csi chart values |
| `CF_API_TOKEN`         | `.env::CLOUDFLARE_TOKEN_CREATOR` | cloudflare-tunnel controller |
| `CF_ACCOUNT_ID`        | `.env::CLOUDFLARE_ACCOUNT_ID` | cloudflare-tunnel controller |

If your `.env` uses different names (e.g. `PROXMOX_API_TOKEN` instead
of `PROXMOX_TOKEN_ID`), alias them in the shell before running:

```bash
export SSH_AUTH_SOCK=/home/bruj0/.bitwarden-ssh-agent.sock
export PROXMOX_TOKEN_ID="$PROXMOX_API_TOKEN"
export PROXMOX_TOKEN_SECRET="$PROXMOX_API_TOKEN"
export CF_API_TOKEN="$CLOUDFLARE_TOKEN_CREATOR"
export CF_ACCOUNT_ID="$CLOUDFLARE_ACCOUNT_ID"
```

### 4.2 -- Idempotency contract

On a rerun, the script reads
`infra/clusters/<name>/bootstrap_state.json` and skips phases whose
name appears in `phases_done`. This is the canonical "convergence from
partial state" path required by NFR-011. **Idempotency is the
contract; the operator may safely rerun the bootstrap at any point.**

To force every phase to re-run:

```bash
rm -f infra/clusters/cicd/bootstrap_state.json
python -m tools.bootstrap_cluster --cluster cicd
```

To run only a subset:

```bash
python -m tools.bootstrap_cluster --cluster cicd \
  --phases helm,kubeconfig,host_ports,externalname
```

The apiserver tunnel opened during the `helm` phase is held alive
across phases for the lifetime of the script and torn down in a
`finally` block, so the same tunnel is reused for the `kubeconfig`
phase. The script does not leave orphan ssh processes on the
operator host.

### 4.3 -- Success criteria

Assert ALL before proceeding:

1. `kubectl --context cicd get nodes` shows all control-plane +
   worker nodes in `Ready` state.
2. `kubectl --context cicd -n kube-system get pods -A` shows Cilium
   + proxmox-ccm + proxmox-csi + cloudflare-tunnel + cert-manager
   pods `Running` (no `kube-vip` static pod -- on Ubuntu+k3s the
   API VIP is owned by `kube-vip`'s userspace daemonset, not a Talos
   static pod manifest).
3. `python -m tools.bootstrap_cluster --cluster cicd --phases all`
   exits 0 in <60 seconds (idempotent rerun -- every phase skips
   because state is already done).

## Step 4a -- install_k3s sub-phase

Lands on 2026-07-08 (per [docs/install-k3s-plan.md](../../../docs/install-k3s-plan.md)).
The recipe is implemented in [tools/lib/k3s_installer.py](../../../tools/lib/k3s_installer.py)
and wired into the dispatcher by `_run_install_k3s` in
[tools/bootstrap_cluster.py](../../../tools/bootstrap_cluster.py). Versions come
from `tools/versions.lock.yaml::k3s_stable_version` (currently
`v1.34.9+k3s1`).

**Inputs**: `infra/clusters/<name>/output.json` (control-plane +
worker IPs, VIP). Run **after** `cloudinit` succeeds and **before**
the `k3s` healthz phase.

### 4a.1 -- Recipe per node

| Role | Env vars exported on the remote shell | Installer tail-flags |
|---|---|---|
| control_plane | `INSTALL_K3S_VERSION=v1.34.9+k3s1`, `INSTALL_K3S_CHANNEL=stable`, `K3S_NODE_NAME=<name>` | `server --flannel-backend=none --disable=traefik --disable=servicelb --disable=local-storage --disable=metrics-server --kubelet-arg=cloud-provider=external --node-ip=<ip> --node-external-ip=<ip> --tls-san=<vip>` |
| agent | `INSTALL_K3S_VERSION=v1.34.9+k3s1`, `INSTALL_K3S_CHANNEL=stable`, `K3S_NODE_NAME=<name>`, `K3S_URL=https://<vip>:6443`, `K3S_TOKEN=<node-token>` | `agent --flannel-backend=none --node-ip=<ip> --node-external-ip=<ip>` |

Both roles invoke the upstream installer at <https://get.k3s.io>
over SSH; the env is rendered inline into the remote shell so the
`K3S_TOKEN` and any other env var never appears in the operator's
argv / process list.

### 4a.2 -- Idempotency

Two gates, in order:

1. **Upstream installer** is hash-checked -- `install.sh` no-ops on
   identical env: `No change detected so skipping service start`.
2. **Python wrapper** short-circuits on
   `systemctl is-active --quiet k3s && test -f /etc/rancher/k3s/k3s.yaml`
   (server) or `systemctl is-active --quiet k3s-agent` (agent) BEFORE
   the upstream installer is invoked at all. This protects against
   anyone calling the phase with the cluster already up.

```bash
# operator-driven invocation (the canonical one)
SSH_AUTH_SOCK=/home/bruj0/.bitwarden-ssh-agent.sock \
  python -m tools.bootstrap_cluster --cluster cicd \
  --phases cloudinit,install_k3s,k3s

# phase-only re-run on a healthy cluster is a no-op (< 10 s)
SSH_AUTH_SOCK=/home/bruj0/.bitwarden-ssh-agent.sock \
  python -m tools.bootstrap_cluster --cluster cicd --phases install_k3s
# Expect: log line "k3s.skip_install reason=already healthy ..."
```

### 4a.3 -- Live-host gotchas (2026-07-08)

**4a.3.1 -- `--tls-san=<vip>` is mandatory.**
K3s generates a serving cert whose SANs include `--node-ip` /
`--node-external-ip` by default. Without `--tls-san=<vip>` the
kubeconfig pulled from the server has `server: https://<vip>:6443`
but the serving cert does NOT carry the VIP SAN, so external
clients fail with `x509: certificate is not valid for <vip>`. The
verification note [docs/install-k3s-vip-verification.md](../../../docs/install-k3s-vip-verification.md)
records the live-host probe that surfaced this.

**4a.3.2 -- The VM IP comes from the SDN DHCP lease, not from
`output.json::nodes[i].ip`.** Phase 2 still emits the intended
`10.0.1.x / 10.0.2.x` IPs as PowerDNS records, but the SDN IPAM
hands out `10.0.0.50–10.0.0.200` regardless of `var.ip_start`. The
installer reads `--node-ip` from the actual lease (`qm agent
<vmid> network-get-interfaces`) so the kubelet advertises the IP
that the rest of the SDN sees.

**4a.3.3 -- SSH user is `ubuntu`, not `root`.**
The Ubuntu cloud image sets `qm set --ciuser ubuntu`, which all
clones inherit. The Bitwarden SSH key lives on root@<sdn-ip> by
virtue of the cluster module's `--sshkeys` arg (proxied through
`Proxmox-Jump-Host -> -W <sdn-ip>:22`).

**4a.3.4 -- Idempotency probes require a working `qm agent` channel
on every VM.** This is the same constraint the cluster's Phase 2
satisfies; the bootstrap assumes each VM came up with the agent
alive. If the cluster has been live for a while and an agent has
gone stale, re-running the cluster root tofu module will reset
that, but the install_k3s phase itself does not restart VMs.

**4a.3.5 -- SSH user is `ubuntu` (sudo NOPASSWD), not `root`.**
The cloud image's sshd_config rejects `root` logins with "Please
login as the user ubuntu rather than the user root.". The
Bitwarden SSH key (proxied through PVE) lands in `/root/.ssh/authorized_keys`
**inside** each VM via the cluster root's `--sshkeys` arg; for
root-privileged operations (systemctl, /etc/rancher, kubelet
config) we wrap calls in `sudo -n bash -c '...'`.

**4a.3.6 -- `sudo -n bash -c` strips the caller's env.** A bare
env-prefix before the sudo invocation IS silently dropped (e.g.
`INSTALL_K3S_VERSION=v1.34.9+k3s1 sudo -n bash -c '...'` ends up
running k3s's installer with an EMPTY env). Fix: put `export
K=V; export T=V;` INSIDE the bash -c, then the actual install.
This bit the first cicd-w-1 install attempt; the env file
remained 0 bytes and `journalctl` reported "Error: --token is
required".

**4a.3.7 -- `k3s agent` does NOT accept `--flannel-backend`.**
That flag is server-only; the agent binary rejects it with "flag
provided but not defined: -flannel-backend" in journalctl. Server
passes `--flannel-backend=none`; agent inherits the CNI choice
via the kubelet join handshake and MUST NOT pass the flag. (Bit
the first cicd-w-1 attempt too.) See test `test_plan_for_agent_joins_vip_not_eth0`
in `tools/tests/test_k3s_installer.py` for the regression guard.

**4a.3.8 -- Use ProxyCommand, not `-W`, for the SSH tunnel.**
OpenSSH's `-W <host>:22` cmdline flag forces stdio-forwarding
mode and refuses a remote command. For the install call we need
both tunneling AND a remote exec, so `-o ProxyCommand="ssh -W
%h:%p ..."` is the right form. See
`tools/lib/k3s_installer.py::_ssh_argv` for the canonical
shape.

**4a.3.9 -- Agent reaches `127.0.0.1:6444` over a local
load-balancer, which then tunnels to the VIP (`10.0.0.30` /
`10.0.0.40`).** Until the `helm` phase lands `kube-vip` on the
server, that load-balancer connection resets with
`127.0.0.1:NNNNN -> 127.0.0.1:6444: read: connection reset by peer`.
This is the agent retrying every 10 s waiting for the apiserver.
It's expected post-install_k3s; the unit stays `activating` until
the `helm` phase completes. Don't mistake this for an
install_k3s failure.

### 4a.4 -- Success criteria

1. `systemctl is-active k3s` returns `active` on every
   control-plane VM.
2. `systemctl is-active k3s-agent` returns `active` on every
   worker VM.
3. `/var/lib/rancher/k3s/server/node-token` is non-empty on the
   first control-plane node.
4. `curl -sk https://10.0.0.30:6443/healthz` returns `ok` from
   the operator host within 90 s of the install completing (after
   the helm phase lands kube-vip; before that the VIP is
   upstream-claimed by the kubelet loopback and is only reachable
   from inside the cluster).
5. Rerunning
   `python -m tools.bootstrap_cluster --cluster cicd --phases install_k3s`
   exits 0 in <10 s with `k3s.skip_install` log entries -- the
   upstream installer is never invoked a second time.

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

## Phase 1 -> Phase 4 history (Talos -> Ubuntu pivot, then v2 cleanup)

The pipeline was originally built around Talos Linux (v1.10.0 in
2026-07-05, then v1.13.5 in 2026-07-07 via Sidero Image Factory).
On 2026-07-07 the operator decided to pivot to Ubuntu 24.04 LTS
+ k3s because the Talos serial-console debug cycle was too painful
on this host. After the first pivot the build used a custom
NoCloud seed ISO baked on the operator host -- that still required
a post-create `apt install qemu-guest-agent` step inside the VM
that failed silently, leaving all clones without the agent.

The **2026-07-07 v2 cleanup** replaced the custom NoCloud flow
with the canonical Proxmox+Ubuntu recipe (virt-customize +
native cloud-init drive) and moved the template to VMID 900. The
second pivot removed the entire class of "no qemu-guest-agent on
first boot" + "serial console capture" + "initramfs EXT4 journal"
issues that plagued the first Ubuntu build.

| Layer | Before (Talos) | First Ubuntu pivot (2026-07-07) | Canonical Ubuntu+k3s v2 (2026-07-07) |
|---|---|---|---|
| Golden image | Sidero Image Factory schematic | Noble cloud image + custom NoCloud seed ISO | Noble cloud image + `virt-customize` (qemu-guest-agent baked in) + Proxmox's native cloud-init drive |
| VMID | 900 | 952 (950/951 stuck) | 900 |
| Template conversion | `qm template 900` | `qm template 952` + NoCloud seed detach | `qm template 900` (native drive stays, regenerated from `--ciuser/--sshkeys/--ipconfig0` on every `qm start`) |
| First-boot customize | n/a (Talos installer) | `apt install qemu-guest-agent` (silently failed) | n/a (qemu-guest-agent is in the image from the start) |
| Cluster bootstrap | `talosctl apply-config` | cloud-init NoCloud seed ISO per VM; k3s installer runs as `runcmd` | Same: cloud-init runcmd installs k3s, but driven by Proxmox's native drive |
| API VIP | kube-vip in Talos machineconfig | kube-vip userspace (`kube-vip-cloud-provider` static pod) installed by the `cloudinit` phase | Same |
| Capture recipe | `ssh ... timeout N cat /var/run/qemu-server/<vmid>.serial0` | `scripts/capture_serial.py` (build-time, mandatory) | `scripts/capture_serial.py` (debug-only, see Step 1.5.1) |
| `--flannel-backend` | n/a | `none` (Cilium takes over) | `none` (Cilium takes over) |

The Phase 0/2/3/5 plumbing (tokens, tofu cluster module, host-ports
baseline, final verification) is unchanged because it was already
OS-agnostic. The Phase 4 sub-phases are
`cloudinit, install_k3s, k3s, helm, kubeconfig, host_ports, externalname` --
renamed from `talos` to `cloudinit` when the OS pivot landed.

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
