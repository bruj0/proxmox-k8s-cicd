---
context_name: "Token Provisioning"
version: "1.1"
subsystem: "infra/tokens"
created: "2026-07-05T15:50:00Z"
updated: "2026-07-06T14:25:00Z"
---

# Token Provisioning

The cross-cutting infrastructure subsystem that mints and owns the two least-privilege API credentials the cluster pipeline consumes: a scoped Cloudflare API token and a Proxmox role+user+token pair. Idempotent OpenTofu root at `infra/tokens/`.

## Language

**Cloudflare Admin Token**:
A break-glass Cloudflare API token generated once in the Cloudflare dashboard, used only by `infra/tokens/` to mint the scoped token. Never written to state, never committed.
_Avoid_: `admin_cloudflare_token`, `cf_root_token`, `CLOUDFLARE_API_TOKEN` (env var name is reserved for the admin token when running `tofu apply`).
_Subsystems_: SS0
_Files_: `infra/tokens/main.tf`, `infra/tokens/variables.tf`
_Relates to_: Scoped Cloudflare Token (mints)
_History_:
- 2026-07-05 (001-build-a-kubernetes-k3s-cluster-on-proxmo): initial definition.

**Scoped Cloudflare Token**:
A Cloudflare API token containing exactly three permission groups — `zone:read`, `dns:edit`, and `account:cloudflare-tunnel:edit` — used by the STRRL/cloudflare-tunnel-ingress-controller and any DNS automation downstream. Minted by `infra/tokens/` from the admin token; written to `output.json`.
_Avoid_: `cloudflare_api_token` (too generic — must specify scoped vs admin).
_Subsystems_: SS0, SS2 (consumed via `output.json`)
_Files_: `infra/tokens/main.tf`, `infra/tokens/output.json`
_Relates to_: Cloudflare Admin Token (minted from)
_History_:
- 2026-07-05 (001-build-a-kubernetes-k3s-cluster-on-proxmo): initial definition.

**Proxmox Role**:
A PVE role (`k3s-cluster`) bundling the privilege set bpg/proxmox + sergelogvinov/proxmox-csi-plugin + hashicorp/proxmox (Packer) require to provision VMs, attach disks, configure SDN, use datastores, and (for Phase 1) bake a Talos template. Created once and intentionally retained across `tofu destroy` so re-applies are idempotent.
_Avoid_: `pve_role`, `cluster_role` (ambiguous with k8s RBAC).
_Subsystems_: SS0
_Files_: `infra/tokens/proxmox.tf`
_Relates to_: Proxmox User (assigned to), Proxmox Token (inherits from)
_History_:
- 2026-07-05 (001-build-a-kubernetes-k3s-cluster-on-proxmo): initial definition (12 privs per spec T005).
- 2026-07-06: extended to **19 privs** on the live BigBertha host. The 12 spec-T005 privs were sufficient for SS2 (bpg/proxmox) but lacked the privs Packer (`hashicorp/proxmox` proxmox-clone v1.2.3) needs to clone VMID 999 → 900 and convert it to a template. Seven privs were added: `Sys.Audit` (for `/access` namespace reads), `VM.Audit` (read VM 999 cfg / qemu list), `VM.Clone`, `VM.Migrate` (failed-template cleanup), `VM.Config.CDROM` (Talos ISO attach/detach), `VM.Config.HWType` (set `machine=q35` for UEFI), `VM.Snapshot.Rollback` (rollback on template-bake failure). Pinned by `infra/tokens/tests/main.tftest.hcl::proxmox_role_has_spec_t005_privileges` which asserts `length(...) == 19`.

**Proxmox User**:
A PVE user (`k3s-terraform@pam`) bound to the Proxmox Role. Comment and email identify it as automation-owned.
_Avoid_: `pve_user`, `terraform_user` (ambiguous with other tooling users on the host).
_Subsystems_: SS0
_Files_: `infra/tokens/main.tf`
_Relates to_: Proxmox Role (assigned to), Proxmox Token (owned by)
_History_:
- 2026-07-05 (001-build-a-kubernetes-k3s-cluster-on-proxmo): initial definition.

**Proxmox Token**:
A PVE API token (`<user>!<token_name>`) whose privileges are inherited from the Proxmox Role. Secret value lives in `output.json`; downstream WPs consume via env.
_Avoid_: `pve_token`, `proxmox_api_token`.
_Subsystems_: SS0, SS2 (consumed via `output.json`)
_Files_: `infra/tokens/main.tf`, `infra/tokens/output.json`
_Relates to_: Proxmox User (owned by), Proxmox Role (inherits from)
_History_:
- 2026-07-05 (001-build-a-kubernetes-k3s-cluster-on-proxmo): initial definition.

**Token Output File**:
The gitignored `infra/tokens/output.json` containing `cloudflare_scoped_token`, `cloudflare_account_id`, `cloudflare_zone_id`, `proxmox_token_id`, `proxmox_token_secret`, `pve_endpoint`. Written with `file_permission = "0600"` via the `local_sensitive_file` resource.
_Avoid_: `tokens.json`, `secrets.json`.
_Subsystems_: SS0 (producer), SS2/SS3 (consumers)
_Files_: `infra/tokens/output.json`
_Relates to_: Scoped Cloudflare Token (writes), Proxmox Token (writes)
_History_:
- 2026-07-05 (001-build-a-kubernetes-k3s-cluster-on-proxmo): initial definition.
- 2026-07-06: fixed `proxmox_token_secret` schema. bpg/proxmox v0.111.x's `proxmox_user_token.<>.value` attribute returns the FULL api-token string in `USER@REALM!TOKENID=secret` form, not just the bare secret. The original `output_json.tf` wrote that whole value into `proxmox_token_secret`, which broke downstream consumers (the cluster roots) that concatenate `${proxmox_token_id}=${proxmox_token_secret}` to build the PVEAuth header. `output_json.tf` now exposes a `locals {}` block that splits the value on `=` and writes only `[1]` (the bare UUID) into `proxmox_token_secret`. Verified 2026-07-06: `output.json.proxmox_token_secret` is a 36-char UUID. Pinned by `tools/tests/test_agent_skill.py::test_skill_documents_output_json_secret_split`.

**Versions Lock File**:
The `versions.lock.yaml` at `infra/tokens/` capturing the exact provider and toolchain versions used (cloudflare/cloudflare, bpg/proxmox, OpenTofu, Python). Generated by the T000 subtask before any other work.
_Avoid_: `versions.yaml` (that name belongs at the repo root per WP01).
_Subsystems_: SS0
_Files_: `infra/tokens/versions.lock.yaml`
_Relates to_: Provider Resource (constrains)
_History_:
- 2026-07-05 (001-build-a-kubernetes-k3s-cluster-on-proxmo): initial definition.

## Relationships

- A **Cloudflare Admin Token** mints one **Scoped Cloudflare Token**.
- A **Proxmox User** is assigned to exactly one **Proxmox Role**.
- A **Proxmox Token** is owned by one **Proxmox User** and inherits its privileges.
- The **Token Output File** contains one **Scoped Cloudflare Token** and one **Proxmox Token**.
- The **Versions Lock File** constrains the OpenTofu provider versions used to mint tokens.

## Flagged Ambiguities

- "API token" was used generically in research logs — resolved: use **Cloudflare Admin Token** (break-glass) vs **Scoped Cloudflare Token** (pipeline runtime) to disambiguate.
- "Proxmox credentials" was used generically — resolved: use **Proxmox User**, **Proxmox Role**, **Proxmox Token** as the three distinct concepts.