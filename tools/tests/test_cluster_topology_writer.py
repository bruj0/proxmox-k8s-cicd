"""Tests for tools.lib.cluster_topology_writer.

The writer parses infra/clusters/<name>/main.tf for the static topology
and uses the public PVE API + qemu-guest-agent for live IP discovery.
These tests use fixture main.tf text so we never need a live cluster.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.cluster_topology_writer import (  # noqa: E402
    _module_keyword,
    _parse_module_literal_block,
    _parse_nested_count,
    _resolve_runtime_cluster_topology,
    _read_main_tf,
    discover_via_pve_api,
    write_output_json,
)


MAIN_TF_APPS = """\
module "apps" {
  source = "../../modules/proxmox-k3s-cluster"

  cluster_name = "apps"
  vmid_start   = 210
  image_id     = "900"

  vnet_bridge = "vnet0"
  pod_cidr    = "172.20.0.0/16"
  svc_cidr    = "172.21.0.0/16"
  cluster_dns = "172.21.0.10"

  control_plane = {
    count = 1
    cpu   = 4
  }
  workers = {
    count = 2
    cpu   = 4
  }
}
"""

MAIN_TF_CICD = """\
module "cicd" {
  source = "../../modules/proxmox-k3s-cluster"

  cluster_name = "cicd"
  vmid_start   = 200
  image_id     = "900"

  vnet_bridge = "vnet0"
  pod_cidr    = "172.16.0.0/16"
  svc_cidr    = "172.17.0.0/16"
  cluster_dns = "172.17.0.10"

