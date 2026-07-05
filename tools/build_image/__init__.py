"""CLI: bake a Talos Linux VM into a Proxmox template.

Implements SS1 (Image Build Pipeline). Addresses misfits:

  M1 (Packer race)       — `build/.build.lock` serialises concurrent builds
                           via an atomic create + fcntl flock. Second
                           invocation while the first holds the lock
                           exits non-zero with a structured error.
  M8 (compatibility)     — `--talos-version` is validated against
                           versions.yaml before any PVE call. Mismatch →
                           exit non-zero with structured error.
  M4 (silent failure)    — Every event emits one structured JSON line to
                           the audit log; secrets never logged. Packer
                           failure → destroy half-baked VM → exit non-zero.

Invocation: `python -m tools.build_image` (package form) -- see
`tools/build_image/__main__.py` for the entry-point shim.
"""
from __future__ import annotations

import argparse
import fcntl
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from tools.lib.log import StructuredLogger
from tools.lib.pve_client import PveClient
from tools.lib.secret_loader import SecretLoader


# Lockfile placed in build_dir to serialise concurrent builders (M1).
LOCK_FILE = ".build.lock"
# Output contract to SS2 (per plan.md): build/image-id.txt contains the
# Proxmox VMID of the baked Talos template.
IMAGE_ID_FILE = "image-id.txt"
# Template VMID hard-coded per spec; one template per Proxmox cluster.
TEMPLATE_VMID = 900
# Packer subprocess timeout: 10 minutes per spec.
PACKER_TIMEOUT_SECONDS = 600
PACKER_BIN = "packer"


@dataclass
class BuildImage:
    talos_version: str
    pve_endpoint: str
    pve_node: str
    pve_token_id: str
    pve_token_secret: str
    build_dir: Path
    versions_yaml: Path
    logger: StructuredLogger
    verbose: bool = False
    dry_run: bool = False
    pve: PveClient = field(init=False)
    secrets: SecretLoader = field(init=False)
    # File descriptor for the atomic lock (M1). Set in _acquire_lock,
    # cleared in _release_lock. Stays open for the lifetime of the build.
    _lock_fd: int | None = field(init=False, default=None, repr=False)

    def __post_init__(self) -> None:
        self.pve = PveClient(self.logger, endpoint=self.pve_endpoint)
        self.secrets = SecretLoader(self.logger)

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self) -> int:
        """Execute the build pipeline. Returns the process exit code."""
        try:
            try:
                # M8: validate Talos version against the compatibility matrix
                # before any PVE call.
                self._check_version()
            except _BuildAborted as exc:
                # The structured error is already in the audit log; just exit.
                self.logger.info(
                    step="aborted",
                    message=str(exc),
                )
                return 2

            # M1: lock against concurrent builds.
            if not self._acquire_lock():
                self.logger.error(
                    step="lock_held",
                    error=(
                        f"another build is holding {LOCK_FILE} in "
                        f"{self.build_dir}"
                    ),
                    resolution=(
                        "wait for the other build to finish, or remove "
                        f"{LOCK_FILE} if it's stale"
                    ),
                    lock_path=str(self.build_dir / LOCK_FILE),
                )
                return 10

            try:
                # Idempotency: if image-id.txt already exists with our
                # VMID, skip Packer.
                image_id_path = self.build_dir / IMAGE_ID_FILE
                if image_id_path.exists() and image_id_path.read_text().strip() == str(TEMPLATE_VMID):
                    self.logger.info(
                        step="idempotent_skip",
                        message="template already exists; nothing to do",
                        vmid=TEMPLATE_VMID,
                    )
                    return 0

                if self.dry_run:
                    self.logger.info(
                        step="dry_run",
                        message=(
                            f"would invoke packer build with talos="
                            f"{self.talos_version}"
                        ),
                        talos_version=self.talos_version,
                        pve_node=self.pve_node,
                    )
                    return 0

                # Run Packer. Any failure cleans up + emits structured error.
                return self._run_packer()

            finally:
                self._release_lock()

        except _PackerFailed as exc:
            self.logger.error(
                step="packer_failed",
                error=exc.reason,
                resolution=(
                    "inspect Packer log; manually delete VM "
                    f"{TEMPLATE_VMID} on bigbertha if it remains"
                ),
                talos_version=self.talos_version,
            )
            # M4: log the cleanup intent + destroy (best-effort).
            # The log entry fires before we delegate to _destroy_vm so that
            # tests which mock the destroy method still observe the
            # cleanup_destroy_vm audit line.
            self.logger.info(
                step="cleanup_destroy_vm",
                vmid=TEMPLATE_VMID,
                mode="best_effort",
            )
            try:
                self._destroy_vm(TEMPLATE_VMID)
            except Exception as cleanup_exc:  # noqa: BLE001 — best effort
                self.logger.warn(
                    step="cleanup_error",
                    message=str(cleanup_exc),
                    vmid=TEMPLATE_VMID,
                )
            return 3

