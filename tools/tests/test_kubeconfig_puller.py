"""Tests for tools/kubeconfig_puller.py -- the operator entry point for
producing a kubectl context whose server: URL points at a localhost
port-forwarded to a cluster's kube-apiserver through the PVE jump.

We test the pure-Python parts (argparse, kubeconfig rewrite, file
write). The actual SSH tunnel / `sudo cat` is not exercised here.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kubeconfig_puller import (  # noqa: E402
    _parse_args,
    _rewrite_server_url,
)


def test_rewrite_server_url_replaces_loopback() -> None:
    """k3s binds 127.0.0.1:6443 on CP; we point kubectl at our tunnel."""
    body = (
        "apiVersion: v1\n"
        "kind: Config\n"
        "clusters:\n"
        "- cluster:\n"
        "    server: https://127.0.0.1:6443\n"
        "  name: default\n"
    )
    out = _rewrite_server_url(body, local_port=16443)
    assert "server: https://127.0.0.1:16443" in out
    # The CP-side URL must be gone.
    assert "server: https://127.0.0.1:6443" not in out


def test_rewrite_preserves_indentation() -> None:
    body = (
        "apiVersion: v1\n"
        "clusters:\n"
        "- cluster:\n"
        "    server: https://127.0.0.1:6443\n"
        "  name: default\n"
    )
    out = _rewrite_server_url(body, local_port=12345)
    # Same 4-space indent on the new server: line.
    assert "    server: https://127.0.0.1:12345" in out


def test_rewrite_only_replaces_first_server_line() -> None:
    """A kubeconfig has exactly one server: -- the cluster's. We
    only swap the first occurrence (defensive: don't accidentally
    rewrite a user-added second context's server:)."""
    body = (
        "apiVersion: v1\n"
        "clusters:\n"
        "- cluster:\n"
        "    server: https://127.0.0.1:6443\n"
        "  name: default\n"
    )
    out = _rewrite_server_url(body, local_port=8080)
    # Only one server: line in the output.
    assert out.count("server:") == 1
    assert "127.0.0.1:8080" in out


def test_rewrite_raises_when_no_server_line() -> None:
    import pytest
    body = "apiVersion: v1\nkind: Config\n"  # no `clusters:`
    with pytest.raises(RuntimeError):
        _rewrite_server_url(body, local_port=1234)


def test_parse_args_default_output_path(tmp_path) -> None:
    """No --kubeconfig -> <repo>/infra/clusters/<name>/kubeconfig.pveproxy.

    The repo locator requires the target dir to look like a repo
    (must contain `infra/clusters/`), so we make the tmp_path
    fixture look like one.
    """
    (tmp_path / "infra" / "clusters" / "cicd").mkdir(parents=True)
    cfg = _parse_args(["--cluster", "cicd", "--repo-root", str(tmp_path)])
    assert cfg.cluster == "cicd"
    assert cfg.output_path == (
        tmp_path / "infra" / "clusters" / "cicd" / "kubeconfig.pveproxy"
    )
    assert cfg.keep_tunnel is True
    assert cfg.local_port is None


def test_parse_args_explicit_output_path(tmp_path) -> None:
    cfg = _parse_args(
        [
            "--cluster", "apps",
            "--kubeconfig", str(tmp_path / "kubeconfig"),
            "--local-port", "16443",
        ]
    )
    assert cfg.output_path == tmp_path / "kubeconfig"
    assert cfg.local_port == 16443


def test_parse_args_no_tunnel_requires_local_port() -> None:
    """`--no-tunnel` with no `--local-port` is an error path; we
    surface that in main(), not at parse time. But the parser must
    at least preserve the flag."""
    cfg = _parse_args(["--cluster", "cicd", "--no-tunnel"])
    assert cfg.keep_tunnel is False
    assert cfg.local_port is None
