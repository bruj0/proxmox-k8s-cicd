"""Helm client: thin wrapper for SS3's helm phase.

Encapsulates the helm calls needed to install the 'first two' releases:
  - cilium
  - kube-vip (run as DaemonSet on control-plane nodes)

Pattern: `helm upgrade --install` (idempotent). Re-running on a populated
cluster is a no-op rather than a failure.

WP05 extends this module with `remaining_releases()` which returns the
four non-helm chart installs that follow the first two:
  - sergelogvinov/proxmox-cloud-controller-manager (providerID + topology labels)
  - sergelogvinov/proxmox-csi-plugin (lvm-thin StorageClass)
  - cert-manager/cert-manager (in-cluster CA only; NO ACME)
  - oci://ghcr.io/strrl/charts/cloudflare-tunnel-ingress-controller
plus a `kubectl apply` payload for the Traefik HelmChartConfig that
WP02 rendered into infra/clusters/<name>/manifests/traefik-helmchartconfig.yaml.

Demoted Traefik itself runs INSIDE k3s (via the HelmChartConfig) so
SS3 does not call `helm upgrade --install` for it.
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

    Recipes are pinned against the live host (cicd + apps clusters,
    2026-07-08 cross_check in infra/clusters/<name>/versions.lock.yaml).
    The cilium release pulls pod_cidr from the cluster output so IPAM
    cluster-pool sizing matches SS2's Pod CIDR; the kube-vip release
    uses the chart's `config.address` + `env.cp_enable` shape, which
    is the upstream-canonical form for chart 0.9.x (the old
    `controlPlane.enabled` shape was a 1.x-only preview that never
    made it into a release).

    Versions and values are recorded in tools/versions.lock.yaml and
    pinned by tests/test_remaining_releases.py + test_agent_skill.py
    so a stale-pin regression fails CI.
    """
    pod_cidr = cluster.get("pod_cidr", "10.42.0.0/16")
    vip = str(cluster.get("vip", ""))
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
            version="0.9.9",
            # The kube-vip chart's values shape in 0.9.x lives under
            # `config:` and `env:` (camelCase keys). The "controlPlane.*"
            # shape is NOT supported in any released chart version; it
            # was a doc-only preview. Live-validated 2026-07-08.
            values={
                "config.address": vip,
                "env.cp_enable": "true",
                "env.vip_interface": "eth0",
                "env.vip_arp": "true",
                "env.vip_leaderelection": "true",
                "env.lb_enable": "true",
                "env.lb_port": "6443",
                "env.svc_enable": "true",
                "env.svc_election": "false",
            },
        ),
    ]


@dataclass(frozen=True)
class ManifestApply:
    """A kubectl apply step for a pre-rendered Kubernetes manifest.

    Used by WP05 to apply the Traefik HelmChartConfig that
    infra/modules/proxmox-k3s-cluster/ rendered in WP02.
    """

    kind: str
    path: Path
    namespace: str


def _proxmox_region_zone(cluster: Mapping[str, object]) -> tuple[str, str]:
    pve = str(cluster.get("pve_node") or "proxmox-host")
    return pve, pve.capitalize()


def remaining_releases(
    cluster: Mapping[str, object],
    secrets: Mapping[str, str],
) -> tuple[list[HelmRelease], ManifestApply | None]:
    """WP05: remaining helm releases + Traefik HelmChartConfig apply.

    Secrets are passed in directly (read at runtime from
    secret_loader.SecretLoader); they never enter logs.

    Chart versions and OCI refs are pinned against the live host
    (cicd + apps clusters, 2026-07-08) and asserted by
    tests/test_agent_skill.py. The sergelogvinov charts moved to
    OCI in late 2025; the old HTTP `sergelogvinov/<chart>` paths
    return 404 now. Use `oci://ghcr.io/sergelogvinov/charts/<chart>`.
    """
    region, zone = _proxmox_region_zone(cluster)
    cluster_name = str(cluster["name"])
    cluster_dir = Path("clusters") / cluster_name
    manifests_dir = cluster_dir / "manifests"
    rels: list[HelmRelease] = [
        HelmRelease(
            name="proxmox-cloud-controller-manager",
            chart="oci://ghcr.io/sergelogvinov/charts/proxmox-cloud-controller-manager",
            namespace="kube-system",
            version="0.2.29",
            values={
                "region": region,
                "zone": zone,
                "credentials.url": "https://10.0.0.1:8006",
                "credentials.tokenId": secrets["proxmox_token_id"],
                "credentials.tokenSecret": secrets["proxmox_token_secret"],
            },
        ),
        HelmRelease(
            name="proxmox-csi-plugin",
            chart="oci://ghcr.io/sergelogvinov/charts/proxmox-csi-plugin",
            namespace="proxmox-csi-plugin",
            version="0.5.9",
            values={
                "storageclass.name": "proxmox-lvm-thin",
                "storageclass.default": "true",
                "region": region,
                "zone": zone,
                "csi.lvm.thinPool": "data1/data1",
            },
        ),
        HelmRelease(
            name="cloudflare-tunnel-ingress-controller",
            chart="oci://ghcr.io/strrl/charts/cloudflare-tunnel-ingress-controller",
            namespace="cloudflare-tunnel-ingress-controller",
            version="0.0.23",
            values={
                "cloudflare.apiToken": secrets["cf_api_token"],
                "cloudflare.accountId": secrets["cf_account_id"],
                "cloudflare.tunnelName": str(
                    cluster.get("cf_tunnel_name") or f"{cluster_name}-tunnel"
                ),
                "ingressClass.name": "cloudflare-tunnel",
                "ingressClass.controller": "dev.strrl.cloudflaretunnelingresscontroller/ingress",
                "ingressClass.enabled": "true",
            },
        ),
        HelmRelease(
            name="cert-manager",
            chart="cert-manager/cert-manager",
            namespace="cert-manager",
            version="1.20.3",
            values={
                "installCRDs": "true",
            },
        ),
    ]
    traefik_yaml = manifests_dir / "traefik-helmchartconfig.yaml"
    traefik_apply: ManifestApply | None = None
    if traefik_yaml.exists():
        traefik_apply = ManifestApply(
            kind="HelmChartConfig",
            path=traefik_yaml,
            namespace="kube-system",
        )
    return rels, traefik_apply


def apply_manifest(
    manifest: ManifestApply, kubeconfig: Path, *, dry_run: bool = False
) -> None:
    cmd = [
        "kubectl",
        "--kubeconfig",
        str(kubeconfig),
        "apply",
        "--namespace",
        manifest.namespace,
        "-f",
        str(manifest.path),
    ]
    if dry_run:
        cmd.append("--dry-run=client")
    _LOG.info(
        "kubectl.apply",
        kind=manifest.kind,
        path=str(manifest.path),
        namespace=manifest.namespace,
        dry_run=dry_run,
    )
    subprocess.run(cmd, check=True)