"""PveClient — thin subprocess wrapper for Proxmox VE CLI utilities.

Backs onto `qm` and `pvesh` (always present on a PVE host). Wraps them so
the rest of the pipeline doesn't have to construct CLI strings, and so
failures become structured log entries instead of raw stderr.

This is intentionally not the bpg/proxmox Go SDK — we want the build_image
script to run on a workstation without requiring a Go toolchain.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

from tools.lib.log import StructuredLogger


@dataclass
class PveError(RuntimeError):
    """Raised when `qm`/`pvesh` exits non-zero on a non-cleanup call."""

    command: list[str]
    returncode: int
    stderr: str


@dataclass
class PveClient:
    logger: StructuredLogger
    endpoint: str = ""

    @dataclass
    class RunResult:
        returncode: int
        stdout: str
        stderr: str

    def _run(
        self,
        args: list[str],
        *,
        timeout: float = 30.0,
        allow_failure: bool = False,
    ) -> RunResult:
        """Run a subprocess command and stream structured logs.

        `allow_failure=True` swallows non-zero exit codes (used for
        best-effort cleanup like qm destroy on a non-existent VM).
        """
        self.logger.info(
            step="pve_exec",
            command=args[0],
            args_count=len(args),
        )
        try:
            completed = subprocess.run(
                args,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            self.logger.error(
                step="pve_timeout",
                error=str(exc),
                resolution=(
                    "increase timeout; check PVE host reachability "
                    "(ssh bigbertha)"
                ),
            )
            raise

        result = self.RunResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )

        if completed.returncode != 0:
            self.logger.warn(
                step="pve_nonzero_exit",
                message=f"{args[0]} exited with code {completed.returncode}",
                stderr_summary=_one_line(completed.stderr),
                allow_failure=allow_failure,
            )
            if not allow_failure:
                raise PveError(
                    command=args,
                    returncode=completed.returncode,
                    stderr=completed.stderr,
                )
        return result

    # ----- public API -----

    def destroy_vm(self, vmid: int) -> None:
        """Best-effort VM destroy. Swallows non-zero exit (the VM may
        already be gone — that's still a successful cleanup).

        We also stop the VM first in case it's running; qm destroy on a
        running VM will fail.
        """
        self.logger.info(step="cleanup_destroy_vm", vmid=vmid, mode="stop_then_destroy")
        try:
            self._run(["qm", "stop", str(vmid)], timeout=30.0, allow_failure=True)
        except subprocess.TimeoutExpired:
            pass
        try:
            self._run(
                ["qm", "destroy", str(vmid), "--skiplock", "--purge"],
                timeout=60.0,
                allow_failure=True,
            )
        except subprocess.TimeoutExpired:
            self.logger.warn(
                step="cleanup_destroy_vm_timeout",
                vmid=vmid,
                message="qm destroy timed out — VM may still exist",
                resolution=(
                    "run `qm destroy 900 --skiplock --purge` manually on bigbertha"
                ),
            )

    def find_template_vmid(self, name: str) -> int | None:
        """Look up the VMID for a Proxmox template by name. Returns None if absent.

        Uses `qm list` (output: whitespace-padded columns). We avoid the
        JSON output because `qm list --json` requires a Proxmox >=7.2 with
        a new enough qm, which we don't want to require for the CLI.
        """
        result = self._run(
            ["qm", "list"], timeout=30.0, allow_failure=False
        )
        for line in result.stdout.splitlines():
            # Match e.g.: "       900  talos-v1.10.0    stopped"
            # The first whitespace-padded column is the VMID, the second
            # is the name; subsequent columns (status, etc.) are ignored.
            m = re.match(r"^\s*(\d+)\s+(\S+)", line)
            if m and m.group(2) == name:
                return int(m.group(1))
        return None


def _one_line(text: str, *, limit: int = 240) -> str:
    """Backwards-compat shim: collapse multi-line text for log readability.

    Implementation now lives in :mod:`tools.lib.log` (single source of
    truth). This thin wrapper exists so existing imports keep working
    until the call sites are migrated.
    """
    from tools.lib.log import _one_line as _impl
    return _impl(text, limit=limit)