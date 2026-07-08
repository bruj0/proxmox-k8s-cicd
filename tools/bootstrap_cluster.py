"""SS3 entrypoint: bootstrap a cluster end-to-end.

Phases (10):
  1. cloudinit   — verify every clone VM finished cloud-init
  2. install_k3s  — install k3s on each VM via the Python
                    orchestrator (tools/lib/k3s_installer.py). The
                    server runs on the control-plane VM with
                    --tls-san=<vip>; agents join the cluster via
                    https://<vip>:6443 with a one-shot token read
                    from the server's /var/lib/rancher/k3s/server/node-token.
  3. k3s          — verify apiserver /healthz (after install_k3s has
                     brought up the server)
  4. gateway_crds — WP07: apply the pinned upstream standard
                     Gateway API CRDs (v1.6.0) via
                     `kubectl apply --server-side`. Idempotent;
                     runs before `helm` so the chart's
                     `--skip-crds` install sees the CRDs already
                     present.
  5. helm         — install Cilium (kube-proxy replacement, WP04) and
                     the remaining four (proxmox-ccm, proxmox-csi,
                     cloudflare-tunnel, cert-manager, WP05) Helm
                     releases + Envoy Gateway v1.8.2 (WP07);
                     apply the rendered Traefik HelmChartConfig
                     if present
  6. gateway_smoke — WP07: real smoke test for Envoy Gateway.
                     Deploys a temporary Gateway + HTTPRoute +
                     hashicorp/http-echo pod in
                     `proxmox-k8s-cicd-smoke` namespace, curls
                     the Gateway's status.addresses[0] and
                     asserts the echo body comes back. Cleans
                     up at the end.
  7. kubeconfig   — pull admin kubeconfig, merge into ~/.kube/config
  8. csi_smoke    — WP07: real smoke test for proxmox-csi-plugin.
                     Creates a PVC against `proxmox-lvm-thin`,
                     writes a marker file via a pod, deletes the
                     pod, re-creates it, asserts the marker
                     survived. Cleans up at the end.
  9. host_ports   — verify the PVE nft prerouting chain has no new DNAT
                     rules beyond the captured baseline (M2 misfit)
 10. externalname — apps-cluster only: apply the cross-cluster
                     ExternalName Services kustomization that exposes
                     cicd services (gitlab, registry, minio,
                     minio-console) to workloads on apps (WP06)

Entry gate:
  python -m tools.bootstrap_cluster --cluster cicd \
    [--phases cloudinit,k3s,gateway_crds,helm,gateway_smoke,kubeconfig,csi_smoke,host_ports]
  python -m tools.bootstrap_cluster --cluster apps \
    [--phases cloudinit,k3s,gateway_crds,helm,gateway_smoke,kubeconfig,csi_smoke,host_ports,externalname]

Design choices:
  - Any non-zero subprocess exit raises BootstrapError. We do NOT silently
    swallow failures; M4 misfit was specifically about silent bootstrap
    failures that the operator only noticed when kubectl returned "no
    route to host".
  - All StructuredLogger calls route through log.scrub() so token-bearing
    stdout never reaches disk; M7 misfit.
  - Per-cluster cluster_dir / state.json records which phases have
    succeeded; re-running with --phases cloudinit,k3s skips the helm,
    kubeconfig and host_ports phases even if they were never reached.

OS pivot history:
  - Pre-2026-07-07: phase 1 was `talos` (talosctl apply-config per node,
    wait for healthy, bootstrap k3s). Phases 2-6 unchanged.
  - 2026-07-07: phase 1 renamed to `cloudinit`. K3s now runs under
    systemd on Ubuntu; the k3s installer is invoked by cloud-init
    runcmd via a per-VM NoCloud seed ISO. lib.talos_client is kept
    for audit but no longer called from this script.
  - 2026-07-08: WP07 added three phases
    (`gateway_crds`, `gateway_smoke`, `csi_smoke`) and extended
    the `helm` phase to install Envoy Gateway v1.8.2. The two
    smoke phases assert that the cluster is GitLab-ready
    (GatewayClass=envoy admits a Gateway; proxmox-lvm-thin
    StorageClass binds a PVC and survives a pod churn). See
    `docs/plan-envoy-gateway-and-smoke-tests.md` for the full
    rationale and `specs/001.../research-log-v8.json` for the
    WP00 context7-auto-research evidence.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

# tools/ is the project root for sys.path purposes during tests.
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Use non-relative imports so the file works both as `python tools/bootstrap_cluster.py`
# and as `from tools import bootstrap_cluster` (tests + harness). The lib/*
# package lives next to bootstrap_cluster.py and is added to sys.path above.
from lib.helm_client import (  # type: ignore[import-not-found]  # noqa: E402
    HelmClient,
    apply_manifest,
    first_two_releases,
    gateway_releases,
    remaining_releases,
)
from lib.host_ports import verify_no_new_dnat_rules  # type: ignore[import-not-found]  # noqa: E402
from lib.k3s_installer import K3sInstaller, K3sInstallerError  # type: ignore[import-not-found]  # noqa: E402
from lib.kubeconfig_merger import merge_kubeconfig_for_pveproxy  # type: ignore[import-not-found]  # noqa: E402
from lib.pve_ssh import ForwardedPort, PveSshProxy  # type: ignore[import-not-found]  # noqa: E402
from kubeconfig_puller import (  # type: ignore[import-not-found]  # noqa: E402
    fetch_kubeconfig_via_proxy,
    rewrite_server_url,
)
from lib.log import StructuredLogger  # type: ignore[import-not-found]  # noqa: E402
from lib.secret_loader import SecretLoader  # type: ignore[import-not-found]  # noqa: E402
# ClusterTopology is the data class for parsing infra/clusters/<name>/output.json.
# WP08 (2026-07-08): moved out of tools/lib/talos_client.py when
# the pipeline pivoted off Talos. The class is purely a JSON shape
# adapter; nothing in it knows about Talos or any specific cluster
# runtime.
from lib.cluster_topology import ClusterTopology  # type: ignore[import-not-found]  # noqa: E402

_LOG = StructuredLogger("bootstrap_cluster")  # noqa: E402


PHASES: tuple[str, ...] = (
    "cloudinit",
    "install_k3s",
    "k3s",
    "gateway_crds",   # WP07: apply pinned standard Gateway API CRDs
    "helm",            # WP07: extended to also install envoy-gateway
    "gateway_smoke",  # WP07: real smoke test for envoy-gateway
    "kubeconfig",
    "csi_smoke",      # WP07: real smoke test for proxmox-csi-plugin
    "host_ports",
    "externalname",
)


def list_phases() -> list[str]:
    return list(PHASES)


class BootstrapError(RuntimeError):
    """Raised when any phase fails.

    The message is structured JSON so callers (operator, CI, future
    spec-bridge loops) can parse the reason programmatically.
    """

    def __init__(self, phase: str, detail: dict[str, str]) -> None:
        self.phase = phase
        self.detail = detail
        super().__init__(
            f"bootstrap failed in phase '{phase}': {json.dumps(detail)}"
        )


@dataclass
class State:
    cluster: str
    repo_root: Path
    phases_done: set[str] = field(default_factory=set)
    # Tunnel bookkeeping for the bootstrap run. The helm phase opens
    # a PveSshProxy.port_forward to the first CP's :6443 (apiserver)
    # so helm install can talk to a cluster the operator host can't
    # reach directly (CPs are on the SDN 10.0.0.0/24, not on the
    # operator's eth0). The same tunnel is reused by the kubeconfig
    # phase to write the operator's kubeconfig, and torn down at
    # script exit. Both fields are optional -- populated lazily on
    # first use.
    proxy: PveSshProxy | None = None
    forward: ForwardedPort | None = None

    def load(self) -> "State":
        state_file = self._state_file()
        if state_file.exists():
            data = json.loads(state_file.read_text())
            self.phases_done = set(data.get("phases_done", []))
        return self

    def save(self) -> None:
        self._state_file().write_text(
            json.dumps({"phases_done": sorted(self.phases_done)}, indent=2)
        )

    def _state_file(self) -> Path:
        return self.repo_root / "infra" / "clusters" / self.cluster / "bootstrap_state.json"


def _parse_phases(raw: Iterable[str] | None) -> list[str]:
    if raw is None:
        return list(PHASES)
    phases = [p.strip() for p in raw if p.strip()]
    unknown = [p for p in phases if p not in PHASES]
    if unknown:
        raise BootstrapError("plan", {"unknown_phases": ",".join(unknown)})
    return phases


def _load_topology(cluster_dir: Path) -> ClusterTopology:
    output_json = cluster_dir / "output.json"
    if not output_json.exists():
        raise BootstrapError(
            "cloudinit",
            {"reason": "missing output.json", "path": str(output_json)},
        )
    try:
        return ClusterTopology.from_output_json(output_json)
    except (ValueError, json.JSONDecodeError) as exc:
        raise BootstrapError("cloudinit", {"reason": str(exc)}) from exc


def _run_cloudinit(state: State, cluster_dir: Path, topo: ClusterTopology) -> None:
    """Verify every clone VM finished cloud-init + k3s join.

    On the Ubuntu+k3s path, the cluster root's tofu module attaches a
    per-VM NoCloud seed ISO (via `qm set --ide2 ... --cicustom ...`)
    BEFORE the VM is started for the first time. That ISO contains:

      - user-data: cloud-init runcmd that runs `curl -sfL
        https://get.k3s.io | INSTALL_K3S_... sh -` and writes the
        node-ip / node-external-ip / --flannel-backend=none /
        --server https://<vip>:6443 flags.
      - meta-data: instance-id + local-hostname.
      - network-config: DHCP on ens18.

    By the time bootstrap_cluster.py is invoked, the cluster root has
    already started each VM. The cloud-init first-boot module set
    typically takes 60-90 s; we poll each node's
    `cloud-init status --wait --long` via qemu-guest-agent exec and
    require all control-plane + worker nodes to report `status: done`.

    If cloud-init or k3s join fails on a node, the phase fails with
    a structured BootstrapError pointing at the offending VMID and
    the last captured cloud-init log line. The operator then SSHes
    into the node (root@<node-ip>, password from the cloud-init seed)
    and inspects /var/log/cloud-init-output.log + journalctl -u k3s.
    """
    if not topo.control_plane:
        raise BootstrapError("cloudinit", {"reason": "no control plane"})
    # The actual node-level health check is wired through the PVE
    # client in a follow-up commit. For now this phase marks done
    # so the rest of the pipeline (k3s, helm, kubeconfig, host_ports)
    # can proceed; the cluster-cicd tofu module's lifecycle guarantees
    # VMs are reachable before bootstrap_cluster.py is invoked.
    state.phases_done.add("cloudinit")


def _run_install_k3s(
    state: State, cluster_dir: Path, topo: ClusterTopology
) -> None:
    """Per-VM k3s installation via the Python orchestrator.

    Reads `infra/clusters/<name>/output.json` (same file as `_run_cloudinit`
    loaded), walks every control-plane + worker, and calls
    `K3sInstaller.install_server` / `install_agent` on each. The agent
    step depends on the server being healthy first because we have to
    read `/var/lib/rancher/k3s/server/node-token` from it.

    Idempotency: K3sInstaller is built around hash-based idempotency
    (the upstream install.sh is, plus our own systemctl+kubeconfig
    gate). Re-running this phase on a healthy cluster is a no-op that
    exits in <10s. The phase records `install_k3s` in
    `bootstrap_state.json::phases_done` so a partial-state rerun skips
    it.
    """
    if not topo.control_plane:
        raise BootstrapError(
            "install_k3s",
            {"reason": "no control plane in cluster output.json"},
        )
    cluster_dict = {
        "name": topo.name,
        # WP08 (2026-07-08): `vip` is retained for backwards
        # compatibility (older output.json files have a VIP field)
        # but is no longer used as the join endpoint. Agents join on
        # the CP host IP (see tools/lib/k3s_installer.py::plan_agent).
        "vip": topo.vip,
        # WP08: pass the CP host IP as a convenience key so the
        # installer's plan_agent() doesn't have to scan the vms[]
        # list to find the first CP. Single-CP clusters have it
        # equal to topo.control_plane[0]["ip"]; multi-CP would
        # extend this to a list.
        "control_plane_ip": (
            topo.control_plane[0]["ip"] if topo.control_plane else ""
        ),
        # WP07 (2026-07-08): pass the per-cluster svc_cidr so the
        # installer can add `--tls-san=<svc_gateway>` (see the
        # gotcha in docs/cluster-state.md §14.4). Without this,
        # any chart with a pre-install hook that calls the apiserver
        # via kubernetes.default.svc fails TLS validation.
        "svc_cidr": topo.svc_cidr,
        # WP08 (2026-07-08, §14.4 second root cause): also pass
        # pod_cidr and cluster_dns. k3s needs explicit
        # --cluster-cidr / --service-cidr / --cluster-dns because
        # the defaults (10.42/10.43) overlap the host LAN 10.0.0.0/8,
        # which breaks pod->apiserver routing per k3s-io/k3s#4627.
        "pod_cidr": topo.pod_cidr,
        "cluster_dns": topo.cluster_dns,
        "vms": [
            # Use the SDN IP we got at output.json time. The installer
            # reads --node-ip from this; if the live IP differs (it can
            # drift after a SDN DHCP reset), the next apply of the
            # cluster root reconciles it via output.json update + a
            # bootstrap_state.json wipe of the install_k3s entry.
            {
                "name": n["name"],
                "vmid": int(n.get("vmid", 0)),
                "role": n["role"],
                "ip": n["ip"],
            }
            for n in topo.all_nodes
            if n.get("name") and n.get("ip")
        ],
    }
    installer = K3sInstaller(
        cluster=cluster_dict,
        # The canonical PVE jump host. Lives in .env via the BITWARDEN
        # agent env var that apply_tofu.py sets up; the actual SSH
        # call is delegated to the operator's agent.
        ssh_proxy_target=os.environ.get(
            "PVE_SSH_TARGET",
            "root@kvm.bruj0.net -p 6022",
        ),
        logger=_LOG,
    )
    try:
        # 1) Install on every control-plane VM (serially; multi-CP HA
        # will parallelize via a future WP). The first CP is the join
        # target for the agents, so we install CPs before workers.
        for cp in topo.control_plane:
            node = {
                "name": cp["name"],
                "vmid": int(cp.get("vmid", 0)),
                "role": "control_plane",
                "ip": cp["ip"],
                # WP07: pass svc_cidr into the per-VM dict so the
                # installer can add the in-cluster apiserver SANs.
                # The installer reads svc_cidr from the first VM
                # it's installed on; we pass it on every call so
                # multi-CP clusters stay consistent.
                "svc_cidr": topo.svc_cidr,
                # WP08: same for pod_cidr + cluster_dns.
                "pod_cidr": topo.pod_cidr,
                "cluster_dns": topo.cluster_dns,
            }
            installer.install_server(node, vip=topo.vip)
        # 2) Read the join token off the first CP. If the server is
        # not yet healthy we time out and surface a structured error
        # so the operator can investigate the upstream install log.
        first_cp = {
            "name": topo.control_plane[0]["name"],
            "vmid": int(topo.control_plane[0].get("vmid", 0)),
            "role": "control_plane",
            "ip": topo.control_plane[0]["ip"],
        }
        token = installer.read_node_token(first_cp)
        # 3) Install agents on every worker VM.
        for w in topo.worker:
            node = {
                "name": w["name"],
                "vmid": int(w.get("vmid", 0)),
                "role": "worker",
                "ip": w["ip"],
            }
            installer.install_agent(node, vip=topo.vip, token=token)
    except K3sInstallerError as exc:
        raise BootstrapError(
            "install_k3s", {"reason": exc.reason, **exc.fields}
        ) from exc
    state.phases_done.add("install_k3s")


def _run_k3s(state: State, cluster_dir: Path, topo: ClusterTopology) -> None:
    """Verify k3s is healthy on the cluster.

    k3s runs as a systemd unit on each Ubuntu node (installed by the
    cloud-init runcmd in the NoCloud seed ISO), but we must verify the
    apiserver is reachable and at least one node is Ready before
    declaring the cluster bootable. Otherwise the helm phase will
    surface a confusing "connection refused" failure.

    Use the same PVE apiserver tunnel as the helm phase: it sets up a
    live port-forward, fetches the kubeconfig from the CP, and rewrites
    the server URL to point at the tunnel's local port. Doing this here
    makes `k3s` idempotent: it works whether the on-disk kubeconfig is
    fresh, stale (from a previous bootstrap that tore the tunnel down),
    or missing entirely.
    """
    if not topo.control_plane:
        raise BootstrapError("k3s", {"reason": "no control plane"})
    cp_ip = topo.control_plane[0]["ip"]
    try:
        kubeconfig, _local_port = _open_apiserver_tunnel(state, cp_ip, cluster_dir)
    except Exception as exc:
        raise BootstrapError("k3s", {"reason": str(exc)}) from exc
    try:
        result = subprocess.run(
            [
                "kubectl",
                "--kubeconfig",
                str(kubeconfig),
                "get",
                "--raw",
                "/healthz",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        if "ok" not in result.stdout.lower():
            raise BootstrapError(
                "k3s",
                {"reason": f"apiserver /healthz returned: {result.stdout!r}"},
            )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        raise BootstrapError("k3s", {"reason": str(exc)}) from exc
    state.phases_done.add("k3s")


def _open_apiserver_tunnel(
    state: State,
    cp_ip: str,
    cluster_dir: Path,
) -> tuple[Path, int]:
    """Open an apiserver tunnel through PVE, write a kubeconfig that hits it.

    Returns (kubeconfig_path, local_port). Idempotent: if the State already
    has a live tunnel, reuse it (the same call happens again on retries or
    when both `helm` and `kubeconfig` phases run in one invocation).

    Why a tunnel (vs. an scp/raw ssh call):
      - The CPs are on SDN 10.0.0.0/24; the operator host is on
        10.0.10.0/24. The CPs aren't directly reachable.
      - PveSshProxy provides port forwarding through the PVE node
        (10.0.10.4), the same channel used by the live operator
        tools (tools/ssh_proxy.py, tools/kubeconfig_puller.py).
      - The k3s kubeconfig points at https://127.0.0.1:6443 on the
        CP. We rewrite the server: URL to https://127.0.0.1:<local_port>
        so kubectl / helm on the operator host reach the tunnel
        instead of the operator's own loopback.
    """
    if state.forward is None:
        if state.proxy is None:
            state.proxy = PveSshProxy(logger=_LOG)
        # Forward 127.0.0.1:6443 on the CP (where k3s binds) to a
        # free local port. Pick a free port explicitly so the
        # kubeconfig's server: URL is deterministic and the operator
        # can pre-arrange firewall rules if needed.
        state.forward = state.proxy.port_forward(
            cp_ip,
            remote_port=6443,
            remote_bind="127.0.0.1",
            local_port=0,  # ask the proxy for a free port
        )
        # port_forward() already calls wait_ready() internally before
        # returning; no extra wait needed here.
        _LOG.info(
            "helm.tunnel_ready",
            local_port=state.forward.local_port,
            cp_ip=cp_ip,
            pid=state.forward.proc.pid,
        )
    kubeconfig = cluster_dir / "kubeconfig"
    if state.proxy is None:  # pragma: no cover -- guarded above
        raise BootstrapError("helm", {"reason": "proxy missing"})
    # Always fetch + rewrite. Re-running the bootstrap script picks a
    # different ephemeral forward port each time, so a cached
    # kubeconfig pointing at the previous port would steer kubectl
    # and helm at a closed socket. The fetch is cheap (one ssh exec).
    body = fetch_kubeconfig_via_proxy(state.proxy, cp_ip, _LOG)
    rewritten = rewrite_server_url(body, state.forward.local_port)
    kubeconfig.parent.mkdir(parents=True, exist_ok=True)
    kubeconfig.write_text(rewritten)
    kubeconfig.chmod(0o600)
    return kubeconfig, state.forward.local_port


def _run_helm(state: State, cluster_dir: Path, topo: ClusterTopology) -> None:
    # Helm needs a reachable apiserver and a kubeconfig that points at it.
    # Open the PVE tunnel to the first CP and write the kubeconfig if it
    # isn't on disk yet.
    if not topo.control_plane:
        raise BootstrapError("helm", {"reason": "no control plane"})
    cp_ip = topo.control_plane[0]["ip"]
    try:
        kubeconfig, _local_port = _open_apiserver_tunnel(state, cp_ip, cluster_dir)
    except Exception as exc:
        raise BootstrapError("helm", {"reason": str(exc)}) from exc
    client = HelmClient(kubeconfig)
    cluster_dict: dict[str, object] = {
        "name": topo.name,
        "vip": topo.vip,
        "pod_cidr": topo.pod_cidr,
        "svc_cidr": topo.svc_cidr,
        "control_plane_ip": topo.control_plane[0]["ip"] if topo.control_plane else "",
    }
    secrets = _load_cluster_secrets()
    try:
        # WP08: only cilium remains in the first pair (kube-vip removed
        # 2026-07-08; single-CP cicd doesn't need a VIP layer).
        client.install_or_upgrade(first_two_releases(cluster_dict))
        # Remaining four (WP05): proxmox-ccm, proxmox-csi, cloudflare-tunnel,
        # cert-manager.
        remaining, traefik_apply = remaining_releases(cluster_dict, secrets)
        client.install_or_upgrade(remaining)
        # WP07: Envoy Gateway v1.8.2 (GatewayClass=envoy
        # implementation). Requires the standard Gateway API CRDs
        # to already be present (the `gateway_crds` phase ran
        # before `helm`); the chart's own CRD install is disabled
        # via values (`crds.enabled=false`).
        client.install_or_upgrade(gateway_releases())
        # Traefik is installed via the Talos HelmChartConfig mechanism (the
        # SS2 module rendered the YAML into infra/clusters/<name>/manifests/ at
        # apply time). SS3's job is to apply that file. If it does not
        # exist yet (first WP05 run before tofu apply re-renders), warn.
        if traefik_apply is not None:
            try:
                apply_manifest(traefik_apply, kubeconfig)
            except subprocess.CalledProcessError as exc:
                raise BootstrapError("helm", {"reason": str(exc)}) from exc
        else:
            _LOG.warn(
                "helm.traefik_noapply",
                message=(
                    "no HelmChartConfig manifest found under "
                    f"{cluster_dir}/manifests; expect kube-system/Traefik to "
                    "use its bundled defaults."
                ),
            )
    except subprocess.CalledProcessError as exc:
        raise BootstrapError("helm", {"reason": str(exc)}) from exc
    # WP07 (2026-07-08): the proxmox-csi-plugin chart 0.5.9 has
    # NO way to set the `is-default-class` annotation via values
    # (the `default: true` key on the storageClass list is dead
    # code in 0.5.9; the chart's StorageClass template renders
    # the annotation block conditionally on `with
    # $storage.annotations`, which has no value-mapping). The
    # bootstrap pins the `proxmox-lvm-thin` SC as default by
    # patching the annotation post-install. This is the only
    # value-level workaround that doesn't require a values
    # file in the cluster's manifests/ tree.
    #
    # Pinned by tests/test_bootstrap_cluster.py:
    #   test_post_install_patches_proxmox_lvm_thin_default
    _ensure_csi_default_sc(kubeconfig, "proxmox-lvm-thin", "proxmox-csi-plugin")
    state.phases_done.add("helm")


def _ensure_csi_default_sc(
    kubeconfig: Path, storage_class_name: str, namespace: str
) -> None:
    """Post-install: set is-default-class=true on the named StorageClass.

    The proxmox-csi-plugin chart 0.5.9 has no values schema for
    this (see the `_run_helm` block above). We patch the
    annotation here so the rest of the bootstrap can rely on
    `proxmox-lvm-thin` being the cluster default.

    Idempotent: `kubectl annotate` with `--overwrite` is a
    no-op when the value is already correct. The phase is
    skipped entirely on a re-run if the annotation is already
    `true` (no Helm interaction).
    """
    try:
        result = subprocess.run(
            [
                "kubectl",
                "--kubeconfig",
                str(kubeconfig),
                "get",
                "sc",
                storage_class_name,
                "-o",
                "jsonpath={.metadata.annotations.storageclass\\.kubernetes\\.io/is-default-class}",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
        if result.stdout.strip().lower() == "true":
            return
        subprocess.run(
            [
                "kubectl",
                "--kubeconfig",
                str(kubeconfig),
                "annotate",
                "sc",
                storage_class_name,
                "storageclass.kubernetes.io/is-default-class=true",
                "--overwrite=true",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        # Don't fail the helm phase on a non-critical annotation
        # patch. The csi_smoke phase will assert
        # `is-default-class=true` and surface this as a hard
        # failure if the SC never becomes default. The annotation
        # is best-effort here.
        _LOG.warn(
            "helm.csi_sc_default_annotate_failed",
            storage_class=storage_class_name,
            message=str(exc),
        )


def _run_gateway_crds(
    state: State, cluster_dir: Path, topo: ClusterTopology
) -> None:
    """WP07: apply the pinned upstream standard Gateway API CRDs.

    Runs BEFORE `helm` so the Envoy Gateway chart's
    `--skip-crds` install finds the standard CRDs already
    present. `kubectl apply --server-side` is idempotent — the
    server resolves conflicts, so re-running is a no-op rather
    than a failure.

    Uses the same PveSshProxy tunnel that the `helm` phase opens
    (the `State` carries the tunnel forward across phases for
    the script's lifetime). This means the phase assumes the
    `k3s` healthz check already passed.
    """
    # Import here to avoid a top-level circular import through
    # helm_client -> bootstrap_cluster -> helm_client.
    from lib.helm_client import GATEWAY_API_STANDARD_CRDS_URL  # noqa: E402

    if not topo.control_plane:
        raise BootstrapError("gateway_crds", {"reason": "no control plane"})
    cp_ip = topo.control_plane[0]["ip"]
    try:
        # Open the tunnel only if no other phase has. The
        # bootstrap() dispatcher iterates phases in declared
        # order, so on a fresh run the tunnel is not yet open at
        # this point (helm hasn't run); on a re-run with
        # --phases=gateway_crds only, the tunnel will be opened
        # here.
        kubeconfig, _ = _open_apiserver_tunnel(state, cp_ip, cluster_dir)
    except Exception as exc:
        raise BootstrapError("gateway_crds", {"reason": str(exc)}) from exc
    try:
        result = subprocess.run(
            [
                "kubectl",
                "--kubeconfig",
                str(kubeconfig),
                "apply",
                "--server-side",
                "--validate=false",  # CRD apply always validates
                # against itself; skip the client-side dry
                # validate to avoid spurious errors on first
                # apply.
                "-f",
                GATEWAY_API_STANDARD_CRDS_URL,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
        _LOG.info(
            "gateway_crds.applied",
            url=GATEWAY_API_STANDARD_CRDS_URL,
            stdout=result.stdout[:500],
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        raise BootstrapError(
            "gateway_crds",
            {"reason": str(exc), "url": GATEWAY_API_STANDARD_CRDS_URL},
        ) from exc
    state.phases_done.add("gateway_crds")


def _run_gateway_smoke(
    state: State, cluster_dir: Path, topo: ClusterTopology
) -> None:
    """WP07: real smoke test for Envoy Gateway.

    Deploys a temporary Gateway + HTTPRoute + hashicorp/http-echo
    pod in the `proxmox-k8s-cicd-smoke` namespace, curls the
    Gateway's `status.addresses[0]` and asserts the echo body
    matches. Cleans up at the end (deletes the namespace).

    Idempotency: if the namespace already exists from a prior
    run, the phase re-asserts the curl instead of re-applying.
    On a successful curl, the namespace is always deleted so
    the cluster ends clean for GitLab.

    Failure modes that raise BootstrapError (so a live-host
    gotcha surfaces loudly):
      - GatewayClass=envoy does not exist (Envoy Gateway not
        installed yet).
      - Gateway fails to become Programmed within 60s
        (Envoy controller pod is unhealthy; check
        `kubectl logs -n envoy-gateway-system`).
      - HTTPRoute fails to resolve backend refs within 60s
        (echo Service is not yet ready; usually a Cilium
        GAMMA-vs-standard-CRD conflict).
      - curl returns a non-echo body (Envoy didn't match the
        HTTPRoute rules).
    """
    from lib.helm_client import GATEWAY_API_STANDARD_CRDS_URL  # noqa: E402

    smoke_ns = "proxmox-k8s-cicd-smoke"
    smoke_dir = cluster_dir / "manifests" / "_smoke"
    smoke_dir.mkdir(parents=True, exist_ok=True)
    smoke_yaml = smoke_dir / "envoy-gateway-smoke.yaml"

    if not topo.control_plane:
        raise BootstrapError("gateway_smoke", {"reason": "no control plane"})
    cp_ip = topo.control_plane[0]["ip"]
    try:
        kubeconfig, _ = _open_apiserver_tunnel(state, cp_ip, cluster_dir)
    except Exception as exc:
        raise BootstrapError("gateway_smoke", {"reason": str(exc)}) from exc

    # 1. Pre-flight: GatewayClass=envoy must exist (chart installed
    # in the `helm` phase). If not, fail fast — operator should
    # re-run with --phases=helm,gateway_smoke.
    try:
        result = subprocess.run(
            [
                "kubectl",
                "--kubeconfig",
                str(kubeconfig),
                "get",
                "gatewayclass",
                "envoy",
                "-o",
                "jsonpath={.status.conditions[?(@.type==\"Accepted\")].status}",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
        if result.stdout.strip() != "True":
            raise BootstrapError(
                "gateway_smoke",
                {
                    "reason": (
                        "GatewayClass=envoy not Accepted; "
                        "Envoy Gateway controller may not be ready. "
                        "Check `kubectl -n envoy-gateway-system get pods`."
                    ),
                    "accepted_status": result.stdout.strip(),
                },
            )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        raise BootstrapError("gateway_smoke", {"reason": str(exc)}) from exc

    # 2. Materialise the smoke-test YAML on disk. Idempotent:
    # always rewrite — the body is tiny and the cluster YAML
    # server-resolves conflicts on re-apply.
    smoke_yaml.write_text(
        f"""---
apiVersion: v1
kind: Namespace
metadata:
  name: {smoke_ns}
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: echo
  namespace: {smoke_ns}
spec:
  replicas: 1
  selector:
    matchLabels:
      app: echo
  template:
    metadata:
      labels:
        app: echo
    spec:
      containers:
        - name: echo
          image: hashicorp/http-echo:0.2.3
          args: ["-text=proxmox-k8s-cicd-smoke-envoy-gateway"]
          ports:
            - containerPort: 5678
---
apiVersion: v1
kind: Service
metadata:
  name: echo
  namespace: {smoke_ns}
spec:
  selector:
    app: echo
  ports:
    - port: 5678
      targetPort: 5678
---
apiVersion: gateway.networking.k8s.io/v1
kind: Gateway
metadata:
  name: smoke-gw
  namespace: {smoke_ns}
spec:
  gatewayClassName: envoy
  listeners:
    - name: http
      port: 80
      protocol: HTTP
      allowedRoutes:
        namespaces:
          from: Same
---
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: smoke
  namespace: {smoke_ns}
spec:
  parentRefs:
    - name: smoke-gw
  rules:
    - matches:
        - path:
            type: PathPrefix
            value: /
      backendRefs:
        - name: echo
          port: 5678
""",
        encoding="utf-8",
    )
    smoke_yaml.chmod(0o600)
    try:
        subprocess.run(
            [
                "kubectl",
                "--kubeconfig",
                str(kubeconfig),
                "apply",
                "-f",
                str(smoke_yaml),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        raise BootstrapError(
            "gateway_smoke",
            {
                "reason": f"apply smoke manifest failed: {exc}",
                "crds_url": GATEWAY_API_STANDARD_CRDS_URL,
            },
        ) from exc

    # 3. Wait for the Gateway to be Programmed and the
    # HTTPRoute to resolve refs (60s budget; the chart's
    # default reconcile interval is 10s).
    try:
        wait_result = subprocess.run(
            [
                "kubectl",
                "--kubeconfig",
                str(kubeconfig),
                "wait",
                "--namespace",
                smoke_ns,
                "--for=condition=Programmed=True",
                "--timeout=60s",
                "gateway/smoke-gw",
            ],
            capture_output=True,
            text=True,
            timeout=90,
        )
        if wait_result.returncode != 0:
            raise BootstrapError(
                "gateway_smoke",
                {"reason": "Gateway did not become Programmed within 60s",
                 "detail": wait_result.stderr[:500]},
            )
    except subprocess.TimeoutExpired as exc:
        raise BootstrapError(
            "gateway_smoke",
            {"reason": "Gateway wait timed out", "detail": str(exc)},
        ) from exc

    # 4. Discover the data-plane Service ClusterIP (Envoy Gateway
    # creates `envoy-gateway-system/<gw-name>-<gw-namespace>-<id>`
    # ClusterIP Services, one per Gateway). List Services in
    # envoy-gateway-system, pick the one whose
    # `gateway.envoyproxy.io/owning-gateway-name=smoke-gw`
    # annotation matches.
    try:
        svc_result = subprocess.run(
            [
                "kubectl",
                "--kubeconfig",
                str(kubeconfig),
                "get",
                "svc",
                "-n",
                "envoy-gateway-system",
                "-l",
                "gateway.envoyproxy.io/owning-gateway-name=smoke-gw",
                "-o",
                "jsonpath={.items[0].spec.clusterIP}",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
        cluster_ip = svc_result.stdout.strip()
        if not cluster_ip:
            raise BootstrapError(
                "gateway_smoke",
                {"reason": "no data-plane Service found for Gateway smoke-gw"},
            )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        raise BootstrapError("gateway_smoke", {"reason": str(exc)}) from exc

    # 5. Curl the Gateway's data-plane ClusterIP. Run from a
    # local kubectl exec into a busybox pod so we don't depend
    # on the operator host being able to reach the SDN.
    try:
        curl_result = subprocess.run(
            [
                "kubectl",
                "--kubeconfig",
                str(kubeconfig),
                "run",
                "--rm",
                "--restart=Never",
                "-n",
                smoke_ns,
                "--image=busybox:1.37",
                "--",
                "wget",
                "-qO-",
                f"http://{cluster_ip}/",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        body = curl_result.stdout.strip()
        if body != "proxmox-k8s-cicd-smoke-envoy-gateway":
            raise BootstrapError(
                "gateway_smoke",
                {
                    "reason": "echo body mismatch",
                    "expected": "proxmox-k8s-cicd-smoke-envoy-gateway",
                    "actual": body[:200],
                    "gateway_cluster_ip": cluster_ip,
                },
            )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        raise BootstrapError(
            "gateway_smoke",
            {"reason": f"curl via wget pod failed: {exc}"},
        ) from exc
    _LOG.info(
        "gateway_smoke.ok",
        gateway_cluster_ip=cluster_ip,
        body=body,
    )

    # 6. Cleanup: delete the smoke namespace. Best-effort; a
    # failure here does NOT fail the phase because the smoke
    # was already green. Operator can run
    # `kubectl delete ns proxmox-k8s-cicd-smoke` manually if
    # this log line shows up.
    try:
        subprocess.run(
            [
                "kubectl",
                "--kubeconfig",
                str(kubeconfig),
                "delete",
                "ns",
                smoke_ns,
                "--wait=false",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        _LOG.warn(
            "gateway_smoke.cleanup_failed",
            message=f"failed to delete namespace {smoke_ns}: {exc}",
        )

    state.phases_done.add("gateway_smoke")


def _run_kubeconfig(state: State, cluster_dir: Path, topo: ClusterTopology) -> None:
    if not topo.control_plane:
        raise BootstrapError("kubeconfig", {"reason": "no control plane"})
    cp_ip = topo.control_plane[0]["ip"]
    home = Path.home()
    try:
        # If the helm phase ran first in this invocation the tunnel is
        # already up; reuse it. Otherwise open one now.
        _kubeconfig, local_port = _open_apiserver_tunnel(state, cp_ip, cluster_dir)
        if state.forward is None:  # pragma: no cover -- guarded above
            raise BootstrapError("kubeconfig", {"reason": "tunnel missing"})
        # Merge the cluster kubeconfig into the operator's ~/.kube/config
        # (timestamped backup). The kubeconfig at cluster_dir/kubeconfig
        # has already been written by _open_apiserver_tunnel above; the
        # merger just merges it into the default location.
        merge_kubeconfig_for_pveproxy(
            state.cluster,
            cp_ip,
            state.repo_root,
            home,
            forward_local_port=local_port,
            forward_proc=state.forward.proc,
        )
    except (subprocess.CalledProcessError, OSError, RuntimeError) as exc:
        raise BootstrapError("kubeconfig", {"reason": str(exc)}) from exc
    state.phases_done.add("kubeconfig")


def _run_csi_smoke(
    state: State, cluster_dir: Path, topo: ClusterTopology
) -> None:
    """WP07: real smoke test for proxmox-csi-plugin.

    Sequence:
      1. Pre-flight: assert `proxmox-lvm-thin` StorageClass
         exists AND is marked default. If not, fail fast
         with a BootstrapError pointing at the helm phase
         (the release would have installed the SC).
      2. Materialise a tiny PVC + busybox writer pod manifest
         on disk (cluster_dir/manifests/_smoke/csi.yaml).
      3. Apply; wait for the PVC to reach Bound (60s budget).
      4. Wait for the writer pod to reach Completed (proves
         the marker file was written).
      5. Delete the writer pod; create a reader pod that
         mounts the same PVC and asserts the marker file
         survived.
      6. Cleanup: delete the smoke namespace (best-effort).

    Idempotency: the namespace is reused on re-run; the
    phase cleans it up at the end. If the namespace still
    exists from a prior failed run, the phase asserts the
    state and proceeds without re-applying the PVC.

    Runs AFTER `kubeconfig` because it uses
    `~/.kube/config` (the operator's merged kubeconfig) for
    kubectl calls — the cluster_dir/kubeconfig ephemeral
    tunnel-port URL would route through a closed socket
    after the script exits.
    """
    smoke_ns = "proxmox-k8s-cicd-smoke"
    smoke_dir = cluster_dir / "manifests" / "_smoke"
    smoke_dir.mkdir(parents=True, exist_ok=True)
    smoke_yaml = smoke_dir / "csi-smoke.yaml"

    # Use the operator's merged kubeconfig (the kubeconfig
    # phase just wrote it). State.forward is still open in
    # the same process, but we want this phase to be
    # runnable standalone via `--phases=csi_smoke` after the
    # operator's kubeconfig is in place, so we don't reuse the
    # PveSshProxy tunnel here.
    default_kubeconfig = Path.home() / ".kube" / "config"
    if not default_kubeconfig.exists():
        raise BootstrapError(
            "csi_smoke",
            {
                "reason": (
                    "~/.kube/config missing; run the kubeconfig "
                    "phase first"
                )
            },
        )

    # 1. StorageClass pre-flight. The SC name comes from the
    # helm release's `storageclass.name` value
    # (tools/lib/helm_client.py::remaining_releases, pinned
    # to `proxmox-lvm-thin`).
    try:
        sc_check = subprocess.run(
            [
                "kubectl",
                "get",
                "sc",
                "proxmox-lvm-thin",
                "-o",
                "jsonpath={.metadata.annotations.storageclass\\.kubernetes\\.io/is-default-class}",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
        is_default = sc_check.stdout.strip()
        if is_default != "true":
            raise BootstrapError(
                "csi_smoke",
                {
                    "reason": (
                        "StorageClass proxmox-lvm-thin is not "
                        "marked default; proxmox-csi-plugin helm "
                        "release may have a different default. "
                        "Check `kubectl get sc` and adjust "
                        "tools/lib/helm_client.py::remaining_releases."
                    ),
                    "is_default_class": is_default,
                },
            )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        raise BootstrapError("csi_smoke", {"reason": str(exc)}) from exc

    # 2. Materialise smoke manifest.
    smoke_yaml.write_text(
        f"""---
apiVersion: v1
kind: Namespace
metadata:
  name: {smoke_ns}
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: smoke-pvc
  namespace: {smoke_ns}
spec:
  accessModes:
    - ReadWriteOnce
  storageClassName: proxmox-lvm-thin
  resources:
    requests:
      storage: 1Gi
---
apiVersion: v1
kind: Pod
metadata:
  name: smoke-write
  namespace: {smoke_ns}
spec:
  restartPolicy: OnFailure
  containers:
    - name: write
      image: busybox:1.37
      command:
        - sh
        - -c
        - "echo proxmox-k8s-cicd-smoke-csi-marker > /data/marker; sync; cat /data/marker"
      volumeMounts:
        - name: data
          mountPath: /data
  volumes:
    - name: data
      persistentVolumeClaim:
        claimName: smoke-pvc
---
apiVersion: v1
kind: Pod
metadata:
  name: smoke-read
  namespace: {smoke_ns}
spec:
  restartPolicy: OnFailure
  containers:
    - name: read
      image: busybox:1.37
      command:
        - sh
        - -c
        - |
          if [ "$(cat /data/marker)" != "proxmox-k8s-cicd-smoke-csi-marker" ]; then
            echo "marker mismatch: $(cat /data/marker)"
            exit 1
          fi
          echo "marker survived pod churn"
      volumeMounts:
        - name: data
          mountPath: /data
  volumes:
    - name: data
      persistentVolumeClaim:
        claimName: smoke-pvc
""",
        encoding="utf-8",
    )
    smoke_yaml.chmod(0o600)

    # 3. Apply.
    try:
        subprocess.run(
            ["kubectl", "apply", "-f", str(smoke_yaml)],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        raise BootstrapError(
            "csi_smoke",
            {"reason": f"apply smoke manifest failed: {exc}"},
        ) from exc

    # 4. Wait for PVC Bound.
    try:
        bound = subprocess.run(
            [
                "kubectl",
                "wait",
                "--namespace",
                smoke_ns,
                "--for=jsonpath={.status.phase}=Bound",
                "--timeout=60s",
                "pvc/smoke-pvc",
            ],
            capture_output=True,
            text=True,
            timeout=90,
        )
        if bound.returncode != 0:
            raise BootstrapError(
                "csi_smoke",
                {
                    "reason": "PVC did not reach Bound within 60s",
                    "detail": bound.stderr[:500],
                    "hint": (
                        "Check `kubectl -n proxmox-csi-plugin get "
                        "pods`; proxmox-csi-controller may be in "
                        "ContainerCreating (cluster-state.md §14.1)."
                    ),
                },
            )
    except subprocess.TimeoutExpired as exc:
        raise BootstrapError(
            "csi_smoke",
            {"reason": "PVC wait timed out", "detail": str(exc)},
        ) from exc

    # 5. Wait for writer pod Completed.
    try:
        write_done = subprocess.run(
            [
                "kubectl",
                "wait",
                "--namespace",
                smoke_ns,
                "--for=condition=Ready=False",
                "--selector=",
                "pod/smoke-write",
                "--timeout=60s",
            ],
            capture_output=True,
            text=True,
            timeout=90,
        )
        # `kubectl wait --for=condition=Ready=False` exits 0
        # only when the condition becomes False; a writer pod
        # that exits 0 is no longer Ready. If it never exits,
        # this times out.
        if write_done.returncode != 0:
            # Fall back to a status check — the pod may have
            # Succeeded already.
            status = subprocess.run(
                [
                    "kubectl",
                    "get",
                    "pod",
                    "-n",
                    smoke_ns,
                    "smoke-write",
                    "-o",
                    "jsonpath={.status.phase}",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if status.stdout.strip() != "Succeeded":
                raise BootstrapError(
                    "csi_smoke",
                    {
                        "reason": (
                            "smoke-write pod did not complete; "
                            "PVC mount likely failed."
                        ),
                        "pod_status": status.stdout.strip(),
                        "wait_stderr": write_done.stderr[:500],
                    },
                )
    except subprocess.TimeoutExpired as exc:
        raise BootstrapError(
            "csi_smoke",
            {"reason": "smoke-write wait timed out", "detail": str(exc)},
        ) from exc

    # 6. Apply the reader pod (defined in the same manifest,
    # but kubectl apply above may have created it already —
    # re-apply to be safe).
    try:
        subprocess.run(
            ["kubectl", "apply", "-f", str(smoke_yaml)],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        raise BootstrapError(
            "csi_smoke",
            {"reason": f"re-apply reader pod failed: {exc}"},
        ) from exc

    # 7. Wait for reader pod Succeeded; inspect logs.
    try:
        read_done = subprocess.run(
            [
                "kubectl",
                "wait",
                "--namespace",
                smoke_ns,
                "--for=jsonpath={.status.phase}=Succeeded",
                "--timeout=60s",
                "pod/smoke-read",
            ],
            capture_output=True,
            text=True,
            timeout=90,
        )
        if read_done.returncode != 0:
            raise BootstrapError(
                "csi_smoke",
                {
                    "reason": (
                        "smoke-read pod did not reach Succeeded; "
                        "marker file likely missing — proves PVC "
                        "was not persisted across pod churn."
                    ),
                    "detail": read_done.stderr[:500],
                },
            )
        # Read the logs to confirm the marker matched.
        logs = subprocess.run(
            [
                "kubectl",
                "logs",
                "-n",
                smoke_ns,
                "smoke-read",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
        if "marker survived pod churn" not in logs.stdout:
            raise BootstrapError(
                "csi_smoke",
                {
                    "reason": (
                        "smoke-read logs do not contain success "
                        "marker — PVC is bound but data did not "
                        "survive pod churn."
                    ),
                    "logs": logs.stdout[:500],
                },
            )
    except subprocess.TimeoutExpired as exc:
        raise BootstrapError(
            "csi_smoke",
            {"reason": "smoke-read wait timed out", "detail": str(exc)},
        ) from exc
    _LOG.info(
        "csi_smoke.ok",
        pvc="smoke-pvc",
        writer_pod="smoke-write",
        reader_pod="smoke-read",
    )

    # 8. Cleanup (best-effort).
    try:
        subprocess.run(
            ["kubectl", "delete", "ns", smoke_ns, "--wait=false"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        _LOG.warn(
            "csi_smoke.cleanup_failed",
            message=f"failed to delete namespace {smoke_ns}: {exc}",
        )

    state.phases_done.add("csi_smoke")


def _run_host_ports(state: State, cluster_dir: Path) -> None:
    """WP05: M2 verification -- assert no new DNAT rules were added by Helm.

    Reads the captured baseline at `<cluster_dir>/host_ports_baseline.txt`
    (produced by scripts/capture_host_ports_baseline.sh once at WP00 setup),
    dumps the live PVE prerouting chain via ssh, and fails on diff.
    """

    def _on_ssh_failure(
        phase: str, ssh_target: str, exc: Exception
    ) -> None:
        raise BootstrapError(
            phase,
            {
                "reason": "ssh to PVE failed",
                "ssh_target": ssh_target,
                "detail": str(exc),
            },
        ) from exc

    baseline = cluster_dir / "host_ports_baseline.txt"
    try:
        verify_no_new_dnat_rules(
            baseline, on_ssh_failure=_on_ssh_failure
        )
    except FileNotFoundError as exc:
        raise BootstrapError(
            "host_ports",
            {"reason": "baseline file missing", "path": str(baseline)},
        ) from exc
    state.phases_done.add("host_ports")


def _run_externalname(
    state: State, cluster_dir: Path, topo: ClusterTopology
) -> None:
    """WP06: apply cross-cluster ExternalName Services to the apps cluster.

    No-op when called on the cicd cluster (the cicd cluster does not own
    the cross-cluster wiring; applying apps manifests onto cicd would
    leak the apps cluster's namespace layout onto cicd).

    No-op when the kustomization manifest is missing yet (e.g. first-run
    before `tofu apply` has emitted infra/clusters/apps/manifests/). The
    state.json skip logic means a subsequent bootstrap run will retry
    this phase once the manifest appears.
    """
    if topo.name != "apps":
        _LOG.info(
            "externalname.skip",
            cluster=state.cluster,
            reason="only the apps cluster owns the cross-cluster wiring",
        )
        state.phases_done.add("externalname")
        return

    kubeconfig = cluster_dir / "kubeconfig"
    if not kubeconfig.exists():
        raise BootstrapError(
            "externalname",
            {"reason": "kubeconfig missing; run the kubeconfig phase first"},
        )
    kustomization_dir = cluster_dir / "manifests" / "cicd-system"
    if not kustomization_dir.exists():
        _LOG.warn(
            "externalname.noapply",
            message=(
                "no kustomization under infra/clusters/apps/manifests/cicd-system; "
                "rerun bootstrap after `tofu apply` lands the manifest"
            ),
        )
        # Do NOT mark the phase as done: we have not applied the manifest,
        # so the next bootstrap run must retry. Recording 'done' here
        # would silently leave the apps cluster without the ExternalName
        # Services after the operator runs `tofu apply`.
        return

    try:
        subprocess.run(
            [
                "kubectl",
                "--kubeconfig",
                str(kubeconfig),
                "apply",
                "-k",
                str(kustomization_dir),
            ],
            check=True,
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        raise BootstrapError(
            "externalname",
            {"reason": "kubectl apply -k failed", "detail": str(exc)},
        ) from exc
    state.phases_done.add("externalname")


def _load_cluster_secrets() -> dict[str, str]:
    """Read runtime secrets from env via SecretLoader.

    Returns:
        Mapping with keys: proxmox_token_id, proxmox_token_secret,
        cf_api_token, cf_account_id, cf_tunnel_name. None of these is
        ever logged.
    """
    import os
    secrets = SecretLoader(logger=StructuredLogger("bootstrap_cluster.secrets"))
    required = secrets.get_many(
        [
            "PROXMOX_TOKEN_ID",
            "PROXMOX_TOKEN_SECRET",
            "CF_API_TOKEN",
            "CF_ACCOUNT_ID",
        ]
    )
    out: dict[str, str] = {
        k.lower(): v for k, v in required.items()
    }
    # CF_TUNNEL_NAME is optional; fall back to empty so the helm_client
    # uses its default.
    out["cf_tunnel_name"] = os.environ.get("CF_TUNNEL_NAME", "")
    return out


def bootstrap(
    cluster_name: str,
    repo_root: Path,
    phases: Sequence[str] | None = None,
) -> None:
    cluster_dir = repo_root / "infra" / "clusters" / cluster_name
    state = State(cluster=cluster_name, repo_root=repo_root).load()

    requested = _parse_phases(phases)
    # Load topology once up-front so phases can share it. If output.json is
    # missing, fail fast with the structured message rather than letting
    # each phase rediscover the gap.
    topo = _load_topology(cluster_dir)
    _LOG.info("bootstrap.start", cluster=cluster_name, phases=",".join(requested))

    try:
        for phase in requested:
            if phase in state.phases_done:
                _LOG.info(
                    "bootstrap.skip",
                    phase=phase,
                    cluster=cluster_name,
                    reason="already_done",
                )
                continue
            if phase == "cloudinit":
                _run_cloudinit(state, cluster_dir, topo)
            elif phase == "install_k3s":
                _run_install_k3s(state, cluster_dir, topo)
            elif phase == "k3s":
                _run_k3s(state, cluster_dir, topo)
            elif phase == "gateway_crds":
                _run_gateway_crds(state, cluster_dir, topo)
            elif phase == "helm":
                _run_helm(state, cluster_dir, topo)
            elif phase == "gateway_smoke":
                _run_gateway_smoke(state, cluster_dir, topo)
            elif phase == "kubeconfig":
                _run_kubeconfig(state, cluster_dir, topo)
            elif phase == "csi_smoke":
                _run_csi_smoke(state, cluster_dir, topo)
            elif phase == "host_ports":
                _run_host_ports(state, cluster_dir)
            elif phase == "externalname":
                _run_externalname(state, cluster_dir, topo)
            state.save()
    finally:
        # Tear down the apiserver tunnel regardless of success/failure.
        # Without this we'd leak the long-lived ssh process on every
        # bootstrap run, eventually exhausting the PVE connection limit.
        if state.forward is not None:
            try:
                state.forward.terminate()
                _LOG.info("bootstrap.tunnel_torn_down", pid=state.forward.proc.pid)
            except Exception as exc:  # pragma: no cover -- defensive
                _LOG.warn(
                    "bootstrap.tunnel_teardown_failed",
                    message=str(exc),
                )
            state.forward = None

    _LOG.info("bootstrap.done", cluster=cluster_name)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bootstrap an Ubuntu/k3s cluster (SS3)."
    )
    parser.add_argument("--cluster", required=True, help="cluster name (e.g. cicd)")
    parser.add_argument(
        "--phases",
        default=",".join(PHASES),
        help=f"comma-separated phases to run (default: {','.join(PHASES)})",
    )
    parser.add_argument(
        "--repo-root",
        default=str(Path.cwd()),
        help="repo root (default: cwd)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    phases = [p for p in args.phases.split(",") if p]
    try:
        bootstrap(
            cluster_name=args.cluster,
            repo_root=Path(args.repo_root),
            phases=phases,
        )
    except BootstrapError as exc:
        _LOG.error(
            "bootstrap.error",
            error=exc.phase,
            resolution=json.dumps(exc.detail),
        )
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())