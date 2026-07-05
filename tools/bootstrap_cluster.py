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


def _run_talos(state: State, cluster_dir: Path) -> None:
    output_json = cluster_dir / "output.json"
    if not output_json.exists():
        raise BootstrapError(
            "talos",
            {
                "reason": "missing output.json",
                "path": str(output_json),
            },
        )
    topo = ClusterTopology.from_output_json(output_json)
    talos_dir = cluster_dir / "talos"
    client = TalosClient(topo, talos_dir)
    try:
        client.apply_configs()
        client.wait_for_healthy()
        client.bootstrap_k3s()
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise BootstrapError(
            "talos",
            {"reason": str(exc)},
        ) from exc
    state.phases_done.add("talos")


def _run_k3s(state: State, cluster_dir: Path) -> None:
    # k3s runs inside Talos static pods; this phase is intentionally a
    # no-op currently. We record success so subsequent phases proceed.
    _LOG.info("k3s.noop", cluster=state.cluster, note="k3s runs in Talos static pods")
    state.phases_done.add("k3s")


def _run_helm(state: State, cluster_dir: Path, kubeconfig: Path) -> None:
    client = HelmClient(kubeconfig)
    try:
        client.install_or_upgrade(first_two_releases(kubeconfig))
    except subprocess.CalledProcessError as exc:
        raise BootstrapError("helm", {"reason": str(exc)}) from exc
    state.phases_done.add("helm")


def _run_kubeconfig(state: State, cluster_dir: Path) -> None:
    output_json = cluster_dir / "output.json"
    topo = ClusterTopology.from_output_json(output_json)
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
            _run_talos(state, cluster_dir)
        elif phase == "k3s":
            _run_k3s(state, cluster_dir)
        elif phase == "helm":
            kubeconfig = cluster_dir / "kubeconfig"
            _run_helm(state, cluster_dir, kubeconfig)
        elif phase == "kubeconfig":
            _run_kubeconfig(state, cluster_dir)
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