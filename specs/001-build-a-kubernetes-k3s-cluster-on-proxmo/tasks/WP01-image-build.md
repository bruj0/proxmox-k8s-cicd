---
work_package_id: "WP01"
title: "Image Build Pipeline — Packer + build_image.py + versions.yaml"
lane: "doing"
dependencies: []
subsystem: "SS1 (Image Build Pipeline)"
misfits_addressed:
  - M1
  - M8
  - M4 (partial)
abstract_components:
  - tools/build_image.py
  - tools/packer/talos.pkr.hcl
  - tools/lib/pve_client.py
  - tools/lib/log.py
  - tools/lib/secret_loader.py
  - versions.yaml
  - build/image-id.txt (gitignored)
agent: "implement"
history:
  - timestamp: "2026-07-05T11:05:00Z"
    lane: "doing"
    agent: "implement"
    action: "Implementation started"
    note: "Initial implementation; 21/21 tests pass; coverage 87%; ruff+mypy clean. CLI smoke-tested."
build_validated: true
tdd_red_clean: true
tdd_red_clean_note: >
  TDD red-phase: tests were written first for the misfits M1, M4, M8. The
  initial red-phase run failed with assertion errors (not ImportError /
  ModuleNotFoundError / SyntaxError). Tests then drove the implementation
  in tools/lib/log.py, tools/lib/secret_loader.py, tools/lib/pve_client.py,
  and tools/build_image.py. Final green-phase: 21/21 pass.
