"""Talos client: thin wrapper around `talosctl` for SS3 bootstrap.

This is intentionally minimal: it knows how to:
  - read machineconfig YAML files emitted by SS2 (infra/modules/proxmox-k3s-cluster)
  - apply them to each node IP
  - poll health
  - bootstrap k3s on the first healthy control-plane

It does NOT know how to download Talos images (that's WP01/packer) or
how to write kubeconfig (that's lib.kubeconfig_merger).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .log import StructuredLogger

_LOG = StructuredLogger("talos_client")


def _require_bin(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise RuntimeError(
            f"required binary '{name}' not found on PATH; "
            f"see tools/versions.lock.yaml"
        )
    return path


@dataclass(frozen=True)
class ClusterTopology:
    """Shape of infra/clusters/<name>/output.json as SS2 emits it.

    SS2 contract (infra/modules/proxmox-k3s-cluster/outputs.tf::local_sensitive_file
    cluster_output) emits a flat `nodes` array with role="control_plane" or
    "worker". This class splits them into the two collections the bootstrap
    script needs.
    """

    name: str
    vip: str
    pod_cidr: str
    svc_cidr: str
    cluster_dns: str
    control_plane: Sequence[Mapping[str, str]]
    worker: Sequence[Mapping[str, str]]

    @property
    def all_nodes(self) -> list[Mapping[str, str]]:
        return [*self.control_plane, *self.worker]

    @classmethod
    def from_output_json(cls, path: Path) -> "ClusterTopology":
        data: dict[str, Any] = json.loads(path.read_text())
        try:
            nodes = data["nodes"]
            cps = [n for n in nodes if n.get("role") == "control_plane"]
            wks = [n for n in nodes if n.get("role") == "worker"]
            return cls(
                name=data["cluster_name"],
                vip=data["vip"],
                pod_cidr=data.get("pod_cidr", "172.16.0.0/16"),
                svc_cidr=data.get("svc_cidr", "172.17.0.0/16"),
                # WP08 (2026-07-08): default to 172.17.0.10 for cicd-
                # compatible svc CIDR; apps will read 172.19.0.10
                # from output.json (output.tf contract).
                cluster_dns=data.get("cluster_dns", "172.17.0.10"),
                control_plane=cps,
                worker=wks,
            )
        except KeyError as exc:
            missing = exc.args[0]
            raise ValueError(
                f"output.json missing required field '{missing}'"
            ) from exc


class TalosClient:
    def __init__(self, topo: ClusterTopology, talos_dir: Path) -> None:
        _require_bin("talosctl")
        self.topo = topo
        self.talos_dir = talos_dir

    def apply_configs(self) -> None:
        """Push machineconfig to every node.

        Non-zero exits propagate (the BootstrapError handler in
        bootstrap_cluster.py turns them into a clear message).
        """
        for node in self.topo.all_nodes:
            cfg = self.talos_dir / f"{node['name']}.yaml"
            if not cfg.exists():
                raise FileNotFoundError(
                    f"missing Talos machineconfig: {cfg}"
                )
            _LOG.info(
                "talos.apply",
                node=node["name"],
                endpoint=node["ip"],
            )
            subprocess.run(
                ["talosctl", "apply-config", "--insecure", "--nodes", node["ip"], "--file", str(cfg)],
                check=True,
            )

    def wait_for_healthy(self, timeout_s: int = 300) -> None:
        if not self.topo.control_plane:
            raise ValueError("no control_plane nodes to wait for")
        first_cp_ip = self.topo.control_plane[0]["ip"]
        _LOG.info("talos.wait", endpoint=first_cp_ip, timeout_s=timeout_s)
        subprocess.run(
            [
                "talosctl",
                "--nodes",
                first_cp_ip,
                "--endpoints",
                first_cp_ip,
                "health",
                "--wait-timeout",
                f"{timeout_s}s",
            ],
            check=True,
        )

    def bootstrap_k3s(self) -> None:
        """Run `talosctl bootstrap` against the first control-plane node.

        Note: this is the Talos-bootstrap of k3s (k3s itself runs inside a
        static pod). After this returns, `helm/kubectl` will talk to the
        cluster via the VIP.
        """
        first_cp_ip = self.topo.control_plane[0]["ip"]
        _LOG.info("talos.bootstrap", endpoint=first_cp_ip)
        subprocess.run(
            ["talosctl", "bootstrap", "--nodes", first_cp_ip],
            check=True,
        )


def _self_check() -> int:
    print(f"talos_client loaded; talosctl={_require_bin('talosctl')}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_self_check())