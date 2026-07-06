# Runbook — Rotate Proxmox & Cloudflare Tokens

This runbook rotates the two long-lived tokens minted by `infra/tokens`:

- `cloudflare_api_token.k3s_scoped` (Cloudflare, IP-locked to the runner)
- `proxmox_user_token.k3s_terraform_tf` (Proxmox, scoped to
  `k3s-terraform@pam`, role `k3s-cluster` with **19 privs** as of
  2026-07-06 — was 12 per spec T005; the 7 extras cover Phase 1 /
  Packer access)

Both tokens are intentionally non-expiring; rotation is operator-driven so
we can compose it with `tofu apply` to validate the new token before tearing
down the old one.

> **Privilege set matters**: the `k3s-cluster` role carries 19
> privileges (12 spec T005 + `Sys.Audit`, `VM.Audit`, `VM.Clone`,
> `VM.Migrate`, `VM.Config.CDROM`, `VM.Config.HWType`,
> `VM.Snapshot.Rollback`). If `tofu apply` in `infra/tokens/` reports
> "missing one or more spec T005 or Phase-1 privileges", you may have
> an older lock file; check `.terraform.lock.hcl` matches the latest
> priv set in `infra/tokens/proxmox.tf`. Rotation must NOT remove any
> of the 7 Phase-1 privs, or Packer / Phase 2 will start failing with
> `403 Permission check failed`.

## When to rotate

- Quarterly (every 90 days) under normal operation.
- Immediately on any of: (a) suspected secret exposure in CI logs, (b) a
  runner host change that shifts the IP-lock CIDR, (c) Proxmox admin change,
  (d) Cloudflare account compromise.

## Pre-flight

```bash
# Confirm the workspace is clean and on a tagged release.
git status            # expect: clean
git describe --tags   # expect: v0.x.y
tofu version          # expect: >= 1.7.0
```

## Rotate Cloudflare scoped token

Cloudflare API tokens are immutable — rotation means **destroy and recreate**.
The apply wrapper handles both steps in a single `tofu apply`.

```bash
cd infra/tokens
tofu apply -target=cloudflare_api_token.k3s_scoped -auto-approve
```

Verify the new value in `output.json`:

```bash
jq -r .cloudflare_scoped_token.value output.json
```

Sanity-check the new token from a non-runner host (must fail):

```bash
CLOUDFLARE_API_TOKEN="$(jq -r .cloudflare_scoped_token.value output.json)" \
  curl -sS https://api.cloudflare.com/client/v4/user/tokens/verify | jq .
# expect: success=false, errors[0].code=10000 (IP not allowed)
```

## Rotate Proxmox API token

The Proxmox provider supports in-place rotation via `expiration_date` +
token recreation. Easiest path: `-replace` on the user token resource
(its short alias in bpg/proxmox v0.111.x is `proxmox_user_token`,
NOT the legacy `proxmox_virtual_environment_user_token` from the
telmate provider).

```bash
cd infra/tokens
tofu apply \
  -replace=proxmox_user_token.k3s_terraform_tf \
  -auto-approve
```

Confirm the new value:

```bash
jq -r '{id: .proxmox_token_id, secret_len: (.proxmox_token_secret | length)}' output.json
# expect: { "id": "k3s-terraform@pam!tf", "secret_len": 36 }
```

`secret_len: 36` confirms the secret-split fix from 2026-07-06:
bpg/proxmox's `proxmox_user_token.value` attribute returns the FULL
`USER@REALM!TOKEN=secret` string, but `output_json.tf` splits on `=`
and writes only the bare UUID into `proxmox_token_secret`. If you see
`secret_len: 73` (or any value > 36), the split is broken and
downstream consumers will produce malformed PVEAuth headers.

Propagate the new secret to downstream WPs. WP02 onward read `output.json`
at apply time, so a `tofu apply` in each of `infra/cluster-cicd/` and
`infra/cluster-apps/` picks up the new value automatically.

## Post-rotation

1. Update any external secret stores (1Password, Bitwarden, GitHub Actions
   secrets) with the new values.
2. Revoke the old Proxmox token via the PVE UI (Datacenter → Permissions →
   Users → k3s-terraform → Tokens → ⋯ → Delete).
3. Open a PR with the rotated secrets **redacted** to confirm workflow.
4. Tag the release: `git tag -a v0.x.y -m "rotate tokens" && git push --tags`.

## Disaster recovery

If the rotation fails mid-flight (e.g. new token rejected by Proxmox):

```bash
cd infra/tokens
tofu apply -auto-approve  # re-creates the old token on next apply
```

The Proxmox resource graph is acyclic, so a partial apply always converges
on re-run. The Cloudflare token cannot be partially created — the API
returns the new secret only after the resource is fully provisioned, so a
failed create leaves the old token untouched.