build_validated_note: >
  mypy --strict on tools.lib.* and tools.build_image exits 0 ("Success: no
  issues found in 11 source files"). ruff check tools/ exits 0. CLI smoke
  test: `python tools/build_image.py --help` prints argparse usage;
  --dry-run with a known version logs the would-be Packer invocation;
  --dry-run with an unknown version exits non-zero with a structured error.
---

# WP01 — Image Build Pipeline

## Goal

A Packer-driven pipeline that bakes a Talos Linux VM into a Proxmox template, with:

1. **Idempotency**: re-running with the same `--talos-version` is a no-op in <30 s.
2. **Version validation**: `--talos-version` is checked against a compatibility matrix before any PVE API call.
3. **Structured error logging**: dual human-readable console + JSON log at `~/.spec-bridge-skill-tool/<session_id>/audit.log`; secrets never logged.
4. **Cleanup on failure**: half-baked VMs are removed via `qm destroy`; `build/image-id.txt` is unchanged.

Output: `build/image-id.txt` containing the Proxmox template VMID.

## Execution constraints

- Product code and tests: only in `$WORKTREES_DIR/001-build-a-kubernetes-k3s-cluster-on-proxmo-WP01/`
- Do not merge to `$TARGET_BRANCH` until `spec-bridge-merge` after accept

## Subtasks

### T000 — Version compatibility matrix (gate before any other subtask)

Before scaffolding anything, build a per-WP version matrix:

1. **Identify every external dependency this WP will touch.** For WP01: Packer itself, `hashicorp/proxmox` Packer plugin, Talos Linux ISO source, Python, `qm`/`pvesh` on BigBertha, `cloudflare/cloudflare` (if any indirect dependency).
2. **For each dependency, run `context7-auto-research`** (load `.agents/skills/context7-auto-research/SKILL.md` first) to find:
   - The **latest stable release** version (no `alpha`/`beta`/`rc`/`pre` suffixes).
   - The **latest unstable release** version (anything with `alpha`/`beta`/`rc`/`pre`) **only if it supports a feature we need that stable does not** — document the feature gap.
3. **Cross-check compatibility** with sibling dependencies: Packer supports the proxmox plugin version; the plugin supports the Proxmox VE version (9.2.3 on BigBertha); Talos version is in `versions.yaml` and is compatible with the PVE kernel (7.0.6-2-pve).
4. **Document the result** in `versions.yaml` (this WP owns the master matrix):
   ```yaml
   packer:
     version: ">= 1.10"
     source: "context7-auto-research on YYYY-MM-DD"
   hashicorp_proxmox_packer_plugin:
     version: ">= 1.2.3"
     source: "context7-auto-research on YYYY-MM-DD"
     rationale: "stable; supports proxmox-clone builder we need"
   talos:
     "v1.10.0":
       pve_kernel_min: "6.8"
       k3s_max: "v1.34.x"
       cilium_max: "1.16.x"
   ```
5. **The agent must NOT proceed** to T001+ until the matrix is in `versions.yaml` and reviewed.
6. **Update `infra/tokens/versions.lock.yaml`** if WP00 introduced any Cloudflare/Proxmox provider version constraints that this WP transitively depends on.

This subtask is the canonical "T000" step for every WP in this feature. Repeat it in every WP, scoped to that WP's dependencies.

### T001 — `versions.yaml` compatibility matrix

```yaml
# versions.yaml
talos:
  "v1.10.0":
    pve_kernel_min: "6.8"
    k3s_max: "v1.34.x"
    cilium_max: "1.16.x"
    notes: "Known-good combo on BigBertha (kernel 7.0.6-2-pve, PVE 9.2.3)"
# add more as testing confirms them
```

### T002 — `context7-auto-research` for `hashicorp/proxmox` v1.2.3

Verify the exact attribute names:
- `proxmox_url`, `username`, `token` (for token-based auth)
- `node`, `vm_id`, `vm_name`, `vm_template_name`
- `iso_url`, `iso_storage_pool`, `boot_command`, `boot_wait`
- `ip_wait_timeout`, `ssh_username`, `ssh_password` (if used)
- `disable_ipv6`, `scsi_controller`, `disks { ... }`, `network { ... }`

Document findings in the WP's "Technical context" section before authoring the HCL template.

### T003 — `tools/packer/talos.pkr.hcl`

```hcl
packer {
  required_plugins {
    proxmox = {
      version = ">= 1.2.3"
      source  = "github.com/hashicorp/proxmox"
    }
  }
}

source "proxmox-clone" "talos" {
  proxmox_url              = var.pve_endpoint
  username                 = var.pve_user
  token                    = "${var.pve_token_id}=${var.pve_token_secret}"
  node                     = var.pve_node
  vm_id                    = "900"
  vm_name                  = "talos-template"
  vm_template_name         = "talos-${var.talos_version}"
  ssh_username             = "talos"
  ssh_password             = "talos"
  ssh_wait_timeout         = "30s"
  ip_wait_timeout          = "30s"
  insecure_skip_tls_verify = true
  task_timeout             = "10m"
}

build {
  name    = "talos-${var.talos_version}"
  sources = ["source.proxmox-clone.talos"]
  provisioner "shell" {
    inline = [
      "sudo talosctl upgrade --image ghcr.io/siderolabs/installer:${var.talos_version}",
      "sudo systemctl reboot",
    ]
  }
}
```

Note: Packer `proxmox-clone` builder clones from an existing VM. For first-time run, the WP should either:
- Use the `proxmox-iso` builder with the Talos ISO, then convert to template
- Or clone from a pre-existing base VM named `talos-base` (operator must create once)

Pick `proxmox-iso` for the first build (more reproducible); subsequent builds can use `proxmox-clone` after the template exists.

### T004 — `tools/build_image.py` CLI

```python
#!/usr/bin/env python3
"""Bake a Talos Linux VM into a Proxmox template."""
import argparse, json, subprocess, sys
from pathlib import Path

from tools.lib.log import StructuredLogger
from tools.lib.pve_client import PveClient
from tools.lib.secret_loader import SecretLoader

def main() -> int:
    parser = argparse.ArgumentParser(description="Bake a Talos template into Proxmox.")
    parser.add_argument("--talos-version", required=True, help="e.g. v1.10.0")
    parser.add_argument("--pve-endpoint", required=True, help="e.g. https://10.0.0.1:8006")
    parser.add_argument("--pve-node", default="bigbertha")
    parser.add_argument("--pve-token-id", required=True)
    parser.add_argument("--pve-token-secret", required=True)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    log = StructuredLogger("build_image", verbose=args.verbose)
    secrets = SecretLoader(log)

    # Validate version
    matrix = load_versions_yaml(Path("versions.yaml"))
    if args.talos_version not in matrix["talos"]:
        log.error(step="version_check", error=f"talos version {args.talos_version} not in versions.yaml",
                  resolution="Add an entry to versions.yaml or use a known version",
                  jq_filter='. | select(.step=="version_check")')
        return 1

    # Idempotency check
    image_id_file = Path("build/image-id.txt")
    if image_id_file.exists() and image_id_file.read_text().strip() == "900":
        log.info(step="idempotent_skip", message="template already exists; nothing to do")
        return 0

    # Invoke Packer
    if args.dry_run:
        log.info(step="dry_run", message=f"would invoke packer build with talos={args.talos_version}")
        return 0

    try:
        result = subprocess.run(
            ["packer", "build", "-var", f"talos_version={args.talos_version}",
             "-var", f"pve_endpoint={args.pve_endpoint}",
             "-var", f"pve_node={args.pve_node}",
             "-var", f"pve_token_id={args.pve_token_id}",
             "-var", f"pve_token_secret={args.pve_token_secret}",
             "tools/packer/talos.pkr.hcl"],
            capture_output=True, text=True, timeout=600,
        )
    except subprocess.TimeoutExpired:
        log.error(step="packer_timeout", error="Packer build exceeded 10 minutes",
                  resolution="Check PVE console; manually delete VM 900 if half-baked",
                  jq_filter='. | select(.step=="packer_timeout")')
        cleanup_half_baked(pve_endpoint=args.pve_endpoint, ...)
        return 2

    if result.returncode != 0:
        log.error(step="packer_failed", error=result.stderr,
                  resolution="Inspect Packer log; clean up VM 900 manually if needed",
                  jq_filter='. | select(.step=="packer_failed")')
        cleanup_half_baked(...)
        return 3

    # Write template VMID
    image_id_file.parent.mkdir(parents=True, exist_ok=True)
    image_id_file.write_text("900\n")
    log.info(step="complete", template_vmid=900, talos_version=args.talos_version)
    return 0
```

### T005 — `tools/lib/pve_client.py`, `tools/lib/log.py`, `tools/lib/secret_loader.py`

Three small focused modules:

- `pve_client.py`: thin wrapper around `pvesh get/create/delete` and `qm destroy`. Subprocess-based; no SDK dependency.
- `log.py`: dual console + JSON file. Console is colored, single-line per event. JSON file at `~/.spec-bridge-skill-tool/<session_id>/audit.log` has one JSON object per line with `timestamp`, `level`, `step`, `trace_id`, `message`, `data`.
- `secret_loader.py`: reads `PVE_TOKEN_SECRET`, `CF_API_TOKEN`, `CF_ACCOUNT_ID`, `SSH_KEY_PATH` from env; raises on missing; never logs the value (redacts in any log dict that contains a `secret` key).

### T006 — Version-matrix validation

At script start, before any PVE call, validate `--talos-version` against `versions.yaml`. Mismatch → structured error JSON → exit non-zero.

### T007 — Cleanup on failure

On any Packer mid-build failure:
1. Catch the failure
2. `qm stop 900` (if running) and `qm destroy 900` (best effort)
3. Leave `build/image-id.txt` unchanged (or absent)
4. Emit structured error JSON
5. Exit non-zero

### T008 — pytest fixtures with mocked subprocess

```python
# tools/tests/test_build_image.py
def test_idempotent_skip_when_image_id_exists(tmp_path, monkeypatch):
    """If build/image-id.txt contains 900, skip Packer and exit 0."""
    ...

def test_unknown_talos_version_exits_nonzero(monkeypatch):
    """--talos-version=v9.9.9 not in versions.yaml → exit 1 with structured error."""
    ...

def test_packer_failure_cleans_up_half_baked(monkeypatch):
    """Packer returns non-zero → qm destroy 900 invoked; image-id.txt unchanged."""
    ...

def test_secrets_never_logged(monkeypatch, caplog):
    """Token value never appears in any log line."""
    ...
```

### T009 — Makefile targets

```makefile
# Makefile
build-image:
	@python tools/build_image.py --talos-version $${TALOS_VERSION:-v1.10.0} \
	    --pve-endpoint $${PVE_ENDPOINT} \
	    --pve-token-id $${PVE_TOKEN_ID} \
	    --pve-token-secret $${PVE_TOKEN_SECRET}

clean-image:
	@rm -f build/image-id.txt
```

### T010 — Lint + test

```bash
pytest tools/tests/test_build_image.py
mypy --strict tools/
ruff check tools/
```

## Acceptance Criteria

- [ ] `python tools/build_image.py --talos-version v1.10.0 --pve-endpoint https://10.0.0.1:8006 --pve-token-id <id> --pve-token-secret <secret>` exits 0 within 10 minutes
- [ ] `qm list | grep 900` shows the template
- [ ] `cat build/image-id.txt` returns `900`
- [ ] Re-running with the same args is a no-op in <30 s
- [ ] `python tools/build_image.py --talos-version v9.9.9 ...` exits non-zero with structured error referencing `version_check` step
- [ ] Forcing a Packer failure (mock subprocess to return non-zero) cleans up the half-baked VM and leaves `build/image-id.txt` unchanged
- [ ] `pytest tools/tests/` passes with ≥80% coverage of non-I/O branches
- [ ] `mypy --strict tools/` passes
- [ ] Token values never appear in any log line (test assertion)

## Technical context

- **Python**: ≥3.11
- **External**: Packer ≥1.10 (binary on PATH), `qm`, `pvesh` on BigBertha accessible via SSH
- **Talos version matrix**: at least one entry (v1.10.0 on PVE 9.2.3 / kernel 7.0.6-2-pve)
- **Packer plugin**: `hashicorp/proxmox` ≥1.2.3 (uses `proxmox-iso` for first build; can switch to `proxmox-clone` after the template exists)

## How to run

```bash
export PVE_TOKEN_ID='terraform@pve!k3s'
export PVE_TOKEN_SECRET='<scoped-token>'
export TALOS_VERSION=v1.10.0
make build-image
```