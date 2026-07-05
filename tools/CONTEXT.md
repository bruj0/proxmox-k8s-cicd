---
context_name: "Image Build Pipeline"
version: "1"
subsystem: "tools/"
created: "2026-07-05T19:00:00Z"
updated: "2026-07-05T19:00:00Z"
---

# Image Build Pipeline (SS1)

The Python + Packer subsystem that bakes a Talos Linux VM into a Proxmox template. Idempotent (re-running with the same inputs is a no-op), version-validated (Talos must be in `versions.yaml`), and audit-logged (every event emits one JSON line). Outputs a `build/image-id.txt` containing the Proxmox VMID (900) for SS2 to read.

## Language

**Image Template**:
A Proxmox VM that has been converted from a halted base VM into a clonable blueprint. In this WP the template VMID is hard-coded to 900 (per spec). The base VM (VMID 999) is created once by an operator and is *not* managed by this pipeline.
_Avoid_: `golden image`, `base image`, `base AMI` (these are cloud-specific terms).
_Subsystems_: SS1
_Files_: `tools/packer/talos.pkr.hcl`, `build/image-id.txt`
_Relates to_: Talos Version (validated against), Build Lock (cohort)
_History_:
- 2026-07-05 (001-build-a-kubernetes-k3s-cluster-on-proxmo): initial definition.

**Build Lock**:
A sentinel file at `build/.build.lock` whose presence indicates a build is in progress. Two concurrent invocations of `build_image.py` race for this lock; the second exits non-zero (M1 Packer race resolution).
_Avoid_: `mutex`, `flock` (lower-level Unix primitives — the spec asks for a portable file lock).
_Subsystems_: SS1
_Files_: `build/.build.lock`
_Relates to_: Image Template (one build at a time per template VMID)
_History_:
- 2026-07-05 (001-build-a-kubernetes-k3s-cluster-on-proxmo): initial definition.

**Packer Timeout**:
The 600-second (10-minute) hard cap on a Packer subprocess. Exceeding it raises `_PackerFailed("packer_timeout")` which triggers VM cleanup. Matches the spec's "exits 0 within 10 minutes" acceptance criterion.
_Subsystems_: SS1
_Files_: `tools/build_image.py` (PACKER_TIMEOUT_SECONDS constant)
_Relates to_: Image Template (Packer build), Talos Version (mapped via versions.yaml)
_History_:
- 2026-07-05 (001-build-a-kubernetes-k3s-cluster-on-proxmo): initial definition.

**Versions Matrix**:
The `versions.yaml` file at the repository root, owned by SS1 but read by every other subsystem. Each entry is a known-good (Talos version ↔ PVE kernel ↔ k3s ↔ Cilium) tuple. Validated at script start (M8 compatibility); mismatch ⇒ structured error ⇒ exit non-zero.
_Avoid_: `versions.yml`, `versions.json` (canonical name is `versions.yaml` per spec).
_Subsystems_: SS1 (owner), SS2/SS3 (consumers)
_Files_: `versions.yaml`, `tools/build_image.py` (consumer at startup)
_Relates to_: Talos Version (key), Image Template (the only output is locked to these combos)
_History_:
- 2026-07-05 (001-build-a-kubernetes-k3s-cluster-on-proxmo): initial definition.

**Audit Log Entry**:
One JSON object per line written to `~/.spec-bridge-skill-tool/<session>/build-image-audit.log`. Every event (startup, version_check, lock acquisition, Packer invocation, cleanup, completion) emits exactly one entry. Secrets (PVE_TOKEN_SECRET, api_token, etc.) are redacted (key dropped) per M7.
_Avoid_: `stdout` (console output is human-readable; the audit log is machine-parseable).
_Subsystems_: SS1
_Files_: `tools/lib/log.py`
_Relates to_: Image Template (every event is keyed to a step in the build)
_History_:
- 2026-07-05 (001-build-a-kubernetes-k3s-cluster-on-proxmo): initial definition.

**Half-Baked VM**:
A VM whose Talos install was interrupted mid-build (e.g. Packer crashed). On any Packer non-zero exit, `_destroy_vm(TEMPLATE_VMID)` removes the half-baked VM via `qm stop && qm destroy --skiplock --purge`. The build is treated as failed; `build/image-id.txt` is unchanged.
_Avoid_: `stuck VM`, `broken VM` (vague).
_Subsystems_: SS1
_Files_: `tools/lib/pve_client.py`, `tools/build_image.py`
_Relates to_: Image Template (cleanup target), Packer Timeout (one common trigger)
_History_:
- 2026-07-05 (001-build-a-kubernetes-k3s-cluster-on-proxmo): initial definition.

**Talos Version**:
A release tag (e.g. v1.10.0) validated against `versions.yaml` before any PVE call. The combination of `<talos_version>` + the Proxmox environment is what makes a known-good Image Template.
_Avoid_: `Talos release`, `Talos build`.
_Subsystems_: SS1 (validated at runtime), SS2 (consumed via the baked template)
_Files_: `versions.yaml`, `tools/build_image.py`, `tools/packer/talos.pkr.hcl`
_Relates to_: Versions Matrix (its container), Image Template (the baked artifact)
_History_:
- 2026-07-05 (001-build-a-kubernetes-k3s-cluster-on-proxmo): initial definition.

## Relationships

- An **Image Template** is the output of one successful build; its VMID is locked to one **Talos Version**.
- The **Build Lock** ensures one Image Template is baked at a time (M1 Packer race resolution).
- A **Packer Timeout** aborts the build and triggers cleanup of any **Half-Baked VM**.
- The **Versions Matrix** gates the entire pipeline: an unknown **Talos Version** rejects the build before any PVE call (M8 compatibility).
- Every pipeline step emits at least one **Audit Log Entry**; secrets are redacted before they are written.

## Flagged Ambiguities

- "Proxmox template" is also used by HashiCorp's Packer docs to mean a generic image template. Resolved: use **Image Template** for the baked Talos VM and "Packer template" for the HCL file at `tools/packer/talos.pkr.hcl`.
- "Build state" was used generically — resolved: use **Build Lock** for the file at `build/.build.lock` and the broader subprocess state for the Packer invocation.
- "Image id" was used in research logs ambiguously — resolved: **build/image-id.txt** (the contract file) and the **Talos Version** (its content from versions.yaml), never mix them up.
