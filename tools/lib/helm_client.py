"""Helm client: thin wrapper for SS3's helm phase.

Encapsulates the helm calls needed to install the 'first two' releases:
  - cilium
  - kube-vip (run as DaemonSet on control-plane nodes)

Pattern: `helm upgrade --install` (idempotent). Re-running on a populated
cluster is a no-op rather than a failure.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from .log import StructuredLogger

_LOG = StructuredLogger("helm_client")


def _require_bin(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise RuntimeError(f"required binary '{name}' not found on PATH")
    return path


@dataclass(frozen=True)
class HelmRelease:
    name: str
    chart: str
    namespace: str
    version: str
    values: Mapping[str, object]

    def install_cmd(self, kubeconfig: Path) -> list[str]:
        cmd = [
            "helm",
            "upgrade",
            "--install",
            self.name,
            self.chart,
            "--namespace",
            self.namespace,
            "--create-namespace",
            "--version",
            self.version,
            "--wait",
            "--kubeconfig",
            str(kubeconfig),
        ]
        for k, v in self.values.items():
            cmd += ["--set", f"{k}={v}"]
        return cmd


class HelmClient:
    def __init__(self, kubeconfig: Path) -> None:
        _require_bin("helm")
        self.kubeconfig = kubeconfig

    def install_or_upgrade(self, releases: Sequence[HelmRelease]) -> None:
        for r in releases:
            cmd = r.install_cmd(self.kubeconfig)
            _LOG.info("helm.upgrade_install", release=r.name, namespace=r.namespace)
            subprocess.run(cmd, check=True)


def first_two_releases(cluster: Mapping[str, object]) -> list[HelmRelease]:
    """The first two Helm releases for a new cluster.

    Recipes come straight from the WP04 spec (T005 + T006). The Cilium
    release pulls pod_cidr from the cluster output so IPAM cluster-pool
    sizing matches SS2's Pod CIDR.

    Versions and values are recorded in tools/versions.lock.yaml.
    """
    pod_cidr = cluster.get("pod_cidr", "10.42.0.0/16")
    return [
        HelmRelease(
            name="cilium",
            chart="cilium/cilium",
            namespace="kube-system",
            version="1.16.1",
            values={
                "kubeProxyReplacement": "true",
                "gatewayAPI.enabled": "true",
                "ipv4NativeRoutingCIDR": "10.0.0.0/8",
                "ipam.mode": "cluster-pool",
                "ipam.operator.clusterPoolIPv4PodCIDRList": pod_cidr,
                "hubble.enabled": "false",
            },
        ),
        HelmRelease(
            name="kube-vip",
            chart="kube-vip/kube-vip",
            namespace="kube-system",
            version="1.2.1",
            values={
                "interface": "eth0",
                "leaderElection": "true",
                "controlPlane.enabled": "true",
                "controlPlane.hostPort": "6443",
                "services.etcd.enabled": "false",
            },
        ),
    ]