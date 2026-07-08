# AGENTS.md — guide for AI agents modifying this repo

This repository provisions two k3s clusters on a single Proxmox VE host.
It is driven by an [agentskills.io](https://agentskills.io)-format
Agent Skill at
[`.agents/skills/proxmox-k3s-pipeline/SKILL.md`](.agents/skills/proxmox-k3s-pipeline/SKILL.md).
If you are an AI agent making modifications, you should:

1. **Read the skill first** — it is the authoritative playbook.
2. **Read this file** — it covers the conventions that don't fit in the
   skill (commit hygiene, test patterns, common pitfalls).
3. **Read the relevant `docs/` entry** — `architecture.md` for
   subsystem boundaries, `verification.md` for the success criteria,
   `cluster-instances.md` for the seven-element uniqueness contract
   that every new cluster must satisfy.

## Canonical vocabulary

The bounded context for this repo is in
[`.agents/skills/proxmox-k3s-pipeline/CONTEXT.md`](.agents/skills/proxmox-k3s-pipeline/CONTEXT.md).
The terms you'll see most often:

- **Agent Skill** — the `.agents/skills/proxmox-k3s-pipeline/SKILL.md`
  file that Claude Code / Cursor loads as the playbook.
- **Operator** — the human or AI agent invoking the skill.
- **Pipeline** — the 5-top-level-phase end-to-end sequence
  (Phase 0 tokens -> Phase 1 image -> Phase 2 clusters -> Phase 3
  baseline -> Phase 4 bootstrap -> Phase 5 verification).
- **Phase 4 sub-phases** — `cloudinit, k3s, helm, kubeconfig, host_ports,
  externalname` (renamed from `talos, k3s, ...` when the OS pivot
  landed 2026-07-07).
- **Runbook** — a single-concern copy-pasteable procedure under
  `docs/runbooks/`. Runbooks do not require an Agent; the operator
  follows them directly.
- **SS0/SS1/SS2/SS3** — the four subsystems in the spec, owned by
  specific phases (SS0=tokens, SS1=image build, SS2=cluster
  provisioning, SS3=bootstrap). See `docs/architecture.md` for the
  subsystem boundary table.

## Repository conventions

### File layout

- `tools/` — Python automation (`tools.build_image`, `tools.bootstrap_cluster`,
  `tools.lib.pve_client`, `tools.lib.helm_client`, `tools.lib.secret_loader`,
  `tools.lib.log`). Strict mypy + ruff.
- `infra/` — OpenTofu modules. State is in the GitLab HTTP backend
  (see `docs/runbooks/gitlab-state-backend.md` and
  `scripts/gitlab_backend.sh`).
- `scripts/` — operational scripts that wrap tofu + Python
  (`apply_tofu.py`, `sync_dns_to_sdn.py`, `capture_host_ports_baseline.sh`,
  `capture_serial.py` for debug).
- `docs/` — human-facing documentation (`architecture.md`, `verification.md`,
  `cluster-instances.md`, `runbooks/`, `proxmox-serial-capture.md`).
- `.agents/skills/proxmox-k3s-pipeline/` — the Agent Skill. The
  YAML frontmatter + body are the contract; pin every change in
  `versions.lock.yaml::cross_check`.
- `specs/` — spec-bridge planning artefacts. Read-only after
  implementation.
- `build/` — generated (image-id.txt, etc.). Gitignored.

### Python style

- `python -m ruff check tools/` — must pass.
- `mypy --strict --explicit-package-bases` on `tools/` — must pass
  (see `mypy.ini` for the per-module overrides; tests are excluded).
- Tests live in `tools/tests/`. 92 tests as of 2026-07-07.
- All CLI entry points expose `main()` returning `int` exit code.
- The audit log is JSONL via `tools.lib.log.StructuredLogger`; every
  log line has a `step=` and a `message=` (for `warn`) / `error=`+`resolution=`
  (for `error`).

### OpenTofu style

- `tofu test` must pass for `infra/tokens/`, every module under
  `infra/modules/`, and every instance under `infra/clusters/`.
- State is in the GitLab HTTP backend; **never** `tofu init` against
  local state. Use `scripts/gitlab_backend.sh init <stack>`.
- Module variables are camelCase. The cluster module
  (`infra/modules/proxmox-k3s-cluster`) is the only reusable module;
  the cluster roots (`infra/clusters/{cicd,apps}/`) are thin
  per-cluster instantiations.
- PowerDNS records use the bpg `pan-net/powerdns` provider; records
  are short-circuited when `var.powerdns_api_key` is empty (so
  `tofu test` passes without secrets).

### Security

- **Never commit `.env`**, `terraform.tfvars`, `output.json`, or
  `*.tfstate*` (all gitignored, but double-check before `git add -A`).
- The PVE token used by the build is **separate** from the one
  minted by `infra/tokens/` for tofu; the build uses SSH only and
  doesn't actually need `PVE_TOKEN_ID` / `PVE_TOKEN_SECRET` (kept
  in the dataclass for parity with `apply_tofu.py`).
- `audit_log` entries redact keys named `secret`, `token`, `password`
  automatically via `StructuredLogger`'s redactor (see
  `tools/lib/log.py`).

## How to add a cluster

The `docs/cluster-instances.md` runbook walks through the seven-element
uniqueness contract that every new cluster must satisfy
(`cluster_name`, `vip`, `vmid_start`, `ip_start` (must be a fresh `/24`),
`pod_cidr`, `svc_cidr`, `cf_tunnel_name`). Read it first.

## How to modify the canonical recipe

If you're changing the build flow (Phase 1), the cluster module
(Phase 2), or the bootstrap (Phase 4):

