"""Tests for tools/ssh_proxy.py -- the operator entry point for day-to-day
SSH into cluster VMs, optionally with a k8s apiserver port-forward.

We test the pure-Python parts (argparse, target resolution, port
parsing, argv building). The actual subprocess calls are NOT
exercised here -- those go through the live host.
"""
from __future__ import annotations

import sys
from pathlib import Path

# tests/ lives under tools/; lib/ is a sibling. Make it importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.pve_ssh import PveSshProxy  # noqa: E402
from ssh_proxy import (  # noqa: E402
    ParsedForward,
    _build_argv,
    _resolve_target,
)


def test_parsed_forward_two_token_form() -> None:
    """`<local>:<remote>` -> default remote_bind = 127.0.0.1."""
    pf = ParsedForward.parse("6443:6443")
    assert pf.local_port == 6443
    assert pf.remote_bind == "127.0.0.1"
    assert pf.remote_port == 6443


def test_parsed_forward_three_token_form() -> None:
    """`<local>:<bind>:<remote>` -> explicit bind."""
    pf = ParsedForward.parse("16443:10.0.0.1:6443")
    assert pf.local_port == 16443
    assert pf.remote_bind == "10.0.0.1"
    assert pf.remote_port == 6443


def test_parsed_forward_rejects_garbage() -> None:
    import pytest
    with pytest.raises(ValueError):
        ParsedForward.parse("1:2:3:4")  # too many tokens
    with pytest.raises(ValueError):
        ParsedForward.parse("abc:def")  # non-integer ports


def test_resolve_target_first_control_plane_by_default() -> None:
    """No --role/--name -> first CP VM."""
    topo = _mk_topo(
        cps=[{"name": "cicd-cp-1", "ip": "10.0.0.65"}],
        wks=[{"name": "cicd-w-1", "ip": "10.0.0.64"}],
    )
    assert _resolve_target(topo, None, None)["name"] == "cicd-cp-1"


def test_resolve_target_role_worker() -> None:
    topo = _mk_topo(
        cps=[{"name": "cicd-cp-1", "ip": "10.0.0.65"}],
        wks=[{"name": "cicd-w-1", "ip": "10.0.0.64"}],
    )
    assert _resolve_target(topo, "worker", None)["name"] == "cicd-w-1"


def test_resolve_target_name_overrides_role() -> None:
    topo = _mk_topo(
        cps=[{"name": "cicd-cp-1", "ip": "10.0.0.65"}],
        wks=[{"name": "cicd-w-1", "ip": "10.0.0.64"}],
    )
    # Operator asks for a worker by name, even though --role=control_plane.
    assert _resolve_target(topo, "control_plane", "cicd-w-1")["name"] == "cicd-w-1"


def test_build_argv_interactive_no_forwards() -> None:
    """No --port-forward, no command -> just the jump + dest, no -L."""
    proxy = PveSshProxy(jump_host="root@kvm.bruj0.net -p 6022")
    argv = _build_argv(proxy, "10.0.0.65", command=None)
    assert "ProxyCommand=" in " ".join(argv)
    assert "ubuntu@10.0.0.65" in argv
    assert "-L" not in argv


def test_build_argv_with_port_forwards() -> None:
    """-L flags must appear BEFORE the destination hop."""
    proxy = PveSshProxy(jump_host="root@kvm.bruj0.net -p 6022")
    argv = _build_argv(
        proxy,
        "10.0.0.65",
        command=None,
        extra_port_forwards=[ParsedForward.parse("6443:127.0.0.1:6443")],
    )
    # The -L flag must appear BEFORE the destination.
    l_idx = argv.index("-L")
    dest_idx = argv.index("ubuntu@10.0.0.65")
    assert l_idx < dest_idx
    assert argv[l_idx + 1] == "6443:127.0.0.1:6443"


def test_build_argv_with_one_off_command() -> None:
    """`command` is appended after the destination hop (no `--`)."""
    proxy = PveSshProxy(jump_host="root@kvm.bruj0.net -p 6022")
    argv = _build_argv(proxy, "10.0.0.65", command="hostname")
    assert argv[-1] == "hostname"
    assert "--" not in argv  # we use single-arg, no need for separator


# ---------- helpers ----------


def _mk_topo(
    *, cps: list[dict[str, str]], wks: list[dict[str, str]]
):
    """Tiny stand-in for ClusterTopology that the resolver accepts.

    `_resolve_target` only reads `.name`, `.ip`, `.control_plane`,
    `.worker`, and `.all_nodes` -- we mimic that here.
    """
    class _T:
        def __init__(self) -> None:
            self.name = "cicd"
            self.vip = "10.0.0.30"
            self.control_plane = cps
            self.worker = wks
        @property
        def all_nodes(self):
            return [*cps, *wks]
    return _T()
