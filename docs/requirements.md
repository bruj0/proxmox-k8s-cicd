# Requirements

This document lists everything you need to run the proxmox-k8s-cicd
pipeline end-to-end and walks you through getting each token /
credential. The values land in `./.env` at the repo root (gitignored;
mode 0600) and are read by:

- `scripts/apply_tofu.py` (Phase 0 + Phase 2 — OpenTofu env
  translation)
- `tools/build_image` (Phase 1 — used for parity only, the build is
  SSH-only)
- `tools/bootstrap_cluster.py` (Phase 4 — reads the cluster's
  `output.json`, not `.env` directly)
- `scripts/sync_dns_to_sdn.py` (post-apply PowerDNS fix-up)

## TL;DR — the 9 keys

| `.env` key | Source | Required for |
|---|---|---|
| `PROXMOX_API_URL` | Your PVE host (e.g. `https://pve.example.net:8006/api2/json`) | Phase 0 + Phase 2 |
| `PROXMOX_API_TOKEN` | PVE → Datacenter → API Tokens → `root@pam!tf-bootstrap` (Privilege Separation, `Sys.Modify`) | Phase 0 + Phase 2 |
| `CLOUDFLARE_TOKEN_CREATOR` | Cloudflare → My Profile → API Tokens → Create Token → Edit Cloudflare Workers template | Phase 0 (mints the scoped child token) |
| `CLOUDFLARE_GLOBAL_API_KEY` | Cloudflare → My Profile → API Tokens → Global API Key (legacy) | Phase 0 (mints child tokens — `cfat_*` admin tokens cannot) |
| `CLOUDFLARE_GLOBAL_API_EMAIL` | The email tied to the Global API Key | Phase 0 |
| `CLOUDFLARE_ACCOUNT_ID` | Cloudflare right-sidebar on any zone page | Phase 0 + Phase 2 |
| `CLOUDFLARE_ZONE_ID` | Cloudflare zone overview → lower-right API block | Phase 0 + Phase 2 |
| `CLOUDFLARE_DOMAIN` | Your apex domain (e.g. `example.net`) | Phase 0 + Phase 2 |
| `GITLAB_PAT` | GitLab → User Settings → Access Tokens (`api` + `write_repository` scopes) | Phase 0/2 (tofu HTTP backend init) |
| `POWERDNS_API_KEY` | Your PowerDNS server (LXC 101, `10.0.0.3:8081`) | Phase 2 post-apply fix-up |

Plus one non-secret:

