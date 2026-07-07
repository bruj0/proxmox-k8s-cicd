"""Operational: fix PowerDNS A/PTR records to match the IPs the SDN
actually handed out.

Background
----------
The cluster module computes per-node IPs from `var.ip_start` via
`cidrhost()` and writes them to PowerDNS. PVE's SDN however
auto-allocates IPs from its DHCP range (10.0.0.50-200 on this host),
not from `ip_start`. The two systems disagree, so the PowerDNS A
records point at addresses the VMs never had.

This script reads each VM's real `ens18` IPv4 via qemu-guest-agent
and PUTs the correct value into PowerDNS via the API. It also
refreshes the matching PTR record so reverse lookups stay consistent.

Why not fix the module?
-----------------------
Proxmox SDN IPAM does not let you bind a specific /24 host to a VM
without writing a `pvesh set /sdn/.../dhcp/hosts/<mac>` reservation
per VM, and even then the IPAM still walks the configured range --
you can't make it start at a non-zero host. Adjusting the records
post-hoc is the smaller, reversible change.

Usage
-----
    SSH_AUTH_SOCK=/home/bruj0/.bitwarden-ssh-agent.sock \\
    python scripts/sync_dns_to_sdn.py \\
        --vmid 111 --name cicd-cp-1 \\
        --vmid 112 --name cicd-w-1 \\
        --vmid 113 --name apps-cp-1 \\
        --vmid 114 --name apps-w-1 \\
        --pve-host kvm.bruj0.net --pve-ssh-port 6022 \\
        --pdns-api-key <key> \\
        [--pdns-endpoint http://127.0.0.1:18081] \\
        [--forward-zone intranet.local.] \\
        [--reverse-zone 10.in-addr.arpa.]

Defaults assume the operator's standard layout. The PDNS endpoint
default is the SSH-tunnel port that `apply_tofu.py` opens; this
script opens the tunnel itself for the duration of the run.

Exit codes
----------
 0  success
 2  prerequisite failure (ssh missing, etc.)
 3  PowerDNS API not reachable
 4  qemu-guest-agent query failed
 5  PowerDNS record update failed
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

# Make `tools.lib.*` importable.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.lib.log import StructuredLogger  # noqa: E402

DEFAULT_PVE_HOST = "kvm.bruj0.net"
DEFAULT_PVE_SSH_PORT = 6022
DEFAULT_PDNS_API_KEY_ENV = "POWERDNS_API_KEY"
DEFAULT_FORWARD_ZONE = "intranet.local."
DEFAULT_REVERSE_ZONE = "10.in-addr.arpa."
# Port the SSH tunnel will listen on locally. The real PowerDNS API is
# at 10.0.0.3:8081 inside the LXC -- tunneled through PVE because the
# SDN vnet0 is private.
DEFAULT_TUNNEL_LOCAL_PORT = 18081
TUNNEL_TARGET_HOST = "10.0.0.3"
TUNNEL_TARGET_PORT = 8081


@dataclass(frozen=True)
class VmTarget:
    vmid: int
    name: str  # e.g. "cicd-cp-1" -- becomes "<name>.intranet.local."


def _ssh_pve_cmd(
    ssh: str, host: str, port: int, remote_cmd: str, *, timeout: int = 30
) -> str:
    """Run a single command on PVE via the operator's SSH agent.

    Raises RuntimeError on non-zero exit. stdout is returned as text.
    """
    proc = subprocess.run(
        [
            ssh, "-p", str(port), "-o", "BatchMode=yes",
            f"root@{host}", remote_cmd,
        ],
        check=False, capture_output=True, text=True, timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"ssh {host} command failed (rc={proc.returncode}): "
            f"{proc.stderr.strip()}"
        )
    return proc.stdout


def _get_ens18_ipv4(ssh: str, host: str, port: int, vmid: int) -> str:
    """Query qemu-guest-agent on the VM and return the IPv4 of ens18.

    The cluster's Talos nodes use ens18 as the SDN-facing NIC (the
    only NIC on q35 clones of the template). The agent returns a JSON
    blob; we pick the first non-loopback IPv4.
    """
    raw = _ssh_pve_cmd(
        ssh, host, port,
        f"qm agent {vmid} network-get-interfaces",
        timeout=15,
    )
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"vm {vmid}: qm agent returned non-JSON ({exc.msg}): "
            f"{raw[:200]!r}"
        ) from exc

    for iface in data:
        name = iface.get("name", "")
        if name in ("lo", "loopback"):
            continue
        for addr in iface.get("ip-addresses", []):
            ip = addr.get("ip-address", "")
            family = addr.get("ip-address-type", "")
            if family == "ipv4" and ip:
                return ip
    raise RuntimeError(
        f"vm {vmid}: no IPv4 address found in agent response "
        f"(interfaces: {[i.get('name') for i in data]})"
    )


@contextlib.contextmanager
def _pdns_tunnel(ssh: str, host: str, port: int, local_port: int):
    """Open an SSH -L tunnel to 10.0.0.3:8081 for the duration of the
    block. Cleans up on exit. Yields True if the tunnel came up, False
    if it could not (caller can short-circuit).
    """
    cmd = [
        ssh, "-p", str(port), "-f", "-N", "-M",
        "-o", "BatchMode=yes",
        "-o", "ExitOnForwardFailure=yes",
        "-L", f"{local_port}:{TUNNEL_TARGET_HOST}:{TUNNEL_TARGET_PORT}",
        f"root@{host}",
    ]
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if proc.returncode != 0:
        yield False
        return
    try:
        yield True
    finally:
        # -M master mode means -O exit will close the tunnel.
        subprocess.run(
            [
                ssh, "-p", str(port), "-O", "exit",
                "-o", "BatchMode=yes",
                f"root@{host}",
            ],
            check=False, capture_output=True, text=True,
        )


def _pdns_update_record(
    endpoint: str, api_key: str, zone: str, name: str, rec_type: str,
    ttl: int, value: str,
) -> None:
    """PUT a single rrsets entry to PowerDNS. Replaces any existing
    records of the same name+type in the zone.
    """
    url = f"{endpoint.rstrip('/')}/api/v1/servers/localhost/zones/{zone}"
    payload = {
        "rrsets": [{
            "name": name,
            "type": rec_type,
            "ttl": ttl,
            "changetype": "REPLACE",
            "records": [{"content": value, "disabled": False}],
        }],
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="PATCH",
        headers={
            "X-API-Key": api_key,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status not in (200, 201, 204):
                body = resp.read().decode("utf-8", errors="replace")
                raise RuntimeError(
                    f"PowerDNS PATCH {zone}/{name} returned "
                    f"HTTP {resp.status}: {body!r}"
                )
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"PowerDNS PATCH {zone}/{name} HTTP {exc.code}: {body!r}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"PowerDNS API unreachable at {url}: {exc.reason}"
        ) from exc


def _reverse_name(ip: str, reverse_zone: str) -> str:
    """10.0.0.61 -> 61.0.0.10.in-addr.arpa."""
    octets = ip.split(".")
    if len(octets) != 4:
        raise ValueError(f"not an IPv4 address: {ip!r}")
    return f"{'.'.join(reversed(octets))}.{reverse_zone}"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Sync PowerDNS A/PTR records to the IPs the SDN actually "
            "gave each cluster VM."
        ),
    )
    parser.add_argument(
        "--vmid", action="append", type=int, required=True,
        help=(
            "Proxmox VMID of a cluster node. Repeat for each node. "
            "Must be paired 1:1 with --name in the same order."
        ),
    )
    parser.add_argument(
        "--name", action="append", required=True,
        help=(
            "Short hostname (e.g. cicd-cp-1). The forward record is "
            "<name>.<forward-zone>. Repeat for each node, same order "
            "as --vmid."
        ),
    )
    parser.add_argument(
        "--pve-host", default=DEFAULT_PVE_HOST,
        help=f"PVE SSH host (default: {DEFAULT_PVE_HOST}).",
    )
    parser.add_argument(
        "--pve-ssh-port", type=int, default=DEFAULT_PVE_SSH_PORT,
        help=f"PVE SSH port (default: {DEFAULT_PVE_SSH_PORT}).",
    )
    parser.add_argument(
        "--pdns-endpoint",
        default=f"http://127.0.0.1:{DEFAULT_TUNNEL_LOCAL_PORT}",
        help=(
            "PowerDNS API base URL. Default assumes this script's "
            "SSH tunnel; override only if you already have one open."
        ),
    )
    parser.add_argument(
        "--pdns-api-key",
        default=os.environ.get(DEFAULT_PDNS_API_KEY_ENV, ""),
        help=(
            f"PowerDNS API key. Falls back to env "
            f"{DEFAULT_PDNS_API_KEY_ENV}. Never logged."
        ),
    )
    parser.add_argument(
        "--forward-zone", default=DEFAULT_FORWARD_ZONE,
        help=f"PowerDNS forward zone (default: {DEFAULT_FORWARD_ZONE}).",
    )
    parser.add_argument(
        "--reverse-zone", default=DEFAULT_REVERSE_ZONE,
        help=f"PowerDNS reverse zone (default: {DEFAULT_REVERSE_ZONE}).",
    )
    parser.add_argument(
        "--audit-log", type=Path, default=Path("/tmp/sync_dns_to_sdn.audit.jsonl"),
        help="JSONL audit log path.",
    )
    args = parser.parse_args()

    if len(args.vmid) != len(args.name):
        print(
            f"--vmid and --name must be paired; got "
            f"{len(args.vmid)} vmids and {len(args.name)} names",
            file=sys.stderr,
        )
        return 2

    ssh = shutil.which("ssh")
    if ssh is None:
        print("ssh not on PATH", file=sys.stderr)
        return 2

    if not args.pdns_api_key:
        print(
            f"PowerDNS API key not set (--pdns-api-key or "
            f"{DEFAULT_PDNS_API_KEY_ENV})",
            file=sys.stderr,
        )
        return 2

    logger = StructuredLogger(
        "sync_dns_to_sdn", log_path=args.audit_log, verbose=True,
    )
    logger.info(
        step="start",
        vms=list(zip(args.vmid, args.name)),
        forward_zone=args.forward_zone,
        reverse_zone=args.reverse_zone,
    )

    targets = [VmTarget(vmid=v, name=n) for v, n in zip(args.vmid, args.name)]

    # Open the SSH tunnel to the PowerDNS LXC.
    local_port = DEFAULT_TUNNEL_LOCAL_PORT
    with _pdns_tunnel(ssh, args.pve_host, args.pve_ssh_port, local_port) as up:
        if not up:
            logger.error(
                step="tunnel_open_failed",
                message="could not open SSH tunnel to PowerDNS",
                pve_host=args.pve_host,
            )
            return 3
        logger.info(step="tunnel_opened", port=local_port)

        # For each VM: read the actual ens18 IPv4, then PUT the new A
        # record and the matching PTR. Fail-fast: the first VM that
        # can't be queried aborts the whole run.
        for tgt in targets:
            try:
                ip = _get_ens18_ipv4(
                    ssh, args.pve_host, args.pve_ssh_port, tgt.vmid,
                )
            except RuntimeError as exc:
                logger.error(
                    step="agent_query_failed",
                    message=str(exc),
                    vmid=tgt.vmid,
                    name=tgt.name,
                )
                return 4

            fqdn = f"{tgt.name}.{args.forward_zone}"
            ptr_name = _reverse_name(ip, args.reverse_zone)
            logger.info(
                step="resolved",
                vmid=tgt.vmid, name=tgt.name, ip=ip,
                fqdn=fqdn, ptr=ptr_name,
            )

            try:
                _pdns_update_record(
                    args.pdns_endpoint, args.pdns_api_key,
                    zone=args.forward_zone, name=fqdn, rec_type="A",
                    ttl=300, value=ip,
                )
            except RuntimeError as exc:
                logger.error(
                    step="forward_update_failed",
                    message=str(exc), vmid=tgt.vmid, name=tgt.name,
                )
                return 5
            logger.info(
                step="forward_updated",
                vmid=tgt.vmid, name=tgt.name, fqdn=fqdn, ip=ip,
            )

            try:
                _pdns_update_record(
                    args.pdns_endpoint, args.pdns_api_key,
                    zone=args.reverse_zone, name=ptr_name, rec_type="PTR",
                    ttl=300, value=fqdn,
                )
            except RuntimeError as exc:
                logger.error(
                    step="reverse_update_failed",
                    message=str(exc), vmid=tgt.vmid, name=tgt.name,
                )
                return 5
            logger.info(
                step="reverse_updated",
                vmid=tgt.vmid, name=tgt.name, ptr=ptr_name, fqdn=fqdn,
            )

    logger.info(step="all_done", vms=len(targets))
    return 0


if __name__ == "__main__":
    sys.exit(main())
