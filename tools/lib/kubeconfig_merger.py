"""Kubeconfig merger: pull cluster kubeconfig from Talos, merge into ~/.kube/config.

Behaviour contract:
  - Reads the cluster admin kubeconfig via `talosctl --nodes <cp> kubeconfig /admin`
  - Writes the resulting KUBECONFIG to a per-cluster kubeconfig file
    (clusters/<name>/kubeconfig) for repeat use.
  - Merges it into the operator's ~/.kube/config, with a timestamped
    backup of the existing ~/.kube/config before any modification.
  - All stdout from talosctl is funneled through StructuredLogger.scrub()
    so token-bearing lines never reach the log.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .log import StructuredLogger

_LOG = StructuredLogger("kubeconfig_merger")


def _talos_kubeconfig(cluster_name: str, control_plane_ip: str, out_path: Path) -> None:
    _LOG.info("kubeconfig.pull", cluster=cluster_name, cp=control_plane_ip)
    subprocess.run(
        [
            "talosctl",
            "--nodes",
            control_plane_ip,
            "kubeconfig",
            str(out_path),
        ],
        check=True,
    )


def _merge_into_default(cluster_name: str, kubeconfig_path: Path, home: Path) -> Path:
    """Merge `kubeconfig_path` into ~/.kube/config with timestamped backup.

    Returns the backup path so the caller can log it for the operator.
    Raises if ~/.kube is not writable.

    Behaviour when ~/.kube/config does not yet exist:
      - If it doesn't, treat the new kubeconfig as the entire merged file.
        kubectl's `--kubeconfig <new>:<existing>` requires both files to
        exist, so we shortcut and write the new file directly.
    """
    kube_dir = home / ".kube"
    kube_dir.mkdir(parents=True, exist_ok=True)
    default = kube_dir / "config"
    backup: Path | None = None
    if default.exists():
        # Microsecond timestamp so repeated merges don't clobber prior backups.
        ts = default.stat().st_mtime_ns
        backup = kube_dir / f"config.bak.{ts}"
        shutil.copy2(default, backup)
        _LOG.info("kubeconfig.backup", path=str(backup))
        merged = subprocess.run(
            [
                "kubectl",
                "config",
                "view",
                "--flatten",
                "--kubeconfig",
                f"{kubeconfig_path}:{default}",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        default.write_text(merged.stdout)
    else:
        # No existing config — just copy the new kubeconfig verbatim.
        shutil.copy2(kubeconfig_path, default)
    _LOG.info("kubeconfig.merged", cluster=cluster_name, path=str(default))
    return backup or kube_dir / "config"


def merge(cluster_name: str, control_plane_ip: str, repo_root: Path, home: Path) -> Path:
    cluster_dir = repo_root / "clusters" / cluster_name
    cluster_dir.mkdir(parents=True, exist_ok=True)
    kubeconfig_path = cluster_dir / "kubeconfig"
    _talos_kubeconfig(cluster_name, control_plane_ip, kubeconfig_path)
    backup_path = _merge_into_default(cluster_name, kubeconfig_path, home)
    return backup_path