"""Capture PVE VM serial output to a file via the same path PVE's xterm.js uses.

PVE's `qm terminal <vmid> -iface serial0` invokes:
    socat UNIX-CONNECT:/var/run/qemu-server/$vmid.serial0 STDIO,raw,echo=0

Our wrapper mirrors that but writes to a file, and accepts stdio from a pty
so tcgetattr succeeds. We add `-u` to socat to use unbuffered I/O and
write all captured bytes line-buffered to the output file.

Usage:
    scp capture_serial.py root@kvm.bruj0.net:/tmp/
    ssh root@kvm.bruj0.net python3 /tmp/capture_serial.py --vmid 951 --out /tmp/cap.log
"""
from __future__ import annotations

import argparse
import os
import pty
import select
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vmid", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--duration", type=int, default=120)
    ap.add_argument("--socat", default="/usr/bin/socat")
    args = ap.parse_args()

    socat = shutil.which("socat") or args.socat
    if not os.path.exists(socat):
        print(f"socat not found at {socat}", file=sys.stderr)
        return 2

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    out_fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    os.write(out_fd, b"[capture] starting\n")

    master_fd, slave_fd = pty.openpty()

    cmd = [
        "/bin/bash", "-c",
        # Outer loop respawns socat when it dies (e.g. chardev
        # briefly gone during qm reset, or VM stopped). Inner
        # until-loop retries the chardev until it appears.
        f"while true; do "
        f"until socat -u UNIX-CONNECT:/var/run/qemu-server/{args.vmid}.serial0 "
        f"STDIO,raw,echo=0; do sleep 1; done; "
        f"done",
    ]

    sys.stderr.write(f"[capture] spawning: {' '.join(cmd)}\n")
    proc = subprocess.Popen(
        cmd,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        close_fds=True,
    )
    os.close(slave_fd)

    deadline = time.monotonic() + args.duration

    def handle_signal(signum, _frame):
        raise KeyboardInterrupt()

    signal.signal(signal.SIGTERM, handle_signal)

    try:
        consecutive_pty_eofs = 0
        while time.monotonic() < deadline:
            rlist, _, _ = select.select([master_fd], [], [], 0.5)
            if master_fd in rlist:
                try:
                    data = os.read(master_fd, 4096)
                except OSError as exc:
                    sys.stderr.write(f"[capture] pty OSError: {exc}\n")
                    data = b""
                if data:
                    consecutive_pty_eofs = 0
                    os.write(out_fd, data)
                else:
                    # Transient EOF: bash/socat died and is being
                    # respawned by the outer loop. Don't quit; just
                    # wait for data to flow again.
                    consecutive_pty_eofs += 1
                    sys.stderr.write(
                        f"[capture] pty EOF (count={consecutive_pty_eofs})\n"
                    )
                    # If we keep getting EOFs forever (bash exited),
                    # the proc.poll() check below catches it.
                    time.sleep(1.0)
            if proc.poll() is not None:
                sys.stderr.write(
                    f"[capture] wrapper exited rc={proc.returncode}\n"
                )
                break
    except KeyboardInterrupt:
        sys.stderr.write("[capture] interrupted\n")
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        for fd in (master_fd, out_fd):
            try:
                os.close(fd)
            except Exception:
                pass

    sys.stderr.write("[capture] done\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
