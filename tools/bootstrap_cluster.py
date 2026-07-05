"""SS3 entrypoint: bootstrap a cluster end-to-end.

Phases:
  1. talos       — apply-config to every node, wait for healthy, bootstrap k3s
  2. k3s         — placeholder (k3s starts inside Talos static pods; phase is
                   kept for symmetry / future health checks)
  3. helm        — install the first two Helm releases (Cilium + kube-vip)
  4. kubeconfig  — pull admin kubeconfig, merge into ~/.kube/config

Entry gate:
  python -m tools.bootstrap_cluster --cluster cicd [--phases talos,k3s,helm,kubeconfig]

Design choices:
  - Any non-zero subprocess exit raises BootstrapError. We do NOT silently
    swallow failures; M4 misfit was specifically about silent bootstrap
    failures that the operator only noticed when kubectl returned "no
    route to host".
  - All StructuredLogger calls route through log.scrub() so token-bearing
    stdout never reaches disk; M7 misfit.
  - Per-cluster cluster_dir / state.json records which phases have
    succeeded; re-running with --phases talos,k3s skips the helm and
    kubeconfig phases even if helm/kubeconfig were never reached.
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

from lib.helm_client import HelmClient, first_two_releases  # noqa: E402
from lib.kubeconfig_merger import merge as merge_kubeconfig  # noqa: E402
from lib.log import StructuredLogger  # noqa: E402
from lib.talos_client import ClusterTopology, TalosClient  # noqa: E402

_LOG = StructuredLogger("bootstrap_cluster")


PHASES: tuple[str, ...] = ("talos", "k3s", "helm", "kubeconfig")


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
    cluster_dict = {
        "name": topo.name,
        "vip": topo.vip,
        "pod_cidr": topo.pod_cidr,
        "svc_cidr": topo.svc_cidr,
    }
    try:
        client.install_or_upgrade(first_two_releases(cluster_dict))
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