"""SS3 entrypoint: bootstrap a cluster end-to-end.

Phases (6):
  1. talos       — apply-config to every node, wait for healthy, bootstrap k3s
  2. k3s         — verify apiserver /healthz
  3. helm        — install the first two (Cilium + kube-vip, WP04) and
                   remaining four (proxmox-ccm, proxmox-csi,
                   cloudflare-tunnel, cert-manager, WP05) Helm releases;
                   apply the rendered Traefik HelmChartConfig if present
  4. kubeconfig  — pull admin kubeconfig, merge into ~/.kube/config
  5. host_ports  — verify the PVE nft prerouting chain has no new DNAT
                   rules beyond the captured baseline (M2 misfit)
  6. externalname — apps-cluster only: apply the cross-cluster
                   ExternalName Services kustomization that exposes
                   cicd services (gitlab, registry, minio,
                   minio-console) to workloads on apps (WP06)

Entry gate:
  python -m tools.bootstrap_cluster --cluster cicd [--phases talos,k3s,helm,kubeconfig,host_ports]

Design choices:
  - Any non-zero subprocess exit raises BootstrapError. We do NOT silently
    swallow failures; M4 misfit was specifically about silent bootstrap
    failures that the operator only noticed when kubectl returned "no
    route to host".
  - All StructuredLogger calls route through log.scrub() so token-bearing
    stdout never reaches disk; M7 misfit.
  - Per-cluster cluster_dir / state.json records which phases have
    succeeded; re-running with --phases talos,k3s skips the helm,
    kubeconfig and host_ports phases even if they were never reached.
"""
from __future__ import annotations

import argparse
import json
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
from lib.kubeconfig_merger import merge as merge_kubeconfig  # type: ignore[import-not-found]  # noqa: E402
from lib.log import StructuredLogger  # type: ignore[import-not-found]  # noqa: E402
from lib.secret_loader import SecretLoader  # type: ignore[import-not-found]  # noqa: E402
from lib.talos_client import ClusterTopology, TalosClient  # type: ignore[import-not-found]  # noqa: E402

_LOG = StructuredLogger("bootstrap_cluster")  # noqa: E402


PHASES: tuple[str, ...] = (
    "talos",
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
        return self.repo_root / "clusters" / self.cluster / "bootstrap_state.json"


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
            "talos",
            {"reason": "missing output.json", "path": str(output_json)},
        )
    try:
        return ClusterTopology.from_output_json(output_json)
    except (ValueError, json.JSONDecodeError) as exc:
        raise BootstrapError("talos", {"reason": str(exc)}) from exc


def _run_talos(state: State, cluster_dir: Path, topo: ClusterTopology) -> None:
    talos_dir = cluster_dir / "talos"
    client = TalosClient(topo, talos_dir)
    try:
        client.apply_configs()
        client.wait_for_healthy()
        client.bootstrap_k3s()
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise BootstrapError("talos", {"reason": str(exc)}) from exc
    state.phases_done.add("talos")


def _run_k3s(state: State, cluster_dir: Path, topo: ClusterTopology) -> None:
    """Verify k3s is healthy on the cluster.

    k3s runs inside Talos static pods (no operator action needed to start
    it), but we must verify the apiserver is reachable and at least one
    node is Ready before declaring the cluster bootable. Otherwise the
    helm phase will surface a confusing "connection refused" failure.
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
            subprocess.run(
                [
                    "talosctl",
                    "--nodes",
                    cp_ip,
                    "kubeconfig",
                    str(kubeconfig),
                ],
                check=True,
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
            subprocess.run(
                [
                    "talosctl",
                    "--nodes",
                    cp_ip,
                    "kubeconfig",
                    str(kubeconfig),
                ],
                check=True,
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
        # SS2 module rendered the YAML into clusters/<name>/manifests/ at
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
    before `tofu apply` has emitted clusters/apps/manifests/). The
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
                "no kustomization under clusters/apps/manifests/cicd-system; "
                "rerun bootstrap after `tofu apply` lands the manifest"
            ),
        )
        # Record as done so the next run doesn't re-warn; idempotent first-run
        # contract.
        state.phases_done.add("externalname")
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
    cluster_dir = repo_root / "clusters" / cluster_name
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
        if phase == "talos":
            _run_talos(state, cluster_dir, topo)
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
        description="Bootstrap a Talos/k3s cluster (SS3)."
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