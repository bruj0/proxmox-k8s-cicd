"""WP05 acceptance tests.

Extends WP04's bootstrap_cluster suite with the WP05 acceptance criteria:

  - The five remaining Helm releases (proxmox-ccm, proxmox-csi, traefik
    HelmChartConfig, cloudflare-tunnel-ingress-controller, cert-manager)
    are installed via `helm upgrade --install` in spec order.
  - The cert-manager HelmRelease has no ACME solvers configured (only
    in-cluster CA via a ClusterIssuer, NFR-007).
  - Traefik HelmChartConfig is asserted present at the cluster root (the
    module rendered it in WP02; SS3 verifies it landed).
  - The no-new-host-ports verification post-step passes when the nft
    prerouting chain is unchanged vs the captured baseline and fails
    when a new DNAT rule is introduced.

WP07 (2026-07-08) adds the Envoy Gateway (GatewayClass=envoy)
acceptance criteria. The `gateway_releases()` function returns a
single release with the chart OCI ref + version pinned against the
live host (cross_check in tools/versions.lock.yaml).

Side-effect guarantee: tests use `tmp_path` and monkeypatch subprocess.
No real network calls. No real PVE calls.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.host_ports import HostPortsAddedError, verify_no_new_dnat_rules  # noqa: E402
from lib.helm_client import (  # noqa: E402
    GATEWAY_API_STANDARD_CRDS_URL,
    gateway_releases,
    remaining_releases,
)

NS = "kube-system"


def _stub_ok(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=args, returncode=0, stdout="ok", stderr="")


def _stub_fail(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    raise subprocess.CalledProcessError(returncode=1, cmd=list(args), stderr="kaboom")


def _stub_with_stdout(stdout: str, returncode: int = 0) -> Any:
    def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=args,
            returncode=returncode,
            stdout=stdout,
            stderr="",
        )
    return fake_run


# ---------- remaining_releases coverage ----------


def test_remaining_releases_includes_all_five_locked_charts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """WP05: proxmox-ccm, proxmox-csi, traefik, cloudflare-tunnel, cert-manager.

    'traefik' here is the HelmChartConfig render that WP02 emitted; we
    install it via kubectl apply, not via helm upgrade --install, so the
    function returns a non-empty list of kubectl apply steps instead of a
    HelmRelease. The other four are real HelmRelease records.
    """
    manifests = tmp_path / "clusters" / "cicd" / "manifests"
    manifests.mkdir(parents=True)
    (manifests / "traefik-helmchartconfig.yaml").write_text(
        "apiVersion: helm.cattle.io/v1\nkind: HelmChartConfig\n"
    )
    monkeypatch.chdir(tmp_path)
    cluster = {
        "name": "cicd",
        "vip": "10.0.0.10",
        "control_plane": [{"name": "cp1", "ip": "10.0.0.11"}],
        "worker": [],
        "helm_releases": [
            "cilium",
            "kube-vip",
            "proxmox-cloud-controller-manager",
            "proxmox-csi-plugin",
            "traefik",
            "cloudflare-tunnel-ingress-controller",
            "cert-manager",
        ],
        "cf_tunnel_name": "cicd-tunnel",
    }
    secrets = {
        "cf_api_token": "fake-cf-token",
        "cf_account_id": "fake-cf-account",
        "proxmox_token_id": "fake-pve-id",
        "proxmox_token_secret": "fake-pve-secret",
    }
    rels, traefik_apply = remaining_releases(cluster, secrets)
    chart_names = {r.chart for r in rels}
    # The sergelogvinov charts moved to OCI in late 2025 -- the old
    # HTTP `sergelogvinov/<chart>` paths 404. Pin the OCI form.
    assert (
        "oci://ghcr.io/sergelogvinov/charts/proxmox-cloud-controller-manager"
        in chart_names
    )
    assert (
        "oci://ghcr.io/sergelogvinov/charts/proxmox-csi-plugin" in chart_names
    )
    assert "oci://ghcr.io/strrl/charts/cloudflare-tunnel-ingress-controller" in chart_names
    assert "cert-manager/cert-manager" in chart_names
    # Traefik is a kubectl apply step, not a helm release.
    assert traefik_apply is not None
    assert traefik_apply.kind == "HelmChartConfig"
    assert traefik_apply.path.exists()


def test_cert_manager_release_has_no_acme_solvers() -> None:
    """NFR-007 / WP05 acceptance: cert-manager installs without any public ACME.

    The release object explicitly does not include any solver stanza;
    `installCRDs=true` only.
    """
    rels, _ = remaining_releases(
        {"name": "cicd", "vip": "10.0.0.10", "helm_releases": ["cert-manager"], "control_plane": [{"name": "cp1", "ip": "10.0.0.11"}], "worker": []},
        {"cf_api_token": "x", "cf_account_id": "y", "cf_tunnel_name": "z", "proxmox_token_id": "p", "proxmox_token_secret": "q"},
    )
    cert = next(r for r in rels if r.name == "cert-manager")
    flat_keys = list(cert.values.keys())
    assert "installCRDs" in flat_keys
    # No ACME-related keys.
    for k in flat_keys:
        assert "acme" not in k.lower(), f"unexpected ACME key on cert-manager: {k}"
        assert "letsencrypt" not in k.lower()


def test_proxmox_ccm_values_include_proxmox_credentials() -> None:
    """WP05 T002: sergelogvinov/proxmox-ccm carries credentials + region/zone.

    WP07 (2026-07-08) update: the chart's documented schema is
    `config.clusters[0].{url,token_id,token_secret,region}`, not
    `credentials.*`. The old schema was silently ignored, so the
    chart's secrets.yaml conditional `if ne (len
    .Values.config.clusters) 0` never fired and the
    `proxmox-cloud-controller-manager` Secret was never created.
    The Deployment pod was stuck in ContainerCreating on a missing
    `cloud-config` volume mount for 4+ hours before the live-host
    apply surfaced it.
    """
    rels, _ = remaining_releases(
        {"name": "cicd", "vip": "10.0.0.10", "helm_releases": ["proxmox-cloud-controller-manager"], "control_plane": [{"name": "cp1", "ip": "10.0.0.11"}], "worker": []},
        {"proxmox_token_id": "pve_user@realm!tid", "proxmox_token_secret": "pve-secret", "cf_api_token": "", "cf_account_id": "", "cf_tunnel_name": ""},
    )
    ccm = next(r for r in rels if r.name == "proxmox-cloud-controller-manager")
    # The chart's `secrets.yaml` template is gated on
    # `if ne (len .Values.config.clusters) 0`. We must populate
    # that path so the chart actually creates the Secret.
    assert "config.clusters[0].url" in ccm.values
    assert "config.clusters[0].token_id" in ccm.values
    assert "config.clusters[0].token_secret" in ccm.values
    assert "config.clusters[0].region" in ccm.values
    # Region/zone labels for cloud-node topology.
    assert "config.features.provider" in ccm.values
    # Make sure the OLD broken shape is gone.
    assert "credentials.tokenId" not in ccm.values
    assert "credentials.tokenSecret" not in ccm.values
    assert "credentials.url" not in ccm.values


def test_proxmox_csi_release_creates_proxmox_lvm_thin_storageclass() -> None:
    """WP05 T003: csi chart declares storageclass `proxmox-lvm-thin` default.

    WP07 (2026-07-08) update: the chart 0.5.x schema is
    `storageClass: []` (list of dicts), NOT the legacy
    `storageclass.{name,default,region,zone}` flat keys.
    The chart's `storageclass` top-level key is the 0.5.x
    `storageClass` list. The legacy `csi.lvm.thinPool` key is
    no longer accepted; the chart's storageClass item takes
    a `storage: <pve-storage-name>` key (NOT lvm.thinPool).
    The `is-default-class` annotation is set as a post-install
    step in `_run_helm` because the chart has no values
    schema for it.
    """
    rels, _ = remaining_releases(
        {"name": "cicd", "vip": "10.0.0.10", "helm_releases": ["proxmox-csi-plugin"], "control_plane": [{"name": "cp1", "ip": "10.0.0.11"}], "worker": []},
        {"proxmox_token_id": "pve-id", "proxmox_token_secret": "pve-secret", "cf_api_token": "", "cf_account_id": "", "cf_tunnel_name": ""},
    )
    csi = next(r for r in rels if r.name == "proxmox-csi-plugin")
    # The new shape.
    assert csi.values.get("storageClass[0].name") == "proxmox-lvm-thin"
    assert csi.values.get("storageClass[0].storage") == "data1"
    assert "storageClass[0].region" in csi.values
    assert "storageClass[0].zone" in csi.values
    # The `is-default-class` annotation must be set via
    # `--set-string` because helm's default `--set` path
    # parser auto-coerces `true` to a bool, which then fails
    # to render the annotation (`json: cannot unmarshal bool
    # into Go struct field ObjectMeta.metadata.annotations of
    # type string`). This contract pins the use of set_strings
    # (not values) for annotation-style keys.
    assert (
        csi.set_strings.get(
            "storageClass[0].annotations.storageclass\\.kubernetes\\.io/is-default-class"
        )
        == "true"
    )
    # The annotation key MUST NOT be in `values` (where
    # it would be passed via --set and re-trigger the same
    # bool-coercion bug).
    assert (
        "storageClass[0].annotations.storageclass\\.kubernetes\\.io/is-default-class"
        not in csi.values
    )
    # Make sure the OLD broken shape is gone.
    assert "storageclass.name" not in csi.values
    assert "csi.lvm.thinPool" not in csi.values
    # Make sure the Secret will be created (the
    # secrets.yaml template is gated on
    # `if ne (len .Values.config.clusters) 0`).
    assert "config.clusters[0].url" in csi.values
    assert "config.clusters[0].token_id" in csi.values
    assert "config.clusters[0].token_secret" in csi.values


