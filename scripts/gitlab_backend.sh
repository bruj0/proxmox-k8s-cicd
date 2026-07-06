#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# GitLab-managed Terraform state migration / init helper.
#
# Project:    infra-state/bigbertha (project_id=84156476)
# Per-stack state names (4 stacks):
#   - infra-tokens                      (infra/tokens/)
#   - proxmox-k3s-cluster-module        (infra/modules/proxmox-k3s-cluster/)
#   - cluster-cicd                      (infra/clusters/cicd/)
#   - cluster-apps                      (infra/clusters/apps/)
#
# Usage:
#   GITLAB_ACCESS_TOKEN=glpat-xxx \
#     ./scripts/gitlab_backend.sh init <stack-name> [--migrate-state]
#   GITLAB_ACCESS_TOKEN=glpat-xxx \
#     ./scripts/gitlab_backend.sh unlock <stack-name>
#
# The script always prints the exact `tofu init` command it would run,
# so you can audit the -backend-config flags before execution.
# ---------------------------------------------------------------------------
set -euo pipefail

GITLAB_HOST="${GITLAB_HOST:-gitlab.com}"
GITLAB_PROJECT_ID="${GITLAB_PROJECT_ID:-84156476}"
GITLAB_USERNAME="${GITLAB_USERNAME:-bruj0}"

declare -A STACK_DIRS=(
  [infra-tokens]="infra/tokens"
  [proxmox-k3s-cluster-module]="infra/modules/proxmox-k3s-cluster"
  [cluster-cicd]="infra/clusters/cicd"
  [cluster-apps]="infra/clusters/apps"
)

usage() {
  sed -n '3,20p' "$0"
  exit 64
}

require_token() {
  if [[ -z "${GITLAB_ACCESS_TOKEN:-}" ]]; then
    echo "ERROR: GITLAB_ACCESS_TOKEN env var is required (create one at" >&2
    echo "  https://gitlab.com/-/user_settings/personal_access_tokens" >&2
    echo "  with the 'api' scope)." >&2
    exit 2
  fi
}

backend_flags_for() {
  local stack_name="$1"
  local state_url="https://${GITLAB_HOST}/api/v4/projects/${GITLAB_PROJECT_ID}/terraform/state/${stack_name}"
  printf -- '-backend-config=address=%s\n' "${state_url}"
  printf -- '-backend-config=lock_address=%s/lock\n' "${state_url}"
  printf -- '-backend-config=unlock_address=%s/lock\n' "${state_url}"
  printf -- '-backend-config=username=%s\n' "${GITLAB_USERNAME}"
  # Only print the password line if a token is set, so `show` can be
  # run safely without GITLAB_ACCESS_TOKEN.
  if [[ -n "${GITLAB_ACCESS_TOKEN:-}" ]]; then
    printf -- '-backend-config=password=%s\n' "${GITLAB_ACCESS_TOKEN}"
  fi
  printf -- '-backend-config=lock_method=POST\n'
  printf -- '-backend-config=unlock_method=DELETE\n'
  printf -- '-backend-config=retry_wait_min=5\n'
}

cmd="${1:-}"
stack="${2:-}"

if [[ -z "$cmd" || -z "$stack" ]]; then
  usage
fi

# Drop the cmd + stack positional args so $@ contains only the trailing flags.
shift 2 2>/dev/null || shift_count=2

if [[ -z "${STACK_DIRS[$stack]:-}" ]]; then
  echo "ERROR: unknown stack '$stack'. Valid: ${!STACK_DIRS[*]}" >&2
  exit 2
fi

stack_dir="${STACK_DIRS[$stack]}"

case "$cmd" in
  init)
    require_token
    mapfile -t backend_flags < <(backend_flags_for "$stack")
    extra_args=("$@")
    migrate=""
    filtered_args=()
    for a in "${extra_args[@]}"; do
      if [[ "$a" == "--migrate-state" ]]; then
        migrate="-migrate-state"
        # do NOT pass it through again
        continue
      fi
      filtered_args+=("$a")
    done
    echo ">>> tofu -chdir='${stack_dir}' init ${migrate} (with GitLab backend)"
    for f in "${backend_flags[@]}"; do
      echo "    ${f%=*}=***"   # mask the password value
    done
    # shellcheck disable=SC2086
    tofu -chdir="${stack_dir}" init ${migrate} -input=false -force-copy "${backend_flags[@]}" "${filtered_args[@]}"
    ;;
  unlock)
    require_token
    mapfile -t backend_flags < <(backend_flags_for "$stack")
    echo ">>> tofu -chdir='${stack_dir}' force-unlock (dummy)"
    tofu -chdir="${stack_dir}" init -input=false "${backend_flags[@]}" >/dev/null
    tofu -chdir="${stack_dir}" force-unlock -force "${stack}" || true
    ;;
  show)
    backend_flags_for "$stack"
    ;;
  *)
    usage
    ;;
esac