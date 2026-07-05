# Runbook — Rotate Proxmox & Cloudflare Tokens

This runbook rotates the two long-lived tokens minted by `infra/tokens`:

- `cloudflare_api_token.k3s_scoped` (Cloudflare, IP-locked to the runner)
- `proxmox_virtual_environment_user_token.k3s_terraform_tf` (Proxmox, scoped to `k3s-terraform@pam`)

Both tokens are intentionally non-expiring; rotation is operator-driven so
we can compose it with `tofu apply` to validate the new token before tearing
down the old one.

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
token recreation. Easiest path: taint and re-apply.

```bash
cd infra/tokens
tofu apply \
  -replace=proxmox_virtual_environment_user_token.k3s_terraform_tf \
  -auto-approve
```

Confirm the new value:

```bash
jq -r .proxmox_token_value.value output.json
```

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