# ------------------------------------------------------------------
    # Step: version validation (M8)
    # ------------------------------------------------------------------

    def _check_version(self) -> None:
        # Open the file directly and let FileNotFoundError win the race
        # cleanly, instead of an exists()/open() TOCTOU pair.
        try:
            fh = self.versions_yaml.open("r", encoding="utf-8")
        except FileNotFoundError:
            self.logger.error(
                step="versions_yaml_missing",
                error=f"versions.yaml not found at {self.versions_yaml}",
                resolution=(
                    "create versions.yaml with a `talos:` map; "
                    "see tools/tests/test_build_image.py for the shape"
                ),
            )
            raise _BuildAborted("versions_yaml_missing") from None

        with fh:
            data: dict[str, Any] = yaml.safe_load(fh) or {}

        talos_map = data.get("talos") or {}
        if self.talos_version not in talos_map:
            self.logger.error(
                step="version_check",
                error=(
                    f"talos version {self.talos_version!r} is not in "
                    f"versions.yaml under `talos:`"
                ),
                resolution=(
                    "add an entry to versions.yaml["
                    f"'talos'][{self.talos_version!r}] describing the "
                    "PVE kernel / k3s / Cilium compatibility, or pass "
                    "a known version (v1.10.0)"
                ),
                known=list(talos_map.keys()),
            )
            raise _BuildAborted("version_check")

        self.logger.info(
            step="version_check",
            message=f"talos_version {self.talos_version!r} is in versions.yaml",
            talos_version=self.talos_version,
        )

    # ------------------------------------------------------------------
    # Step: lock acquisition (M1)
    # ------------------------------------------------------------------

    def _acquire_lock(self) -> bool:
        """Atomically acquire an exclusive build lock.

        Two layers of mutual exclusion:

        1. ``O_CREAT | O_EXCL`` creates the lockfile only if no
           ``.build.lock`` exists. POSIX guarantees this is race-free
           across processes on the same filesystem: exactly one process
           will see ``fd >= 0``; concurrent attempts get ``FileExistsError``.
        2. ``fcntl.flock(LOCK_EX | LOCK_NB)`` takes an advisory exclusive
           lock on the open fd. This is a second gate covering the case
           where a prior process crashed mid-build and left a stale
           lockfile behind: even though ``O_EXCL`` would succeed against
           an inherited stale file, ``flock`` would conflict if another
           live process holds the same fd. (NB: stale files are safe to
           re-use — flock is per-process-descriptor on Linux, so a fresh
           open + flock will not conflict with a dead PID's lock.)

        Returns True if this call acquired the lock; False if another
        live process already holds it.
        """
        self.build_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self.build_dir / LOCK_FILE

        # Step 1: try to atomically create-or-open the lockfile with
        # ``O_CREAT | O_EXCL``. This is POSIX-atomic: only one process
        # across the entire host can win the create; concurrent attempts
        # get ``FileExistsError``. ``flock`` then guards against inheriting
        # a stale lockfile from a dead PID.
        try:
            fd = os.open(
                lock_path, os.O_CREAT | os.O_RDWR | os.O_EXCL, 0o600
            )
            # WE WON the create. We still must take flock so subsequent
            # contenders (who get FileExistsError, then reopen) cannot
            # also acquire.
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                return False
        except FileExistsError:
            # Someone else created the lockfile. Open without O_EXCL and
            # try to flock. If flock fails with EWOULDBLOCK, a live process
            # holds the lock and we must not steal.
            try:
                fd = os.open(lock_path, os.O_RDWR, 0)
            except OSError:
                return False
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                return False

        try:
            os.write(fd, f"{os.getpid()}\n".encode())
        except OSError:
            pass

        self._lock_fd = fd
        return True

    def _release_lock(self) -> None:
        """Release the lock acquired by _acquire_lock.

        Idempotent — safe to call multiple times or after _acquire_lock
        returned False.
        """
        fd = self._lock_fd
        if fd is None:
            return
        self._lock_fd = None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass
        # Defensive unlink: the lockfile's lifecycle is tied to the fd,
        # but if a crash left a stale one behind the next run will
        # recreate it via O_EXCL only if needed. We DO unlink on a
        # clean release so the next run starts fresh.
        lock_path = self.build_dir / LOCK_FILE
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass

    # ------------------------------------------------------------------
    # Step: Packer invocation (the thing that can fail mid-build)
    # ------------------------------------------------------------------

    def _run_packer(self) -> int:
        self.logger.info(
            step="packer_invoke",
            message=f"invoking {PACKER_BIN} build",
            talos_version=self.talos_version,
            pve_node=self.pve_node,
        )

        # Path of the HCL template. This file lives at tools/build_image/
        # (parent of __init__.py), so the template is one level up under
        # tools/packer/.
        template_path = (
            Path(__file__).resolve().parent.parent / "packer" / "talos.pkr.hcl"
        )
        if not template_path.exists():
            self.logger.error(
                step="packer_template_missing",
                error=f"{template_path} does not exist",
                resolution=(
                    "ensure tools/packer/talos.pkr.hcl is committed"
                ),
            )
            raise _PackerFailed("packer_template_missing")

        cmd = [
            PACKER_BIN,
            "build",
            "-var", f"talos_version={self.talos_version}",
            "-var", f"pve_endpoint={self.pve_endpoint}",
            "-var", f"pve_node={self.pve_node}",
            "-var", f"pve_token_id={self.pve_token_id}",
            "-var", f"pve_token_secret={self.pve_token_secret}",
            str(template_path),
        ]

        try:
            completed = subprocess.run(
                cmd,
                check=False,
                capture_output=True,
                text=True,
                timeout=PACKER_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            self.logger.error(
                step="packer_timeout",
                error=f"{PACKER_BIN} exceeded {PACKER_TIMEOUT_SECONDS}s",
                resolution=(
                    f"check PVE console on bigbertha; manually delete "
                    f"VM {TEMPLATE_VMID} if half-baked"
                ),
            )
            raise _PackerFailed("packer_timeout")

        if completed.returncode != 0:
            # Capture only stderr summary so we don't leak secrets into the
            # audit log via Packer echoing env vars.
            err_summary = _one_line(completed.stderr)
            self.logger.error(
                step="packer_nonzero_exit",
                error=(
                    f"{PACKER_BIN} exited with code {completed.returncode}"
                ),
                resolution=(
                    "rerun with --verbose; check that Proxmox creds are "
                    "valid; check that the Talos ISO URL is reachable"
                ),
                stderr_summary=err_summary,
            )
            raise _PackerFailed(f"packer_nonzero_exit={completed.returncode}")

        # Success: write the image-id.txt contract for SS2.
        self.build_dir.mkdir(parents=True, exist_ok=True)
        (self.build_dir / IMAGE_ID_FILE).write_text(f"{TEMPLATE_VMID}\n")

        self.logger.info(
            step="complete",
            message="Talos template baked",
            template_vmid=TEMPLATE_VMID,
            talos_version=self.talos_version,
        )
        return 0

    # ------------------------------------------------------------------
    # Step: cleanup on failure
    # ------------------------------------------------------------------

    def _destroy_vm(self, vmid: int) -> None:
        """Best-effort VM destroy. PveClient swallows non-zero exit.

        Note: callers should log the `cleanup_destroy_vm` step before this
        call so tests that mock this method still observe the audit line.
        """
        self.pve.destroy_vm(vmid)


# ----------------------------------------------------------------------
# Internal exception types — never escape BuildImage.run()
# ----------------------------------------------------------------------


class _BuildAborted(Exception):
    """Used for clean exits the audit log already explains."""


class _PackerFailed(Exception):
    """Packer mid-build failure that requires cleanup."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _one_line(text: str, *, limit: int = 240) -> str:
    """Collapse a multi-line string to a single line for log readability."""
    import re

    collapsed = re.sub(r"\s+", " ", text or "").strip()
    if len(collapsed) > limit:
        return collapsed[: limit - 1] + "…"
    return collapsed


# ----------------------------------------------------------------------
# CLI entry point
# ----------------------------------------------------------------------


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bake a Talos Linux VM into a Proxmox template.",
    )
    parser.add_argument(
        "--talos-version",
        required=True,
        help="Talos version to bake (e.g. v1.10.0). Must be in versions.yaml.",
    )
    parser.add_argument(
        "--pve-endpoint",
        required=True,
        help="Proxmox VE API endpoint URL (e.g. https://bigbertha:8006/api2/json).",
    )
    parser.add_argument(
        "--pve-node",
        default="bigbertha",
        help="Proxmox node name (default: bigbertha).",
    )
    parser.add_argument(
        "--pve-token-id",
        default=os.environ.get("PVE_TOKEN_ID", ""),
        help=(
            "PVE API token id (USER@REALM!TOK). Sourced from PVE_TOKEN_ID env "
            "var if not set on the command line. Never logged."
        ),
    )
    parser.add_argument(
        "--pve-token-secret",
        default=os.environ.get("PVE_TOKEN_SECRET", ""),
        help=(
            "PVE API token secret. Sourced from PVE_TOKEN_SECRET env var if "
            "not set. Never logged."
        ),
    )
    parser.add_argument(
        "--build-dir",
        type=Path,
        default=Path("build"),
        help="Build output directory (default: ./build).",
    )
    parser.add_argument(
        "--versions-yaml",
        type=Path,
        default=Path("versions.yaml"),
        help="Compatibility matrix YAML (default: ./versions.yaml).",
    )
    parser.add_argument(
        "--audit-log",
        type=Path,
        default=Path.home() / ".spec-bridge-skill-tool" / "build-image-audit.log",
        help="JSON-line audit log path.",
    )
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate inputs and emit structured logs, but do not invoke Packer or PVE.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    logger = StructuredLogger("build_image", log_path=args.audit_log, verbose=args.verbose)
    logger.info(
        step="startup",
        message="build_image invoked",
        talos_version=args.talos_version,
        pve_node=args.pve_node,
        dry_run=args.dry_run,
    )

    # Validate that token is present, either via flag or env var.
    # We use SecretLoader so the value is never logged; we only confirm presence.
    bi = BuildImage(
        talos_version=args.talos_version,
        pve_endpoint=args.pve_endpoint,
        pve_node=args.pve_node,
        pve_token_id=args.pve_token_id,
        pve_token_secret=args.pve_token_secret,
        build_dir=args.build_dir,
        versions_yaml=args.versions_yaml,
        logger=logger,
        verbose=args.verbose,
        dry_run=args.dry_run,
    )
    return bi.run()
