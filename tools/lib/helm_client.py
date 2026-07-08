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
from dataclasses import dataclass, field
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
    # Optional values file path. Used for keys that don't survive
    # the helm --set path grammar (e.g. map keys with dots like
    # `storageclass.kubernetes.io/is-default-class`). The file is
    # applied via `-f` AFTER any `--set` flags (helm precedence:
    # -f beats --set).
    values_file: Path | None = None
    # Per-key `--set-string` overrides. helm's --set path
    # grammar auto-converts values like `true`/`false`/`42` to
    # their YAML types; for keys whose target schema is always a
    # string (e.g. annotation values), the auto-conversion
    # breaks. --set-string preserves the value's string type.
    # Applied AFTER `values` so callers can layer both.
    set_strings: Mapping[str, str] = field(default_factory=dict)

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
        for k, v in self.set_strings.items():
            cmd += ["--set-string", f"{k}={v}"]
        if self.values_file is not None:
            cmd += ["-f", str(self.values_file)]
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
                # WP07 fix (2026-07-08, §14.4): k3s is now started with
                # --disable-kube-proxy (see tools/lib/k3s_installer.py
                # _SERVER_BASE_FLAGS). cilium therefore fully owns
                # ClusterIP routing via eBPF. Two follow-up settings:
                #
                # 1. k8sServiceHost/Port: cilium must reach the apiserver
                #    *before* its eBPF ClusterIP routing is installed
                #    (chicken-and-egg). Without these it tries
                #    https://10.43.0.1:443 and crashes on startup. Point
                #    it at the kube-vip VIP (which is already reachable
                #    via L2 ARP before any pod network exists).
                "kubeProxyReplacement": "true",
                "k8sServiceHost": vip,
                "k8sServicePort": "6443",
                # 2. mtu=1450: vxlan adds 50 bytes of overhead to the
                #    underlying eth0 mtu=1500. Without this, large TLS
                #    ServerHello responses from the apiserver get
                #    fragmented at the vxlan encap and the conntrack
                #    return-path drops them (this was the visible
                #    half of §14.4 before the MASQUERADE root cause was
                #    identified).
                "mtu": "1450",
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
    # WP07 (2026-07-08): proxmox-ccm + proxmox-csi chart values fix.
    # The chart's `config.clusters[0]` is the canonical key shape
    # (see ghcr.io/sergelogvinov/charts values.yaml). The previous
    # `credentials.*` keys were silently ignored, so the chart's
    # secrets.yaml conditional `if ne (len .Values.config.clusters) 0`
    # never fired, the `proxmox-cloud-controller-manager` /
    # `proxmox-csi-plugin` Secrets were never created, and the
    # Deployment Pods were stuck in ContainerCreating on a missing
    # `cloud-config` volume mount for 4+ hours. The fix matches the
    # chart's documented schema (and a `helm template` round-trip
    # now produces the expected Secrets).
    #
    # The PVE URL is hard-coded to kvm.example.net:8006 because the
    # live cluster is not on the SDN (it's a 10.0.10/24 host with
    # a public-ish IP via PowerDNS). The 10.0.0.1 host-internal
    # URL is NOT routable from inside the k3s pods (see
    # docs/cluster-state.md §14.1 + the live-host gotcha log).
    rels: list[HelmRelease] = [
        HelmRelease(
            name="proxmox-cloud-controller-manager",
            chart="oci://ghcr.io/sergelogvinov/charts/proxmox-cloud-controller-manager",
            namespace="kube-system",
            version="0.2.29",
            values={
                # config schema is the chart's documented key shape.
                # The chart's secrets.yaml gates the Secret
                # creation on `len .Values.config.clusters) 0`
                # so an empty list would skip the Secret entirely
                # (and the Deployment pod would be stuck waiting
                # for a volume mount on a Secret that never
                # existed).
                "config.clusters[0].url": "https://kvm.example.net:8006/api2/json",
                "config.clusters[0].token_id": secrets["proxmox_token_id"],
                "config.clusters[0].token_secret": secrets["proxmox_token_secret"],
                "config.clusters[0].region": region,
                # Region/zone labels on the cloud Nodes are
                # derived from the cluster registry, not from
                # this value, but the chart documents them here
                # for the topology controller.
                "config.features.provider": "default",
            },
        ),
        HelmRelease(
            name="proxmox-csi-plugin",
            chart="oci://ghcr.io/sergelogvinov/charts/proxmox-csi-plugin",
            namespace="proxmox-csi-plugin",
            version="0.5.9",
            values={
                # config schema is the chart's documented key shape
                # (see comment on the proxmox-cloud-controller-manager
                # release above).
                "config.clusters[0].url": "https://kvm.example.net:8006/api2/json",
                "config.clusters[0].token_id": secrets["proxmox_token_id"],
                "config.clusters[0].token_secret": secrets["proxmox_token_secret"],
                "config.clusters[0].region": region,
                "config.features.provider": "default",
                # The legacy flat keys (storageclass.name,
                # region, zone, csi.lvm.thinPool) were the
                # chart's OLDER schema (chart <0.4.x). Chart
                # 0.5.x uses the structured `storageClass: []`
                # list. The values below match the 0.5.x
                # schema; the old keys are ignored.
                #
                # The chart's StorageClass template has no
                # built-in `default` -> is-default-class
                # translation (the `default: true` key on
                # the storageClass list is dead code in
                # 0.5.9). To make the SC the cluster default
                # we have to set the annotation explicitly.
                "storageClass[0].name": "proxmox-lvm-thin",
                "storageClass[0].region": region,
                "storageClass[0].zone": zone,
                "storageClass[0].storage": "data1",
            },
            # --set-string for the annotation key. helm --set
            # auto-coerces `true` -> bool, which fails to render
            # the annotation (`json: cannot unmarshal bool into
            # Go struct field ObjectMeta.metadata.annotations of
            # type string`). --set-string preserves the string
            # type so the annotation renders as `"true"`.
            set_strings={
                "storageClass[0].annotations.storageclass\\.kubernetes\\.io/is-default-class": "true",
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


# ---------- WP07: Envoy Gateway (GatewayClass=envoy implementation) ----------
#
# Pinned to v1.8.2 (latest stable on
# oci://docker.io/envoyproxy/gateway-helm as of 2026-07-08; see
# specs/001-build-a-kubernetes-k3s-cluster-on-proxmo/research-log-v8.json
# for the WP00 context7-auto-research evidence).
#
# The chart normally installs the upstream standard Gateway API CRDs
# itself (crds.enabled=true). We disable that and apply the CRDs
# separately in the `gateway_crds` bootstrap phase (pinned at
# v1.6.0 via kubectl --server-side), so a CRD drift surfaces as a
# `kubectl diff` rather than a silent helm-upgrade side-effect.
#
# GatewayClass=envoy is the chart default; we pin it explicitly so
# the test_remaining_releases contract test catches any future
# upstream rename.
#
# service.type=ClusterIP is the chart default and is correct for a
# cluster without a LoadBalancer provisioner (the live Proxmox+k3s
# clusters have none). The data-plane Service is reachable from
# inside the cluster on its ClusterIP; the gateway_smoke phase
# curls that ClusterIP, not a public hostname.


def gateway_releases() -> list[HelmRelease]:
    """WP07: Envoy Gateway as the GatewayClass=envoy implementation.

    Returns a single HelmRelease. The bootstrap script's `_run_helm`
    installs it after `remaining_releases` in the same
    `install_or_upgrade` call so a single helm run lands every
    release atomically (and the per-release audit log line is
    preserved).

    Values are minimal: the defaults cover k8s 1.28+, Cilium 1.16.1
    coexistence, and the GatewayClass name the GitLab chart's
    templates expect.

    Pinned against the live host (cicd + apps clusters,
    2026-07-08 cross_check in tools/versions.lock.yaml). The chart
    OCI ref + values were verified via context7-auto-research and a
    registry probe (research-log-v8.json).
    """
    return [
        HelmRelease(
            name="envoy-gateway",
            chart="oci://docker.io/envoyproxy/gateway-helm",
            namespace="envoy-gateway-system",
            version="v1.8.2",
            values={
                # We install the standard CRDs ourselves (see
                # bootstrap_cluster._run_gateway_crds), so disable
                # the chart's own CRD install and the safe-upgrade
                # policy to avoid a CRD-version drift between the
                # two paths.
                "crds.enabled": "false",
                "crds.gatewayAPI.safeUpgradePolicy.enabled": "false",
                # Pin the controller name explicitly so a chart
                # default change surfaces as a contract-test
                # failure.
                "config.envoyGateway.gateway.controllerName": (
                    "gateway.envoyproxy.io/gatewayclass-controller"
                ),
                # ClusterIP (default; explicit because we don't
                # have a LoadBalancer provisioner and want drift
                # visible).
                "service.type": "ClusterIP",
                # Single replica is correct for a 1-CP cluster;
                # HPA is off by default.
                "deployment.replicas": "1",
            },
        ),
    ]


# Standard-channel Gateway API CRD URL pinned at v1.6.0 (operator
# decision 2026-07-08: "pin them"). Applied by the bootstrap's
# `gateway_crds` phase via `kubectl apply --server-side`, which is
# idempotent (server resolves conflicts; re-apply is a no-op).
#
# Why pin and ship the YAML at runtime (vs. vendoring under
# infra/): the file is 20K lines, gitignored by our .gitignore for
# third-party blobs, and we want a single source of truth so
# `kubectl diff` after a future v1.7.0 release points exactly at
# the upstream tarball. If we ever need offline-install support,
# vendor the file at infra/clusters/<name>/manifests/_pinned/
# gateway-api-v1.6.0-standard-install.yaml and switch the constant
# below to a `Path` argument.
GATEWAY_API_STANDARD_CRDS_URL = (
    "https://github.com/kubernetes-sigs/gateway-api/releases/"
    "download/v1.6.0/standard-install.yaml"
)