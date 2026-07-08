"""WP07 live-apply helper — installed beside bootstrap_cluster.py for the
2026-07-08 first-apply.

Lives outside tools/lib/* because it is a one-shot operator script,
not library code. It does three things the standard bootstrap can't
do today:

  1. Installs Envoy Gateway v1.8.2 via a direct
     `helm upgrade --install` (bypasses `_run_helm`'s --wait on
     proxmox-cloud-controller-manager, which has a pre-existing
     `ContainerCreating` issue -- see docs/cluster-state.md §14.1).
     Resolves to the same final state as
     `lib.helm_client.gateway_releases()`.

  2. Calls `_run_gateway_smoke` + `_run_kubeconfig` + `_run_csi_smoke`
     from tools/bootstrap_cluster.py as ordinary Python functions,
     so the WP07 phases land end-to-end and the smoke tests run.

  3. Tears down the apiserver tunnel cleanly when done.

Removed once proxmox-ccm is fixed (then `_run_helm` is sufficient).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# tools/ + repo-root on sys.path. The bootstrap module imports
# `tools.lib.log` (from k3s_installer.py), so the project root
# must be importable too.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "tools"))
sys.path.insert(0, str(PROJECT_ROOT))

# Import the bootstrap module and pull out the phase functions +
# State dataclass. We deliberately do NOT call bootstrap()'s
# dispatcher; this script drives the phases directly because the
# standard dispatcher's `_run_helm` will fail on the
# proxmox-cloud-controller-manager --wait (known issue
# cluster-state.md §14.1).
from lib.helm_client import gateway_releases  # type: ignore[import-not-found]  # noqa: E402
from bootstrap_cluster import (  # type: ignore[import-not-found]  # noqa: E402
    BootstrapError,
    State,
    _load_topology,
    _open_apiserver_tunnel,
    _run_csi_smoke,
    _run_gateway_smoke,
    _run_kubeconfig,
)
from lib.log import StructuredLogger  # type: ignore[import-not-found]  # noqa: E402

_LOG = StructuredLogger("wp07_live_apply")


def install_envoy_gateway_standalone(kubeconfig: Path) -> None:
    """Install Envoy Gateway v1.8.2 with `--skip-crds` (we already applied them).

    Mirrors `gateway_releases()` in values but skips `--wait` so a
    downstream release's failure cannot block this one. We add
    `--skip-crds` because the bootstrap's `gateway_crds` phase
    already applied the standard CRDs (`crds.enabled=false` +
    `crds.gatewayAPI.safeUpgradePolicy.enabled=false` are chart
    values; `--skip-crds` is the helm flag that matches).
    """
    # Build the same HelmRelease object the bootstrap would pass,
    # but render the install command ourselves with --skip-crds
    # instead of --wait.
    rel = gateway_releases()[0]
    cmd = [
        "helm",
        "upgrade",
        "--install",
        rel.name,
        rel.chart,
        "--namespace",
        rel.namespace,
        "--create-namespace",
        "--version",
        rel.version,
        "--skip-crds",  # we did this in the gateway_crds phase
        "--kubeconfig",
        str(kubeconfig),
    ]
    for k, v in rel.values.items():
        cmd += ["--set", f"{k}={v}"]

    _LOG.info(
        "wp07.envoy_gateway_installing",
        release=rel.name,
        version=rel.version,
        chart=rel.chart,
    )
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if res.returncode != 0:
        # Surface the helm error before raising.
        print("--- helm stderr (last 2000) ---", file=sys.stderr)
        print(res.stderr[-2000:], file=sys.stderr)
        print("--- helm stdout (last 1000) ---", file=sys.stderr)
        print(res.stdout[-1000:], file=sys.stderr)
        raise SystemExit(f"helm install failed rc={res.returncode}")
    _LOG.info(
        "wp07.envoy_gateway_installed",
        release=rel.name,
        stdout_tail=res.stdout[-500:],
    )


def main() -> None:
    cluster_name = os.environ.get("CLUSTER", "cicd")
    repo_root = Path(__file__).resolve().parent.parent
    cluster_dir = repo_root / "infra" / "clusters" / cluster_name

    topo = _load_topology(cluster_dir)
    if not topo.control_plane:
        raise SystemExit("no control plane in output.json; bootstrap can't proceed")

    cp_ip = topo.control_plane[0]["ip"]
    state = State(cluster=cluster_name, repo_root=repo_root).load()

    try:
        # 1. Open the apiserver tunnel + write the kubeconfig so
        # helm + kubectl can talk to the cluster.
        kubeconfig, _ = _open_apiserver_tunnel(state, cp_ip, cluster_dir)
        # 2. Install Envoy Gateway (the standard CRDs were already
        # applied by the previous successful run on this cluster).
        install_envoy_gateway_standalone(kubeconfig)
        # 3. Wait for the controller pod to come up before we run
        # the smoke test (the chart default is --wait; without
        # --wait we need to gate ourselves).
        _wait_for_envoy_controller(kubeconfig, timeout=180)
        # 4. Run the gateway_smoke phase.
        _run_gateway_smoke(state, cluster_dir, topo)
        state.save()
    finally:
        if state.forward is not None:
            try:
                state.forward.terminate()
                _LOG.info("wp07.tunnel_torn_down", pid=state.forward.proc.pid)
            except Exception as exc:
                _LOG.warn("wp07.tunnel_teardown_failed", message=str(exc))
            state.forward = None

    # 5. Refresh the operator's merged kubeconfig (so csi_smoke
    # can find the cicd context).
    state2 = State(cluster=cluster_name, repo_root=repo_root).load()
    try:
        _run_kubeconfig(state2, cluster_dir, topo)
        state2.save()
    finally:
        if state2.forward is not None:
            try:
                state2.forward.terminate()
                _LOG.info("wp07.tunnel2_torn_down", pid=state2.forward.proc.pid)
            except Exception as exc:
                _LOG.warn("wp07.tunnel2_teardown_failed", message=str(exc))
            state2.forward = None

    # 6. Run the csi_smoke phase (uses ~/.kube/config, not the tunnel).
    state3 = State(cluster=cluster_name, repo_root=repo_root).load()
    _run_csi_smoke(state3, cluster_dir, topo)
    state3.save()

    _LOG.info("wp07.done", cluster=cluster_name)


def _wait_for_envoy_controller(kubeconfig: Path, *, timeout: int) -> None:
    """Block until the envoy-gateway controller pod is Ready.

    The chart ships a Deployment (1 replica) of the controller.
    Without `--wait`, helm returns before the pod is Ready. The
    smoke test would then see GatewayClass=envoy with
    Accepted=False and fail confusingly.
    """
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(
            [
                "kubectl",
                "--kubeconfig",
                str(kubeconfig),
                "get",
                "deployment",
                "-n",
                "envoy-gateway-system",
                "envoy-gateway",
                "-o",
                "jsonpath={.status.readyReplicas}",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.stdout.strip() == "1":
            _LOG.info("wp07.envoy_controller_ready")
            return
        time.sleep(2)
    raise SystemExit(
        f"envoy-gateway controller did not become Ready within {timeout}s"
    )


if __name__ == "__main__":
    try:
        main()
    except BootstrapError as exc:
        print(f"WP07 live apply failed: {exc}", file=sys.stderr)
        sys.exit(2)