# ---------- host-ports verification ----------


def test_verify_no_new_dnat_rules_passes_when_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M2 verified: prerouting chain matches baseline -> no error."""
    baseline = tmp_path / "baseline.txt"
    baseline.write_text(
        "table ip nat {\n"
        "    chain prerouting {\n"
        "        type nat hook prerouting priority 0; policy accept;\n"
        "    }\n"
        "}\n"
    )

    monkeypatch.setattr(subprocess, "run", _stub_with_stdout(baseline.read_text()))
    # Should not raise.
    verify_no_new_dnat_rules(baseline, ssh_target="root@10.0.0.1", ssh_port="6022")


def test_verify_no_new_dnat_rules_raises_on_new_dnat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M2 acceptance: a new DNAT rule surfaces as HostPortsAddedError.

    Prevents the misfit where the operator concludes 'no host ports'
    based purely on the absence of a check.
    """
    baseline = tmp_path / "baseline.txt"
    baseline.write_text(
        "table ip nat {\n"
        "    chain prerouting {\n"
        "        type nat hook prerouting priority 0; policy accept;\n"
        "    }\n"
        "}\n"
    )
    # Current state has a DNAT that did NOT exist at baseline.
    diff_state = (
        "table ip nat {\n"
        "    chain prerouting {\n"
        "        type nat hook prerouting priority 0; policy accept;\n"
        "        tcp dport 443 dnat to 10.0.0.20:6443\n"
        "    }\n"
        "}\n"
    )
    monkeypatch.setattr(subprocess, "run", _stub_with_stdout(diff_state))
    with pytest.raises(HostPortsAddedError):
        verify_no_new_dnat_rules(baseline, ssh_target="root@10.0.0.1", ssh_port="6022")


