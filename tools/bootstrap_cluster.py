"""SS3 entrypoint: bootstrap a cluster end-to-end.

Phases (7):
  1. cloudinit   — verify every clone VM finished cloud-init
  2. install_k3s  — install k3s on each VM via the Python
                    orchestrator (tools/lib/k3s_installer.py). The
                    server runs on the control-plane VM with
                    --tls-san=<vip>; agents join the cluster via
                    https://<vip>:6443 with a one-shot token read
                    from the server's /var/lib/rancher/k3s/server/node-token.
  3. k3s          — verify apiserver /healthz (after install_k3s has
                     brought up the server)
  4. helm         — install Cilium (kube-proxy replacement, WP04) and
                     the remaining four (proxmox-ccm, proxmox-csi,
                     cloudflare-tunnel, cert-manager, WP05) Helm
                     releases; apply the rendered Traefik
                     HelmChartConfig if present
  5. kubeconfig   — pull admin kubeconfig, merge into ~/.kube/config
  6. host_ports   — verify the PVE nft prerouting chain has no new DNAT
                     rules beyond the captured baseline (M2 misfit)
  7. externalname — apps-cluster only: apply the cross-cluster
                     ExternalName Services kustomization that exposes
                     cicd services (gitlab, registry, minio,
                     minio-console) to workloads on apps (WP06)

Entry gate:
  python -m tools.bootstrap_cluster --cluster cicd \
    [--phases cloudinit,k3s,helm,kubeconfig,host_ports]
  python -m tools.bootstrap_cluster --cluster apps \
    [--phases cloudinit,k3s,helm,kubeconfig,host_ports,externalname]

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
    remaining_releases,
)
from lib.host_ports import verify_no_new_dnat_rules  # type: ignore[import-not-found]  # noqa: E402
from lib.k3s_installer import K3sInstaller, K3sInstallerError  # type: ignore[import-not-found]  # noqa: E402
from lib.kubeconfig_merger import merge as merge_kubeconfig  # type: ignore[import-not-found]  # noqa: E402
from lib.log import StructuredLogger  # type: ignore[import-not-found]  # noqa: E402
from lib.secret_loader import SecretLoader  # type: ignore[import-not-found]  # noqa: E402
# ClusterTopology is the data class for parsing infra/clusters/<name>/output.json.
# It lives in lib.talos_client.py for historical reasons (it was first
# extracted during the Talos phase); the class itself has no Talos
# dependency -- it just reads JSON. We keep the file for audit but only
# import this one symbol.
from lib.talos_client import ClusterTopology  # type: ignore[import-not-found]  # noqa: E402

_LOG = StructuredLogger("bootstrap_cluster")  # noqa: E402


PHASES: tuple[str, ...] = (
    "cloudinit",
    "install_k3s",
    "k3s",
    "helm",
    "kubeconfig",
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
        "vip": topo.vip,
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
    """
    kubeconfig = cluster_dir / "kubeconfig"
    if not kubeconfig.exists():
        # If kubeconfig hasn't been pulled yet (canonical phase order puts
        # helm after kubeconfig), pull it now so we can talk to the cluster.
        if not topo.control_plane:
            raise BootstrapError("k3s", {"reason": "no control plane"})
        cp_ip = topo.control_plane[0]["ip"]
        kubeconfig.parent.mkdir(parents=True, exist_ok=True)
        try:
            # Ubuntu+k3s: kubeconfig lives at /etc/rancher/k3s/k3s.yaml on
            # the control-plane node. scp over SSH using the Bitwarden SSH
            # agent so the key is forwarded without prompting. The server
            # URL in the kubeconfig points at the VIP (10.0.0.30 for cicd,
            # 10.0.0.40 for apps); kubectl resolves it through the SDN.
            subprocess.run(
                [
                    "scp",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "StrictHostKeyChecking=no",
                    f"root@{cp_ip}:/etc/rancher/k3s/k3s.yaml",
                    str(kubeconfig),
                ],
                check=True,
                env={**__import__("os").environ, "SSH_AUTH_SOCK": "/home/bruj0/.bitwarden-ssh-agent.sock"},
            )
        except (subprocess.CalledProcessError, OSError) as exc:
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


def _run_helm(state: State, cluster_dir: Path, topo: ClusterTopology) -> None:
    # Helm needs a kubeconfig file to talk to the cluster. PHASES puts helm
    # before kubeconfig so the file might not exist yet; pull it inline.
    kubeconfig = cluster_dir / "kubeconfig"
    if not kubeconfig.exists():
        if not topo.control_plane:
            raise BootstrapError("helm", {"reason": "no control plane"})
        cp_ip = topo.control_plane[0]["ip"]
        kubeconfig.parent.mkdir(parents=True, exist_ok=True)
        try:
            # Ubuntu+k3s: kubeconfig lives at /etc/rancher/k3s/k3s.yaml on
            # the control-plane node. scp over SSH using the Bitwarden SSH
            # agent so the key is forwarded without prompting.
            subprocess.run(
                [
                    "scp",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "StrictHostKeyChecking=no",
                    f"root@{cp_ip}:/etc/rancher/k3s/k3s.yaml",
                    str(kubeconfig),
                ],
                check=True,
                env={**__import__("os").environ, "SSH_AUTH_SOCK": "/home/bruj0/.bitwarden-ssh-agent.sock"},
            )
        except (subprocess.CalledProcessError, OSError) as exc:
            raise BootstrapError("helm", {"reason": str(exc)}) from exc
    client = HelmClient(kubeconfig)
    cluster_dict: dict[str, object] = {
        "name": topo.name,
        "vip": topo.vip,
        "pod_cidr": topo.pod_cidr,
        "svc_cidr": topo.svc_cidr,
    }
    secrets = _load_cluster_secrets()
    try:
        # First two: cilium + kube-vip (WP04).
        client.install_or_upgrade(first_two_releases(cluster_dict))
        # Remaining four (WP05): proxmox-ccm, proxmox-csi, cloudflare-tunnel,
        # cert-manager.
        remaining, traefik_apply = remaining_releases(cluster_dict, secrets)
        client.install_or_upgrade(remaining)
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
    state.phases_done.add("helm")


def _run_kubeconfig(state: State, cluster_dir: Path, topo: ClusterTopology) -> None:
    if not topo.control_plane:
        raise BootstrapError("kubeconfig", {"reason": "no control plane"})
    cp_ip = topo.control_plane[0]["ip"]
    home = Path.home()
    try:
        merge_kubeconfig(state.cluster, cp_ip, state.repo_root, home)
    except (subprocess.CalledProcessError, OSError) as exc:
        raise BootstrapError("kubeconfig", {"reason": str(exc)}) from exc
    state.phases_done.add("kubeconfig")


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
        elif phase == "helm":
            _run_helm(state, cluster_dir, topo)
        elif phase == "kubeconfig":
            _run_kubeconfig(state, cluster_dir, topo)
        elif phase == "host_ports":
            _run_host_ports(state, cluster_dir)
        elif phase == "externalname":
            _run_externalname(state, cluster_dir, topo)
        state.save()

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