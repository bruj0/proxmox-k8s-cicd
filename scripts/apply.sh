#!/usr/bin/env bash
###############################################################################
# scripts/apply.sh — wraps `tofu apply` for infra/tokens.
#
# Responsibilities
#   1. Source ./.env so CLOUDFLARE_TOKEN_CREATOR / CLOUDFLARE_ACCOUNT_ID /
#      CLOUDFLARE_DOMAIN are available without echoing their values.
#   2. Translate env-var names into TF_VAR_* (the variable name in
#      variables.tf is cloudflare_admin_token, the env-var name is
#      CLOUDFLARE_TOKEN_CREATOR — see .env).
#   3. Run `tofu init -backend=false && tofu apply -auto-approve`.
#
# output.json is written by the `local_sensitive_file.tokens_output` resource
# declared in infra/tokens/output_json.tf. The apply wrapper no longer needs
# a manual `tofu output -json | jq` post-step — the OpenTofu plan itself
# produces the file. This is the spec T007 contract.
#
# Usage
#   scripts/apply.sh                 # full plan + apply
#   scripts/apply.sh --plan-only     # tofu plan, no apply
###############################################################################
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TF_DIR="${REPO_ROOT}/infra/tokens"

log() { printf '\033[1;34m[apply]\033[0m %s\n' "$*"; }
err() { printf '\033[1;31m[apply]\033[0m %s\n' "$*" >&2; }

# ---------------------------------------------------------------------------
# 1. Source .env (variables only — never echo values)
# ---------------------------------------------------------------------------
if [[ ! -f "${REPO_ROOT}/.env" ]]; then
  err ".env not found at ${REPO_ROOT}/.env. Create it with CLOUDFLARE_TOKEN_CREATOR / CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_DOMAIN."
  exit 2
fi

set -a
# shellcheck disable=SC1091
source "${REPO_ROOT}/.env"
set +a

# ---------------------------------------------------------------------------
# 2. Translate .env keys → TF_VAR_* names
# ---------------------------------------------------------------------------
: "${CLOUDFLARE_TOKEN_CREATOR:?CLOUDFLARE_TOKEN_CREATOR must be set in .env}"
: "${CLOUDFLARE_ACCOUNT_ID:?CLOUDFLARE_ACCOUNT_ID must be set in .env}"
: "${CLOUDFLARE_DOMAIN:?CLOUDFLARE_DOMAIN must be set in .env}"

export TF_VAR_cloudflare_admin_token="${CLOUDFLARE_TOKEN_CREATOR}"
export TF_VAR_cloudflare_account_id="${CLOUDFLARE_ACCOUNT_ID}"
# Domain is provided in .env for cross-WP use; the zone id is looked up by
# downstream WPs from the account. We do not need a zone id to mint the
# scoped token (we use account-scoped resources).

# Proxmox bootstrap token: provided via env so it never lands in HCL.
: "${PROXMOX_API_URL:?PROXMOX_API_URL must be set in .env or CI}"
: "${PROXMOX_API_TOKEN:?PROXMOX_API_TOKEN must be USER@REALM!TOK=secret form}"
export TF_VAR_proxmox_api_url="${PROXMOX_API_URL}"
export TF_VAR_proxmox_endpoint="${PROXMOX_API_URL}"

# PROXMOX_API_TOKEN is the canonical env-var read by the bpg/proxmox
# provider itself, so we forward it unchanged. We also split it into the
# TF_VAR_proxmox_api_token_id / _secret pair so variables.tf stays in sync.
_proxmox_id="${PROXMOX_API_TOKEN%%=*}"
_proxmox_secret="${PROXMOX_API_TOKEN#*=}"
export TF_VAR_proxmox_api_token_id="${_proxmox_id}"
export TF_VAR_proxmox_api_token_secret="${_proxmox_secret}"

# ---------------------------------------------------------------------------
# 3. tofu init / apply
# ---------------------------------------------------------------------------
cd "${TF_DIR}"

if ! command -v tofu >/dev/null 2>&1; then
  err "tofu (OpenTofu) is not on PATH. Install OpenTofu >= 1.6.0 first."
  exit 3
fi

log "tofu init -backend=false"
tofu init -backend=false -input=false -no-color

if [[ "${1:-}" == "--plan-only" ]]; then
  log "tofu plan (no apply)"
  tofu plan -input=false -no-color
  exit 0
fi

log "tofu apply -auto-approve"
tofu apply -auto-approve -input=false -no-color

# ---------------------------------------------------------------------------
# 4. Confirm output.json was written by the local_sensitive_file resource.
# ---------------------------------------------------------------------------
OUTPUT_FILE="${TF_DIR}/output.json"
if [[ -f "${OUTPUT_FILE}" ]]; then
  log "output.json written to ${OUTPUT_FILE} (mode 0600)"
  log "Next: run tofu test in ${TF_DIR} to validate the rotation is a no-op."
else
  err "output.json missing after apply — check tofu output for errors"
  exit 4
fi