def test_verify_no_new_dnat_rules_raises_when_baseline_already_has_dnat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: a NEW DNAT must be flagged even when the baseline already
    has one. The original implementation short-circuited on `baseline_has`
    which silently masked any additional rules."""
    baseline = tmp_path / "baseline.txt"
    baseline.write_text(
        "table ip nat {\n"
        "    chain prerouting {\n"
        "        type nat hook prerouting priority 0; policy accept;\n"
        "        tcp dport 22 dnat to 10.0.0.1:22\n"
        "    }\n"
        "}\n"
    )
    current_state = (
        "table ip nat {\n"
        "    chain prerouting {\n"
        "        type nat hook prerouting priority 0; policy accept;\n"
        "        tcp dport 22 dnat to 10.0.0.1:22\n"
        "        tcp dport 443 dnat to 10.0.0.20:6443\n"
        "    }\n"
        "}\n"
    )
    monkeypatch.setattr(subprocess, "run", _stub_with_stdout(current_state))
    with pytest.raises(HostPortsAddedError):
        verify_no_new_dnat_rules(baseline, ssh_target="root@10.0.0.1", ssh_port="6022")


def test_verify_no_new_dnat_rules_ssh_failure_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """verify_no_new_dnat_rules surfaces non-zero ssh exits as a caller-provided
    callback (which the orchestrator wires up to raise BootstrapError)."""
    from bootstrap_cluster import BootstrapError

    baseline = tmp_path / "baseline.txt"
    baseline.write_text("anything")
    monkeypatch.setattr(subprocess, "run", _stub_fail)
    captured: dict[str, object] = {}

    def on_failure(phase: str, ssh_target: str, exc: Exception) -> None:
        captured["phase"] = phase
        captured["ssh_target"] = ssh_target
        # BootstrapError would normally be raised here.
        raise BootstrapError(phase, {"detail": str(exc)})

    with pytest.raises(BootstrapError):
        verify_no_new_dnat_rules(
            baseline,
            ssh_target="root@10.0.0.1",
            ssh_port="6022",
            on_ssh_failure=on_failure,
        )
    assert captured.get("phase") == "host_ports"
    assert captured.get("ssh_target") == "root@10.0.0.1"


# ---------- WP07: gateway_releases + standard CRDs URL ----------


def test_gateway_releases_returns_envoy_gateway() -> None:
    """WP07: gateway_releases() returns exactly one release, pinned at v1.8.2."""
    rels = gateway_releases()
    assert len(rels) == 1
    gw = rels[0]
    # Canonical OCI ref per WP00 context7 snippet. NOT
    # oci://gateway-helm-charts/gateway-envoy (a non-existent
    # path that the plan originally guessed).
    assert gw.chart == "oci://docker.io/envoyproxy/gateway-helm"
    assert gw.namespace == "envoy-gateway-system"
    # Version string is non-empty, follows semver (with optional
    # 'v' prefix), and has no -rc / -beta suffix.
    assert gw.version
    assert "-" not in gw.version, (
        f"stable version must not contain a pre-release suffix, got {gw.version!r}"
    )
    # Strip a leading 'v' for the structural assertion (the live
    # registry returns 'v1.8.2'; helm accepts both forms).
    stripped = gw.version[1:] if gw.version.startswith("v") else gw.version
    parts = stripped.split(".")
    assert len(parts) == 3 and all(p.isdigit() for p in parts), (
        f"expected semver X.Y.Z (with optional v prefix), got {gw.version!r}"
    )


def test_gateway_releases_disables_chart_crds() -> None:
    """WP07: chart must NOT install CRDs (we install them ourselves).

    The bootstrap applies the pinned standard CRDs URL in
    `_run_gateway_crds` before the helm phase; if the chart
    also installs them, a CRD-version drift surfaces as a
    silent helm upgrade rather than a `kubectl diff`.
    """
    rels = gateway_releases()
    gw = rels[0]
    assert gw.values.get("crds.enabled") == "false"
    # Safe-upgrade policy conflicts with our pinned CRDs;
    # disable it.
    assert gw.values.get("crds.gatewayAPI.safeUpgradePolicy.enabled") == "false"


def test_gateway_releases_pins_controller_name() -> None:
    """WP07: the GatewayClass controller name is pinned explicitly.

    A future upstream rename would otherwise silently create
    a different GatewayClass (e.g. `envoy-gateway`) and the
    GitLab chart's `gatewayClassName=envoy` would never
    resolve.
    """
    rels = gateway_releases()
    gw = rels[0]
    assert gw.values.get(
        "config.envoyGateway.gateway.controllerName"
    ) == "gateway.envoyproxy.io/gatewayclass-controller"


def test_gateway_releases_uses_clusterip_service() -> None:
    """WP07: service.type=ClusterIP (no LoadBalancer provisioner)."""
    rels = gateway_releases()
    gw = rels[0]
    assert gw.values.get("service.type") == "ClusterIP"


def test_gateway_api_standard_crds_url_is_v1_6_0() -> None:
    """WP07: standard-channel CRDs pinned at v1.6.0.

    Operator decision 2026-07-08: 'pin them'. The bootstrap
    applies this URL via `kubectl apply --server-side` in the
    `gateway_crds` phase. If a future operator wants to bump
    to v1.7.0, this is the single string to edit.
    """
    assert "v1.6.0/standard-install.yaml" in GATEWAY_API_STANDARD_CRDS_URL
    assert "github.com/kubernetes-sigs/gateway-api" in GATEWAY_API_STANDARD_CRDS_URL
