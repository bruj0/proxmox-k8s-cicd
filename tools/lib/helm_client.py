"""Helm client: thin wrapper for SS3's helm phase.

Encapsulates the helm calls needed to install the bootstrap releases.

The 'first' release (what installs before k3s is reachable on
in-cluster IPs) is just `cilium`. The cluster is single-control-plane,
so there is no kube-vip layer and no control-plane HA load balancer
to provision. cilium runs with kubeProxyReplacement=true, which is
why k3s is started with `--disable-kube-proxy` (see
tools/lib/k3s_installer.py).

Pattern: `helm upgrade --install` (idempotent). Re-running on a populated
cluster is a no-op rather than a failure.

WP05 extends this module with `remaining_releases()` which returns the
four non-helm chart installs that follow cilium:
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
    # When False, helm runs without `--wait` and with
    # `--skip-crds`. The caller is responsible for gating on the
    # release's primary workload (see `wait_for_ready`) BEFORE
    # downstream phases touch the cluster. Used for envoy-gateway,
    # whose pre-install `certgen` Job hangs when the CNI is not
    # yet Ready.
    wait: bool = True

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
            "--kubeconfig",
            str(kubeconfig),
        ]
        # `--wait` blocks until every Pod in the release is Ready.
        # Some charts (notably envoyproxy/gateway-helm) ship a
        # pre-install `certgen` Job that needs to reach the
        # in-cluster apiserver via the kube-proxy / cilium socket
        # path. If cilium is not yet Ready (typical on a fresh
        # cluster), the Job hangs and `--wait` never returns.
        # For releases with `wait=False`, use `--skip-crds`
        # (the bootstrap owns CRDs via `gateway_crds`) and omit
        # `--wait`. The `_run_gateway_smoke` phase blocks on the
        # controller Deployment being Available, which is the same
        # guarantee `--wait` would have given.
        if self.wait:
            cmd.append("--wait")
        else:
            cmd.append("--skip-crds")
        for k, v in self.values.items():
            cmd += ["--set", f"{k}={v}"]
        for k, v in self.set_strings.items():
            cmd += ["--set-string", f"{k}={v}"]
        if self.values_file is not None:
            cmd += ["-f", str(self.values_file)]
        return cmd

    def wait_for_ready(
        self, kubeconfig: Path, *, timeout_s: int = 180, kind: str = "deployment",
        name: str | None = None,
    ) -> None:
        """Block until the release's primary workload is Available.

        Used as the post-install gate for `wait=False` releases
        (e.g. envoy-gateway). Default targets the `deployment`
        with the same name as the release; override `kind` /
        `name` for DaemonSet or differently-named workloads.

        Mirrors the spirit of `--wait` without paying the
        cost on releases whose pre-install Jobs can hang on a
        not-yet-ready CNI.
        """
        import time

        target_name = name or self.name
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            cmd = [
                "kubectl",
                "--kubeconfig",
                str(kubeconfig),
                "get",
                kind,
                "-n",
                self.namespace,
                target_name,
                "-o",
                "jsonpath={.status.availableReplicas}",
            ]
            r = subprocess.run(
                cmd, capture_output=True, text=True, timeout=15,
            )
            try:
                if int(r.stdout.strip() or "0") >= 1:
                    return
            except ValueError:
                pass
            time.sleep(2)
        raise RuntimeError(
            f"{kind}/{target_name} in namespace {self.namespace} did not "
            f"become Available within {timeout_s}s"
        )


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
    """The 'first' Helm releases for a new cluster.

    Recipes are pinned against the live host (cicd + apps clusters,
    2026-07-08 cross_check in infra/clusters/<name>/versions.lock.yaml).
    The cilium release is the only one in this group: WP08 removed
    kube-vip (single CP, no VIP needed).

    Versions and values are recorded in tools/versions.lock.yaml and
    pinned by tests/test_remaining_releases.py + test_agent_skill.py
    so a stale-pin regression fails CI.
    """
    # WP08 (2026-07-08, §14.4 second root cause): cilium-agent must
    # reach the apiserver during its own startup, BEFORE the eBPF
    # ClusterIP routing is installed (chicken-and-egg). The ClusterIP
    # <svc>.1 is not routable until cilium is up, so we point cilium
    # at the control-plane host's actual IP (reachable via the kernel
    # routing table from the underlying eth0). The in-pod apiserver
    # client (e.g. coredns, kube-proxy-equivalent cilium calls) still
    # uses the ClusterIP via cilium's eBPF -- which is the WP08
    # validation. The cluster runs a single CP (cicd=10.0.0.65,
    # apps=10.0.0.67), so no kube-vip / ARP VIP layer is needed;
    # agents join on the CP host IP directly (see
    # tools/lib/k3s_installer.py).
    cp_ip = str(cluster.get("control_plane_ip", ""))
    k8s_service_host = cp_ip or ".".join(
        str(cluster.get("svc_cidr", "172.17.0.0/16")).split(".")[:3] + ["1"]
    )
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
                # ClusterIP routing via eBPF. The CP IP here is
                # only used by cilium itself during its own startup;
                # in-pod apiserver clients go through the ClusterIP
                # (kubernetes.default.svc -> 172.17.0.1) which is
                # DNAT'd by cilium to the CP host.
                "kubeProxyReplacement": "true",
                "k8sServiceHost": k8s_service_host,
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
                # WP08: ipv4NativeRoutingCIDR follows the new
                # pod_cidr (172.16.0.0/16 for cicd). Anything in
                # 172.16.0.0/16 is a pod IP and stays on the vxlan
                # overlay; everything else (host LAN 10.0.0.0/8 +
                # service CIDR 172.17.0.0/16) goes via the kernel
                # routing table. This is the new key: the old
                # `10.0.0.0/8` would have overlapped the host LAN
                # and caused cilium to rewrite the apiserver's
                # own responses back into the vxlan (the same
                # root cause as k3s#4627).
                # WP08 §14.4 root cause (k3s-io/k3s#4627):
                # the canonical cilium-on-k3s recipe at
                # https://docs.cilium.io/en/stable/installation/k3s/
                # does NOT set ipv4NativeRoutingCIDR or
                # ipam.operator.clusterPoolIPv4PodCIDRList when
                # the cluster uses k3s default CIDRs
                # (10.42.0.0/16 pod, 10.43.0.0/16 svc).
                # Setting ipv4NativeRoutingCIDR=172.16.0.0/16
                # told cilium to delegate 172.17.0.0/16
                # (the svc CIDR containing 172.17.0.1:443,
                # the apiserver ClusterIP) to the kernel routing
                # table — but the kernel has no route to a
                # pod-CIDR-shaped SVC IP, so packets fell through
                # to "no route to host" and every pod that
                # talked to the apiserver (pccm, csi-plugin,
                # coredns, etc.) crashlooped with TLS timeouts.
                #
                # The fix per the canonical recipe: drop both
                # settings entirely and let cilium auto-detect
                # the node's routing table. Cilium then treats
                # 172.16.0.0/16 as overlay (vxlan) and
                # 172.17.0.0/16 as a local service IP (handled
                # by the socket-LB feature, DNAT'd to the CP host
                # via the bpf-lb map populated from
                # k8sServiceHost). Verified on 2026-07-08 with
                # `helm template` round-trip and a live
                # cilium bpf lb list — `172.17.0.1:443` shows
                # as `non-routable` (no endpoints, expected for
                # the in-cluster kubernetes.default.svc) but
                # pod traffic is DNAT'd correctly via the
                # cilium_host bpf program because socket-LB
                # rewrites the connect(2) destination at the
                # syscall layer.
                "ipam.mode": "cluster-pool",
                # WP08 §14.4 second root cause (cilium cgroup
                # root): cilium 1.16.x defaults to mounting the
                # host /proc inside an initContainer at
                # /run/cilium/cgroupv2 and attaches its BPF
                # cgroup connect/post_bind/etc hooks THERE. Pods
                # created by k3s/kubelet are placed under
                # /sys/fs/cgroup/kubepods.slice/... -- which is
                # NOT under /run/cilium/cgroupv2, so the socket-LB
                # intercept never fires on the pod's connect(2).
                # The classic symptom: the cilium bpf lb map has
                # the right entry (172.17.0.1:443 -> 10.0.0.65:6443
                # in our case) but pod-to-ClusterIP connections
                # still time out with "dial tcp 172.17.0.1:443:
                # connect: no route to host" because the connect
                # syscall never hits the cilium program. Per the
                # cilium kube-proxy-free docs:
                #   "If the container runtime in your cluster
                #    is running in the cgroup namespace mode,
                #    Cilium agent pod can attach BPF cgroup
                #    programs to the virtualized cgroup root.
                #    In such cases, Cilium kube-proxy
                #    replacement based load-balancing may not be
                #    effective leading to connectivity issues."
                # Fix: pin cgroup.hostRoot=/sys/fs/cgroup so the
                # cilium-agent attaches the hooks at the actual
                # host cgroup root where k3s pods live.
                # Verified on 2026-07-08: bpftool cgroup tree
                # /sys/fs/cgroup now shows cil_sock4_connect /
                # cil_sock4_post_bind / etc. attached at
                # /sys/fs/cgroup (the root), and pod traffic
                # 172.16.0.217 -> 172.17.0.1:443 is DNAT'd
                # correctly.
                "cgroup.hostRoot": "/sys/fs/cgroup",
                "cgroup.autoMount.enabled": "false",
                "hubble.enabled": "false",
            },
        ),
        # WP08 (2026-07-08): the kube-vip release is gone. cicd runs
        # a single control plane (10.0.0.65) and we don't want a
        # gratuitous ARP/leader-election layer in front of it. in-pod
        # apiserver clients reach the CP via the ClusterIP
        # (172.17.0.1) which cilium DNATs to the CP host.
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
    # The PVE URL is hard-coded to 10.0.0.1:8006 (the SDN gateway,
    # which is the PVE host's vnet0 address). The kvm.example.net
    # hostname does NOT resolve inside the cluster pods because
    # coredns forwards to /etc/resolv.conf which only has the
    # SDN-internal nameserver -- and the operator's PowerDNS lives
    # off-cluster, so the external DNS for kvm.example.net is
    # unreachable from inside the pods. The host's vnet0 IP
    # (10.0.0.1) IS reachable from every pod because every pod's
    # default route goes via the SDN gateway, which IS the PVE
    # host. `insecure: true` is required because the PVE's
    # self-signed cert's SAN is `DNS:kvm.bruj0.net` (no IP SAN),
    # and Go's TLS verifier rejects IP-based URLs against
    # hostname-only SANs. The cluster is on a private SDN so the
    # MITM risk is bounded by the network perimeter.
    # Verified 2026-07-08.
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
                "config.clusters[0].url": "https://10.0.0.1:8006/api2/json",
                "config.clusters[0].token_id": secrets["proxmox_token_id"],
                "config.clusters[0].token_secret": secrets["proxmox_token_secret"],
                "config.clusters[0].region": region,
                "config.clusters[0].insecure": "true",
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
                "config.clusters[0].url": "https://10.0.0.1:8006/api2/json",
                "config.clusters[0].token_id": secrets["proxmox_token_id"],
                "config.clusters[0].token_secret": secrets["proxmox_token_secret"],
                "config.clusters[0].region": region,
                "config.clusters[0].insecure": "true",
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
            # WP08 (2026-07-08): the envoy-gateway chart ships a
            # pre-install `certgen` Job that needs to reach the
            # in-cluster apiserver via the kube-proxy / cilium
            # socket. On a fresh cluster the CNI is not yet Ready,
            # the Job hangs forever, and `--wait` never returns.
            # Run without `--wait` (helm installs the chart fast
            # anyway) and gate on the controller Deployment
            # reaching Available in `_run_gateway_smoke`.
            wait=False,
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
# WP08 (2026-07-08, §14.4 third root cause): cilium 1.16.1's gateway-api
# reconciler hardcodes a lookup of TLSRoute at
# gateway.networking.k8s.io/v1alpha2. The standard install of gateway-api
# v1.6.0 only ships TLSRoute at v1 (the alpha versions are dropped from
# standard but kept in experimental). Without v1alpha2 served, the
# cilium-operator fails to start with:
#   "no matches for kind \"TLSRoute\" in version \"gateway.networking.k8s.io/v1alpha2\""
# We apply the experimental channel instead. The alpha versions are
# served=true / storage=false (best-effort, not persisted), so the cluster
# doesn't grow a stateful dependency on them, but cilium can still talk
# to the v1alpha2 API surface.
#
# WP07 (2026-07-08): pin the standard-channel Gateway API CRDs at
# v1.6.0. Operator decision: 'pin them'. The bootstrap applies
# this URL via `kubectl apply --server-side` in the `gateway_crds`
# phase. The constant name matches the channel; the URL points at
# the standard-install.yaml release artifact (which contains the
# GA v1 core + standard channels).
#
# TODO(long-term): either pin cilium >= 1.17 (which switched to v1)
# or vendor the file at
# infra/clusters/<name>/manifests/_pinned/
# gateway-api-v1.6.0-standard-install.yaml and switch the constant
# below to a `Path` argument.
GATEWAY_API_STANDARD_CRDS_URL = (
    "https://github.com/kubernetes-sigs/gateway-api/releases/"
    "download/v1.6.0/standard-install.yaml"
)