1. **Edit the code** (`tools/build_image/__init__.py`,
   `infra/modules/proxmox-k3s-cluster/*.tf`, or
   `tools/bootstrap_cluster.py`).
2. **Update the test that pins the contract.**
   `tools/tests/test_build_image.py`,
   `tools/tests/test_bootstrap_cluster.py`, or
   `tools/tests/test_agent_skill.py` (which cross-checks the SKILL.md
   text against the actual recipe).
3. **Update the skill** (`.agents/skills/proxmox-k3s-pipeline/SKILL.md`)
   to match. The skill is the playbook; the test pins the playbook.
4. **Add a `cross_check` entry in `versions.lock.yaml`** recording
   the live-host verification (date, what changed, what assertion).
5. **Update `docs/architecture.md`** if the subsystem boundary changed.
6. **Run `make test && make lint`** to confirm.
7. **Commit + push** with a Conventional Commits message.

## Common pitfalls (the "live host lessons" pattern)

Every load-bearing recipe change has a corresponding
"gotcha" entry in the skill (sections `Step 0a.x`, `Step 0b.x`, `Step
1.5.x`, `Step 2.2.x`). When you encounter a new live-host gotcha:

1. Capture the failure mode + the operator-visible symptom in the
   matching Step.
2. Capture the **root cause** and the **fix** in the gotcha body.
3. Capture the **live verification date + cluster state** in
   `versions.lock.yaml::cross_check`.
4. Add a **pytest assertion** that pins the fix
   (`tools/tests/test_agent_skill.py` is the canonical place for
   "this string must be in the skill" assertions).

## State of the live host (read this before assuming any state)

As of 2026-07-07, on `kvm.example.net` (BigBertha, PVE 9.2.3, kernel
`7.0.6-2-pve`):

- VMID 900 = `ubuntu-noble-template` (the golden image). `template: 1`,
  32 GB bootdisk, OVMF, q35, agent enabled, native cloud-init drive.
- VMID 950 has a stuck LV (`vm-950-disk-1`) from an earlier Packer
  build. **Do not use VMID 950**; the build is hardcoded to use 900.
  Recovery recipe is in
  `.agents/skills/proxmox-k3s-pipeline/SKILL.md` Step 1.5.2.
- VMIDs 111-114 are the 4 cluster VMs (`cicd-cp-1`, `cicd-w-1`,
  `apps-cp-1`, `apps-w-1`). All running, all have a working
  `qm agent <vmid> ping`.
- PowerDNS records are managed at `10.0.0.3:8081` (LXC 101). The
  `scripts/sync_dns_to_sdn.py` post-apply fix-up is required
  because the SDN IPAM allocates IPs from `10.0.0.50-200` regardless
  of `var.ip_start` (which is only used for the *intended* records).
- GitLab HTTP backend holds the tofu state at
  `gitlab.com/infra-state/bigbertha`. The `GITLAB_PAT` env var
  must be set before `tofu init` against any stack.

## Useful entry points

| Want to... | File |
|---|---|
| Read the operator playbook | `.agents/skills/proxmox-k3s-pipeline/SKILL.md` |
| Read the subsystem boundaries | `docs/architecture.md` |
| Add a new cluster | `docs/cluster-instances.md` + `infra/clusters/<name>/` |
| Run a single phase | `make build-image` (Phase 1), `python scripts/apply_tofu.py {tokens\|cicd\|apps}` (Phase 0/2), `python -m tools.bootstrap_cluster --cluster <name>` (Phase 4) |
| Modify the build recipe | `tools/build_image/__init__.py` + `tools/tests/test_build_image.py` |
| Modify the cluster module | `infra/modules/proxmox-k3s-cluster/*.tf` + `tools/tests/test_agent_skill.py` (cross-checks) |
| Modify the bootstrap | `tools/bootstrap_cluster.py` + `tools/tests/test_bootstrap_cluster.py` |
| Update the skill | `.agents/skills/proxmox-k3s-pipeline/SKILL.md` + `tools/tests/test_agent_skill.py` (string-assertion tests) + `versions.lock.yaml::cross_check` (live-host evidence) |
| Debug a stuck boot | `docs/proxmox-serial-capture.md` + `scripts/capture_serial.py` |
| Rotate tokens | `docs/runbooks/rotate-tokens.md` |
| Decommission a cluster | `docs/runbooks/decommission-cluster.md` |
| Scale workers | `docs/runbooks/scale-workers.md` |
| Recover from a Cloudflare outage | `docs/runbooks/cloudflare-fallback.md` |
| Reset the GitLab state backend | `docs/runbooks/gitlab-state-backend.md` |

## What NOT to do

- **Don't** use Packer, Talos, or the Sidero Image Factory. The
  pipeline pivoted off them on 2026-07-07.
- **Don't** use a custom NoCloud seed ISO. The template uses
  Proxmox's native `--ide2 data1:cloudinit` drive.
- **Don't** install `qemu-guest-agent` after creating the VM. The
  canonical recipe uses `virt-customize` to bake it into the image
  BEFORE the VM is created.
- **Don't** use VMID 950. It has a stuck LV that prevents
  `lvremove`.
- **Don't** commit `.env`, `output.json`, `*.tfstate*`, or any
  secret-bearing `terraform.tfvars`. They're all gitignored, but
  `git status` before every commit is cheap insurance.
- **Don't** `tofu destroy` the cluster roots without reading
  `docs/runbooks/decommission-cluster.md` first — the GitLab state
  has to be cleaned up in the right order or subsequent applies
  will plan a destructive recreate.
