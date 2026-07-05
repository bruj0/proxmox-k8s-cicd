"""WP06 acceptance tests.

Encodes the cross-cluster wiring acceptance criteria:

  M3 (apps-cluster wiring): apps cluster exposes the four cicd services
    (gitlab / registry / minio / minio-console) as ExternalName Services
    in the cicd-system namespace, sourced from the manifest at
    `clusters/apps/manifests/cicd-system/externalname.yaml`.
  Acceptance: bootstrap_cluster.py --cluster apps gains a new phase
    "externalname" that applies this manifest via `kubectl apply -k`.
  Acceptance: the externalname phase is a no-op when --cluster != apps.

Side-effect guarantee: tests use `tmp_path` and monkeypatch subprocess.
No real network calls. No real cluster calls.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bootstrap_cluster import BootstrapError, bootstrap, list_phases  # noqa: E402


def _stub_ok(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=args, returncode=0, stdout="ok", stderr="")


def _stub_fail_kubectl_apply(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    raise subprocess.CalledProcessError(
        returncode=1, cmd=list(args), stderr="error: unable to apply kustomization"
    )


def _write_apps_cluster(repo_root: Path) -> Path:
    """Create the minimum filesystem shape bootstrap_cluster.py expects for
    an `apps` cluster: clusters/apps/output.json + clusters/apps/kubeconfig.
    """
    cluster_dir = repo_root / "clusters" / "apps"
    cluster_dir.mkdir(parents=True, exist_ok=True)
    output = {
        "cluster_name": "apps",
        "vip": "10.0.0.40",
        "pod_cidr": "10.44.0.0/16",
        "svc_cidr": "10.45.0.0/16",
        "nodes": [
            {"name": "apps-cp1", "ip": "10.0.0.211", "role": "control_plane"},
            {"name": "apps-cp2", "ip": "10.0.0.212", "role": "control_plane"},
            {"name": "apps-cp3", "ip": "10.0.0.213", "role": "control_plane"},
            {"name": "apps-w1", "ip": "10.0.0.214", "role": "worker"},
        ],
    }
    (cluster_dir / "output.json").write_text(json.dumps(output))
    (cluster_dir / "kubeconfig").write_text(
        "apiVersion: v1\nkind: Config\nclusters: []\ncontexts: []\nusers: []\n"
    )
    return cluster_dir


def _write_externalname_manifest(repo_root: Path) -> Path:
    """Author the ExternalName manifest + kustomization.yaml under
    clusters/apps/manifests/cicd-system/.
    """
    cicd_system = repo_root / "clusters" / "apps" / "manifests" / "cicd-system"
    cicd_system.mkdir(parents=True, exist_ok=True)
    external_name_yaml = (
        "apiVersion: v1\n"
        "kind: Service\n"
        "metadata:\n"
        "  name: gitlab\n"
        "  namespace: cicd-system\n"
        "spec:\n"
        "  type: ExternalName\n"
        "  externalName: gitlab.intranet\n"
        "  ports:\n"
        "    - name: http\n"
        "      port: 80\n"
        "      targetPort: 80\n"
        "    - name: ssh\n"
        "      port: 22\n"
        "      targetPort: 22\n"
        "---\n"
        "apiVersion: v1\n"
        "kind: Service\n"
        "metadata:\n"
        "  name: registry\n"
        "  namespace: cicd-system\n"
        "spec:\n"
        "  type: ExternalName\n"
        "  externalName: registry.intranet\n"
        "  ports:\n"
        "    - name: https\n"
        "      port: 443\n"
        "      targetPort: 443\n"
        "---\n"
        "apiVersion: v1\n"
        "kind: Service\n"
        "metadata:\n"
        "  name: minio\n"
        "  namespace: cicd-system\n"
        "spec:\n"
        "  type: ExternalName\n"
        "  externalName: minio.intranet\n"
        "  ports:\n"
        "    - name: https\n"
        "      port: 9000\n"
        "      targetPort: 9000\n"
        "---\n"
        "apiVersion: v1\n"
        "kind: Service\n"
        "metadata:\n"
        "  name: minio-console\n"
        "  namespace: cicd-system\n"
        "spec:\n"
        "  type: ExternalName\n"
        "  externalName: minio-console.intranet\n"
        "  ports:\n"
        "    - name: https\n"
        "      port: 9001\n"
        "      targetPort: 9001\n"
    )
    (cicd_system / "externalname.yaml").write_text(external_name_yaml)
    (cicd_system / "kustomization.yaml").write_text(
        "apiVersion: kustomize.config.k8s.io/v1beta1\n"
        "kind: Kustomization\n"
        "namespace: cicd-system\n"
        "resources:\n"
        "  - externalname.yaml\n"
    )
    return cicd_system


# ---------- externalname.yaml acceptance ----------


def test_externalname_manifest_has_four_services(tmp_path: Path) -> None:
    """WP06 T001: externalname.yaml renders exactly four ExternalName
    Services in the cicd-system namespace, named gitlab / registry /
    minio / minio-console."""
    cicd_system = _write_externalname_manifest(tmp_path)
    text = (cicd_system / "externalname.yaml").read_text()
    docs = [d for d in text.split("\n---\n") if d.strip()]
    services = []
    for doc in docs:
        if "kind: Service" in doc and "type: ExternalName" in doc:
            # crude parse: name + namespace
            for line in doc.splitlines():
                if line.startswith("  name:"):
                    name = line.split(":", 1)[1].strip()
                if line.startswith("  namespace:"):
                    ns = line.split(":", 1)[1].strip()
            services.append((ns, name))
    assert services == [
        ("cicd-system", "gitlab"),
        ("cicd-system", "registry"),
        ("cicd-system", "minio"),
        ("cicd-system", "minio-console"),
    ]
    # All services must be ExternalName.
    assert text.count("type: ExternalName") == 4


def test_kustomization_yaml_references_externalname(tmp_path: Path) -> None:
    """WP06 T002: kustomization.yaml uses v1beta1 schema and references the
    ExternalName manifest."""
    cicd_system = _write_externalname_manifest(tmp_path)
    kust = cicd_system / "kustomization.yaml"
    text = kust.read_text()
    assert "kustomize.config.k8s.io/v1beta1" in text
    assert "namespace: cicd-system" in text
    assert "externalname.yaml" in text


# ---------- bootstrap_cluster.py externalname phase ----------


def test_list_phases_includes_externalname() -> None:
    """WP06: the new phase must appear in the canonical phase list."""
    assert "externalname" in list_phases()


def test_externalname_phase_apps_cluster_applies_kustomization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WP06 T003: bootstrap --cluster apps --phases externalname runs
    `kubectl apply -k` against clusters/apps/manifests/cicd-system/."""
    _write_externalname_manifest(tmp_path)
    _write_apps_cluster(tmp_path)
    invoked: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        invoked.append(list(cmd))
        return _stub_ok(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)
    bootstrap(
        cluster_name="apps",
        repo_root=tmp_path,
        phases=("externalname",),
    )
    # Exactly one kubectl apply -k invocation must have been issued, against
    # the cicd-system kustomization.
    kubectl_calls = [c for c in invoked if c[:1] == ["kubectl"]]
    assert len(kubectl_calls) == 1
    assert "apply" in kubectl_calls[0]
    assert "-k" in kubectl_calls[0]
    assert any("cicd-system" in arg for arg in kubectl_calls[0])