  control_plane = {
    count = 1
  }
  workers = {
    count = 1
  }
}
"""


def test_parse_module_literal_block_extracts_top_level_strings(tmp_path: Path) -> None:
    main_tf = tmp_path / "main.tf"
    main_tf.write_text(MAIN_TF_APPS)
    fields = _parse_module_literal_block(main_tf.read_text(), "apps")
    by_name = {f.name: f.value for f in fields}
    assert by_name["cluster_name"] == "apps"
    assert by_name["vmid_start"] == "210"  # integers are kept as str
    assert by_name["pod_cidr"] == "172.20.0.0/16"
    assert by_name["image_id"] == "900"


def test_parse_module_literal_block_returns_empty_for_unknown_module(
    tmp_path: Path,
) -> None:
    main_tf = tmp_path / "main.tf"
    main_tf.write_text(MAIN_TF_APPS)
    assert _parse_module_literal_block(main_tf.read_text(), "nope") == []


def test_parse_nested_count_finds_cp_and_worker(tmp_path: Path) -> None:
    main_tf = tmp_path / "main.tf"
    main_tf.write_text(MAIN_TF_APPS)
    assert _parse_nested_count(main_tf.read_text(), "apps", "control_plane") == 1
    assert _parse_nested_count(main_tf.read_text(), "apps", "workers") == 2
    main_tf.write_text(MAIN_TF_CICD)
    assert _parse_nested_count(main_tf.read_text(), "cicd", "control_plane") == 1
    assert _parse_nested_count(main_tf.read_text(), "cicd", "workers") == 1


def test_module_keyword_finds_module_even_if_name_differs(
    tmp_path: Path,
) -> None:
    main_tf = tmp_path / "main.tf"
    main_tf.write_text('module "live-apps" { source = "../../modules/proxmox-k3s-cluster" }\n')
    assert _module_keyword("apps", main_tf.read_text()) == "live-apps"


def test_resolve_runtime_cluster_topology_apps(tmp_path: Path) -> None:
    cluster = tmp_path / "infra" / "clusters" / "apps"
    cluster.mkdir(parents=True)
    (cluster / "main.tf").write_text(MAIN_TF_APPS)

    topo = _resolve_runtime_cluster_topology(cluster, "apps")

    assert topo["cluster_name"] == "apps"
    assert topo["pod_cidr"] == "172.20.0.0/16"
    assert topo["svc_cidr"] == "172.21.0.0/16"
    assert topo["cluster_dns"] == "172.21.0.10"
    assert topo["vnet_bridge"] == "vnet0"
    assert topo["control_plane_count"] == 1
    assert topo["worker_count"] == 2

    cps = [n for n in topo["nodes"] if n["role"] == "control_plane"]
    wks = [n for n in topo["nodes"] if n["role"] == "worker"]
    assert cps == [
        {"role": "control_plane", "name": "apps-cp-1", "vmid": 210, "ip": ""},
    ]
    assert wks == [
        {"role": "worker", "name": "apps-w-1", "vmid": 211, "ip": ""},
        {"role": "worker", "name": "apps-w-2", "vmid": 212, "ip": ""},
    ]


def test_resolve_runtime_cluster_topology_missing_field_raises(
    tmp_path: Path,
) -> None:
    cluster = tmp_path / "infra" / "clusters" / "apps"
    cluster.mkdir(parents=True)
    (cluster / "main.tf").write_text(
        # Missing `cluster_name` deliberately.
        """
        module "apps" {
          source = "../../modules/proxmox-k3s-cluster"
          pod_cidr = "10.0.0.0/16"
          svc_cidr = "10.1.0.0/16"
          cluster_dns = "10.1.0.10"
          vnet_bridge = "vnet0"
          vmid_start = 210
          control_plane = { count = 1 }
          workers = { count = 1 }
        }
        """
    )
    with pytest.raises(KeyError, match="cluster_name"):
        _resolve_runtime_cluster_topology(cluster, "apps")


def test_write_output_json_writes_static_topology_when_pve_unreachable(
    tmp_path: Path,
) -> None:
    cluster = tmp_path / "infra" / "clusters" / "apps"
    cluster.mkdir(parents=True)
    (cluster / "main.tf").write_text(MAIN_TF_APPS)

    with mock.patch(
        "lib.cluster_topology_writer._pve_list_vms_with_names",
        return_value=[],  # simulate PVE VM list empty
    ), mock.patch(
        "lib.cluster_topology_writer.discover_via_pve_api",
        return_value={},  # simulate PVE not responding
    ):
        path = write_output_json(
            cluster,
            cluster_name="apps",
            pve_token="dummy-token",
        )
    data = json.loads(path.read_text())
    assert data["cluster_name"] == "apps"
    assert data["pod_cidr"] == "172.20.0.0/16"
    # IPs remain empty because the discovery stub returned nothing.
    for n in data["nodes"]:
        assert n["ip"] == ""
    # file mode is 0600 (contains cluster identity + VMID/IP layout).
    assert path.stat().st_mode & 0o777 == 0o600


def test_write_output_json_merges_prior_ips(tmp_path: Path) -> None:
    """If a previous run had populated IP fields, the writer must keep them
    (idempotency) rather than erase them on a subsequent run."""
    cluster = tmp_path / "infra" / "clusters" / "apps"
    cluster.mkdir(parents=True)
    (cluster / "main.tf").write_text(MAIN_TF_APPS)
    existing = {
        "cluster_name": "apps",
        "nodes": [
            {"name": "apps-cp-1", "ip": "10.44.0.223", "vmid": 210, "role": "control_plane"},
            {"name": "apps-w-1", "ip": "10.44.1.25", "vmid": 211, "role": "worker"},
            {"name": "apps-w-2", "ip": "10.44.1.26", "vmid": 212, "role": "worker"},
        ],
    }
    (cluster / "output.json").write_text(json.dumps(existing))

    with mock.patch(
        "lib.cluster_topology_writer._pve_list_vms_with_names",
        return_value=[],  # PVE lookup disabled; prior IPs win
    ), mock.patch(
        "lib.cluster_topology_writer.discover_via_pve_api",
        return_value={},
    ):
        path = write_output_json(
            cluster,
            cluster_name="apps",
            pve_token="dummy-token",
        )
    data = json.loads(path.read_text())
    by_name = {n["name"]: n for n in data["nodes"]}
    assert by_name["apps-cp-1"]["ip"] == "10.44.0.223"
    assert by_name["apps-w-1"]["ip"] == "10.44.1.25"
    assert by_name["apps-w-2"]["ip"] == "10.44.1.26"


def test_write_output_json_force_rediscovers(tmp_path: Path) -> None:
    cluster = tmp_path / "infra" / "clusters" / "apps"
    cluster.mkdir(parents=True)
    (cluster / "main.tf").write_text(MAIN_TF_APPS)
    (cluster / "output.json").write_text(
        json.dumps(
            {
                "cluster_name": "apps",
                "nodes": [
                    {"name": "apps-cp-1", "ip": "10.0.0.99", "vmid": 210, "role": "control_plane"},
                    {"name": "apps-w-1", "ip": "10.0.0.100", "vmid": 211, "role": "worker"},
                    {"name": "apps-w-2", "ip": "10.0.0.101", "vmid": 212, "role": "worker"},
                ],
            }
        )
    )
    with mock.patch(
        "lib.cluster_topology_writer._pve_list_vms_with_names",
        return_value=[(210, "apps-cp-1"), (211, "apps-w-1"), (212, "apps-w-2")],
    ), mock.patch(
        "lib.cluster_topology_writer.discover_via_pve_api",
        return_value={210: ["10.44.0.223"], 211: ["10.44.1.25"], 212: ["10.44.1.26"]},
    ):
        path = write_output_json(
            cluster,
            cluster_name="apps",
            pve_token="dummy-token",
            force=True,
        )
    data = json.loads(path.read_text())
    by_name = {n["name"]: n for n in data["nodes"]}
    assert by_name["apps-cp-1"]["ip"] == "10.44.0.223"
    assert by_name["apps-w-2"]["ip"] == "10.44.1.26"


def test_write_output_json_reconciles_vmid_via_live_pve(tmp_path: Path) -> None:
    """If Proxmox assigned non-contiguous VMIDs (e.g. main.tf says
    vmid_start=113 but live PVE shows apps-cp-1=114, apps-w-1=113 because
    those were the next free slots), the writer must trust PVE."""
    cluster = tmp_path / "infra" / "clusters" / "apps"
    cluster.mkdir(parents=True)
    (cluster / "main.tf").write_text(MAIN_TF_APPS)

    with mock.patch(
        "lib.cluster_topology_writer._pve_list_vms_with_names",
        return_value=[(114, "apps-cp-1"), (113, "apps-w-1")],  # swapped
    ), mock.patch(
        "lib.cluster_topology_writer.discover_via_pve_api",
        return_value={114: ["10.44.0.223"], 113: ["10.44.1.25"]},
    ):
        path = write_output_json(
            cluster,
            cluster_name="apps",
            pve_token="dummy-token",
            force=True,
        )
    data = json.loads(path.read_text())
    by_name = {n["name"]: n for n in data["nodes"]}
    # cp is at vmid 114 (per PVE), not 113 (main.tf assumption).
    assert by_name["apps-cp-1"]["vmid"] == 114
    assert by_name["apps-cp-1"]["ip"] == "10.44.0.223"
    assert by_name["apps-w-1"]["vmid"] == 113
    assert by_name["apps-w-1"]["ip"] == "10.44.1.25"


def test_discover_via_pve_api_handles_unknown_data_shape() -> None:
    """If the PVE API returns an unexpected JSON shape (future Proxmox
    version changes), the writer logs and returns empty entries instead
    of raising."""
    fake_response = json.dumps({"data": {"unwrapped": "weird-shape"}}).encode()
    with mock.patch(
        "urllib.request.urlopen",
        return_value=mock.MagicMock(read=lambda: fake_response),
    ):
        out = discover_via_pve_api("dummy-token")
    assert out == {}  # empty map; main.tf values stay


def test_read_main_tf_missing_file_raises(tmp_path: Path) -> None:
    cluster_dir = tmp_path / "infra" / "clusters" / "nope"
    cluster_dir.mkdir(parents=True)
    with pytest.raises(FileNotFoundError, match="main.tf"):
        _read_main_tf(cluster_dir)