| Key | Source |
|---|---|
| `OPERATOR_SSH_PUBKEY` | Your `~/.ssh/<key>.pub` (must be authorized in PVE's `/root/.ssh/authorized_keys`) — used by `tools.build_image` to seed the template's `cloud-init` sshkeys |

## 1. Proxmox VE host

You need a single-node PVE 9.x host with:

- One SDN zone (the skill defaults to `intranet`, attached to `vnet0`,
  subnet `10.0.0.0/8` with DHCP range `10.0.0.50-200`).
- An `lvmthin` storage pool (the skill defaults to `data1`).
- SSH reachable on a non-default port (the skill defaults to `6022`)
  with key-based auth for `root` (the `tools.build_image` SSH wrapper
  connects as root).

If your host is brand-new, install PVE 9.x and add the SDN zone:

```bash
# On the PVE host, after install
pvesh create /cluster/sdn/zones --zone intranet --type simple
pvesh create /cluster/sdn/vnets --vnet vnet0 --zone intranet \
  --tag 100 --alias "intranet"
pvesh create /cluster/sdn/vnets/vnet0/subnets --subnet 10.0.0.0/8 \
  --dhcp-dns-server 1.1.1.1 --dhcp-range start=10.0.0.50,end=10.0.0.200
pvesh set /cluster/sdn --apply
```

If you have an existing PVE host with a different SDN layout, override
`vnet_bridge` and `ip_start` in `infra/clusters/<name>/main.tf` and
`var.disk_storage_pool` in the cluster module.

## 2. Proxmox bootstrap API token (`PROXMOX_API_TOKEN`)

Phase 0 mints a per-terraform scoped token, but it needs an initial
**bootstrap token** to do so. The bootstrap token must have
`Sys.Modify` (only the `Administrator` PVE role has it).

1. Open the PVE web UI: `https://pve.example.net:8006`
2. **Datacenter → Permissions → API Tokens → Add**
3. User: `root@pam`  (the only user with `Sys.Modify` by default)
4. Token ID: `tf-bootstrap` (the prefix becomes the `USER@REALM!TOKENID`
   part of the env value)
5. Privilege Separation: **unchecked** (Phase 0 needs the full set of
   `Sys.Modify` powers to create a new role + user + token)
6. Click **Add**. PVE shows the token secret exactly once — copy both
   the `Token ID` and the `Secret` into `.env`:

   ```bash
   PROXMOX_API_URL=https://pve.example.net:8006/api2/json
   PROXMOX_API_TOKEN=root@pam!tf-bootstrap=<paste-secret-here>
   ```

After Phase 0 completes, you can **delete this bootstrap token** from
the PVE UI — the per-terraform scoped token in
`infra/tokens/output.json` is what Phase 2 uses, and it has only the
20-priv `k3s-cluster` role (least privilege).

## 3. Cloudflare account + tokens

The pipeline mints a scoped child API token (for k3s-ingress to use
at runtime) via `infra/tokens/cloudflare.tf`. The minting path
**requires user-level auth** (Global API Key + email); a `cfat_*` admin
token cannot mint child tokens.

### 3.1 — Domain + zone

1. Cloudflare dashboard → **Add a Site** → enter `example.net` (or
   your apex) → select the Free plan.
2. Cloudflare assigns two nameservers. Add them as glue records at
   your registrar (this is the slow part; DNS won't work until you do).
3. Once the zone is **Active**, open it. The right sidebar shows
   **Account ID** — copy it as `CLOUDFLARE_ACCOUNT_ID`.
4. Scroll to the **API** block at the bottom-right of the zone
   overview — copy **Zone ID** as `CLOUDFLARE_ZONE_ID`.

### 3.2 — Global API Key + email

1. Cloudflare → **My Profile → API Tokens → Global API Key → View**
2. Copy the key as `CLOUDFLARE_GLOBAL_API_KEY`.
3. The email tied to the key is the email you log in with — copy it
   as `CLOUDFLARE_GLOBAL_API_EMAIL`.

### 3.3 — Admin API token (`CLOUDFLARE_TOKEN_CREATOR`)

The Global API Key minting is what Phase 0 uses to create the scoped
child token. `CLOUDFLARE_TOKEN_CREATOR` is an admin token that can
also mint child tokens (belt and suspenders; Phase 0 prefers the
Global Key path but falls back to this if needed).

1. Cloudflare → **My Profile → API Tokens → Create Token**
2. Template: **Edit Cloudflare Workers** (or start from **Create
   Custom Token** with `Account.Settings: Edit`)
3. Account Resources: include your account
4. Click **Continue to summary → Create Token**
5. Copy the token as `CLOUDFLARE_TOKEN_CREATOR`.

## 4. GitLab personal access token (`GITLAB_PAT`)

The OpenTofu state backend is the **GitLab HTTP backend** (not local,
not S3). Each cluster root has `backend "http" {}`; `init` requires
`GITLAB_PAT` to authenticate.

You need either:

- A self-hosted GitLab with a project to hold the state
  (e.g. `gitlab.example.net/infra-state/proxmox-k8s-cicd`), or
- A `gitlab.com` project you own (free tier works).

Steps:

1. Create a project (e.g. `infra-state/proxmox-k8s-cicd`).
2. GitLab → **User Settings → Access Tokens → Add new token**
3. Name: `proxmox-k8s-cicd-tofu-state`
4. Scopes: **`api`** + **`write_repository`** (the project API
   needs both to create + lock the state files)
5. Expiration: 1 year is fine; rotate before expiry.
6. Copy the token as `GITLAB_PAT`.

Then update `scripts/gitlab_backend.sh` to point at your project:

```bash
# scripts/gitlab_backend.sh — replace the GHE_HOST / PROJECT / STATE_PREFIX
# values to match your GitLab project. Default values assume
# gitlab.com/infra-state/proxmox-k8s-cicd.
```

If you prefer a different backend (S3, local, Terraform Cloud),
replace the `backend "http" {}` blocks in
`infra/tokens/main.tf`, `infra/clusters/cicd/main.tf`, and
`infra/clusters/apps/main.tf` and update
`scripts/apply_tofu.py` accordingly.

## 5. PowerDNS authoritative server (`POWERDNS_API_KEY`)

The cluster's internal DNS lives in a **PowerDNS** server
(defaults to LXC 101 at `10.0.0.3:8081`). The post-apply
`scripts/sync_dns_to_sdn.py` connects to it via the operator host
(using an SSH tunnel that `scripts/apply_tofu.py` opens for the
duration of the apply) to PATCH A + PTR records with the IPs the
SDN DHCP actually assigned.

You can run PowerDNS on:

- An LXC container on the same PVE host (simplest, see the reference
  setup below), or
- Any other host reachable from `pve.example.net`.

Reference LXC setup:

```bash
# On the PVE host
pveam update
pveam download local bullseye-bookworm-standard_12.2-1_amd64.tar.zst
pct create 101 local:vztmpl/bullseye-bookworm-standard_12.2-1_amd64.tar.zst \
  --hostname pdns --cores 2 --memory 1024 --net0 name=eth0,bridge=vnet0,ip=dhcp \
  --rootfs local-lvm:8 --features nesting=1
pct start 101
pct exec 101 -- apt-get install -y pdns-server pdns-backend-sqlite
# Generate an API key (any random 32+ char string)
API_KEY=$(openssl rand -hex 16)
pct exec 101 -- bash -c "cat > /etc/powerdns/pdns.conf <<EOF
api=yes
api-key=$API_KEY
webserver=yes
webserver-address=0.0.0.0
webserver-allow-from=0.0.0.0/0
launch=gsqlite3
gsqlite3-database=/var/lib/powerdns/pdns.sqlite
EOF"
pct exec 101 -- systemctl restart pdns
# Confirm
pct exec 101 -- curl -s http://127.0.0.1:8081/api/v1/servers | head
```

Copy the API key as `POWERDNS_API_KEY`.

Create the two forward + reverse zones the cluster needs (one-shot):

```bash
pct exec 101 -- pdnsutil create-zone intranet.local
pct exec 101 -- pdnsutil create-zone 10.in-addr.arpa
```

## 6. Operator SSH public key (`OPERATOR_SSH_PUBKEY`)

The build (`tools.build_image`) uses SSH to talk to PVE — it never
hits the API. The PVE host must authorize your public key in
`/root/.ssh/authorized_keys` (PVE's standard SSH key upload UI is
fine: **Datacenter → Users → root → SSH Keys → Add**).

The build then writes the public key into the template's cloud-init
`sshkeys` so every cluster clone also gets it authorized (no
per-VM key bootstrapping needed).

```bash
# If you don't have a key yet
ssh-keygen -t ed25519 -f ~/.ssh/pve.example.net -N ""
# Add it to PVE
ssh-copy-id -p 6022 root@pve.example.net
# Then point the build at it
export OPERATOR_SSH_PUBKEY=~/.ssh/pve.example.net.pub
```

## 7. Operator host packages

The `tools/build_image` and `scripts/apply_tofu.py` entry points run
on the **operator host** (your laptop or workstation), not on PVE.
They shell out to:

- `python` (>= 3.11, per `mypy.ini`)
- `tofu` (>= 1.6.0; install via the OpenTofu install script or your
  package manager)
- `qm`, `pvesh`, `pvesm`, `qemu-img`, `virt-customize` (all run on
  PVE; the operator host does **NOT** need any of them — the
  wrappers SSH into PVE for every `qm` call)
- `ssh` (standard OpenSSH client, with `SSH_AUTH_SOCK` if you use
  Bitwarden's SSH agent)
- `git` (for the GitLab state backend)
- `uv` (optional, for `uv run scripts/<name>.py` per the inline PEP
  723 metadata in `scripts/pyproject.toml`)

The simplest operator-host setup:

```bash
# Arch
sudo pacman -S python tofu openssh git uv
# Debian / Ubuntu
sudo apt-get install -y python3 python3-venv python3-pip \
                       git openssh-client curl
# Install tofu
curl -fsSL https://apt.releases.hashicorp.com/gpg | \
  sudo gpg --dearmor -o /usr/share/keyrings/hashicorp.gpg
echo "deb [signed-by=/usr/share/keyrings/hashicorp.gpg] \
  https://apt.releases.hashicorp.com $(lsb_release -cs) main" | \
  sudo tee /etc/apt/sources.list.d/hashicorp.list
sudo apt-get update && sudo apt-get install -y tofu
```

Python deps are pinned in `tools/` and don't need a `pip install`
(the operator host runs `python -m tools.build_image` from the repo
root and `python` finds them via `sys.path.insert` in
`tools/build_image/__init__.py`).

## 8. Putting it together

Once you have all 10 keys + the operator SSH pubkey:

```bash
cd /home/you/proxmox-k8s-cicd
cat > .env <<'EOF'
PROXMOX_API_URL=https://pve.example.net:8006/api2/json
PROXMOX_API_TOKEN=root@pam!tf-bootstrap=<uuid>
CLOUDFLARE_TOKEN_CREATOR=<cfat_...>
CLOUDFLARE_ACCOUNT_ID=<uuid>
CLOUDFLARE_DOMAIN=example.net
CLOUDFLARE_GLOBAL_API_KEY=<key>
CLOUDFLARE_GLOBAL_API_EMAIL=<email>
CLOUDFLARE_ZONE_ID=<uuid>
GITLAB_PAT=<glpat-...>
POWERDNS_API_KEY=<key>
EOF
chmod 600 .env
export OPERATOR_SSH_PUBKEY=~/.ssh/pve.example.net.pub
```

Sanity-check the wiring (does SSH to PVE work, can the operator
host reach the PVE API, etc.) by running the skill's
**Step 0a — Pre-flight discovery** probes (see
[`.agents/skills/proxmox-k3s-pipeline/SKILL.md`](../.agents/skills/proxmox-k3s-pipeline/SKILL.md)
Step 0a.1 through 0a.9). Each probe is a single bash snippet and
returns success in < 5 s.

If all 9 probes pass, you can run `python scripts/apply_tofu.py tokens`
to start Phase 0.

## 9. Security checklist

- `.env` is **gitignored** — never `git add` it. Verify with
  `git check-ignore -v .env` (should print `.gitignore:line:col:.env`).
- The bootstrap Proxmox token has `Sys.Modify`; rotate it after
  Phase 0 (or delete it if you don't intend to re-run Phase 0).
- `infra/tokens/output.json` is written mode 0600 and contains the
  per-terraform scoped Cloudflare + Proxmox tokens; gitignored.
- All audit logs in `logs/` are redacted via
  `tools.lib.log.StructuredLogger` (keys named `secret`, `token`,
  `password` are replaced with `***REDACTED***` before write).
- See [`docs/runbooks/rotate-tokens.md`](runbooks/rotate-tokens.md)
  for the per-token rotation procedure.