def test_externalname_phase_skips_for_cicd_cluster(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WP06 T003: the externalname phase is a no-op when --cluster != apps.
    The cicd cluster does not own the cross-cluster wiring; running the
    phase there would mistakenly apply apps manifests onto cicd."""
    cicd_dir = tmp_path / "clusters" / "cicd"
    cicd_dir.mkdir(parents=True)
    output = {
        "cluster_name": "cicd",
        "vip": "10.0.0.30",
        "pod_cidr": "10.42.0.0/16",
        "svc_cidr": "10.43.0.0/16",
        "nodes": [
            {"name": "cicd-cp1", "ip": "10.0.0.201", "role": "control_plane"},
        ],
    }
    (cicd_dir / "output.json").write_text(json.dumps(output))
    (cicd_dir / "kubeconfig").write_text(
        "apiVersion: v1\nkind: Config\nclusters: []\ncontexts: []\nusers: []\n"
    )

    invoked: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        invoked.append(list(cmd))
        return _stub_ok(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)
    bootstrap(
        cluster_name="cicd",
        repo_root=tmp_path,
        phases=("externalname",),
    )
    # No kubectl apply -k call against cicd-system.
    assert not any(
        "apply" in c and "-k" in c and any("cicd-system" in arg for arg in c)
        for c in invoked
    )


def test_externalname_phase_failure_raises_bootstrap_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WP06 T003: a non-zero exit from `kubectl apply -k` surfaces as
    BootstrapError, never a silent success (M4 misfit)."""
    _write_externalname_manifest(tmp_path)
    _write_apps_cluster(tmp_path)
    monkeypatch.setattr(subprocess, "run", _stub_fail_kubectl_apply)
    with pytest.raises(BootstrapError) as ei:
        bootstrap(
            cluster_name="apps",
            repo_root=tmp_path,
            phases=("externalname",),
        )
    assert ei.value.phase == "externalname"


def test_externalname_phase_skips_when_manifest_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WP06 T003: if the manifest directory does not yet exist, skip
    (idempotent first-run: externalname phase does not crash the bootstrap;
    operator can rerun after `tofu apply` lands the manifest)."""
    _write_apps_cluster(tmp_path)
    # Deliberately do NOT call _write_externalname_manifest.
    invoked: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        invoked.append(list(cmd))
        return _stub_ok(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)
    bootstrap(
        cluster_name="apps",
        repo_root=tmp_path,
        phases=("externalname",),
    )
    # The phase completed without invoking kubectl.
    assert not any(c[:1] == ["kubectl"] for c in invoked)
    # The phase must NOT have marked itself done: the manifest is missing,
    # so the operator's next bootstrap run after `tofu apply` lands the
    # manifest must retry the apply. Recording 'done' here would silently
    # leave the apps cluster without the ExternalName Services.
    state_file = tmp_path / "clusters" / "apps" / "bootstrap_state.json"
    if state_file.exists():
        data = json.loads(state_file.read_text())
        assert "externalname" not in data.get("phases_done", [])