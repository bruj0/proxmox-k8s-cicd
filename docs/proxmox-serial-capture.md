# Proxmox VE Serial Console Capture

> **Status (2026-07-07, post-v2-cleanup)**: this recipe is **debug-only**.
> The `tools/build_image` build flow no longer needs serial capture
> (the canonical Proxmox+Ubuntu recipe bakes qemu-guest-agent into the
> image BEFORE the VM is created, so `qm agent <vmid> ping` returns
> within ~10 s of boot and the agent channel is the authoritative
> health signal). Use this recipe only when a build fails
> mysteriously and the JSONL audit log doesn't show the root cause,
> or when troubleshooting a manual clone.

How to read the serial console of a PVE VM from another host or inside an automation
script. Useful for diagnosing boot failures and watching kernel/initramfs output
without needing a VNC/xterm.js session.

## 1. What "serial console" actually means in PVE 9.x

A VM that has `serial0: socket` in its config exposes its emulated UART0 as
a Unix socket at:

    /var/run/qemu-server/<vmid>.serial0

There is **no separate per-VM qga socket vs. serial socket** — qga lives on a
different chardev (added by `--agent enabled=1`, backed by
`/var/run/qemu-server/<vmid>.qga`).

The boot output that ends up on this serial port depends entirely on the
guest kernel cmdline. With the Ubuntu 24.04 Noble cloud image the default
`/boot/grub/grub.cfg` already includes both:

    console=tty1 console=ttyS0

so the kernel printk stream **does** appear on ttyS0. The cloud-init
initramfs shell (when reached) also writes to ttyS0.

Note: `vga: serial0` only redirects the **firmware** console (OVMF/SeaBIOS).
It does NOT mirror the running kernel's tty0 output. The kernel still needs
`console=ttyS0` on the cmdline to push to the chardev.

## 2. What actually works (verified 2026-07-07 against PVE 9.2.3)

### The exact PVE invocation

PVE's `qm terminal <vmid> -iface serial0` does this:

```perl
# PVE/CLI/qm.pm
my $cmd = "socat UNIX-CONNECT:$socket STDIO,raw,echo=0$escape";
system($cmd);
```

So the canonical command is:

    socat UNIX-CONNECT:/var/run/qemu-server/<vmid>.serial0 STDIO,raw,echo=0

### Why naive socat from an automation script fails

The `STDIO,raw,echo=0` part tells socat to put the local stdin/stdout into
a raw tty mode and call `tcgetattr(0, ...)` to query it. **Without a real
pty at the socat stdin, socat exits immediately with `tcgetattr: Inappropriate ioctl`**.

This works in an interactive PVE shell session because the user's terminal
provides that pty. It does NOT work over `ssh -o BatchMode=yes ...` (a
non-tty ssh session), nor with stdin redirected from `/dev/null`, nor with
stdin from a regular file.

### Working recipe (used in this repo)

A small Python helper that opens a real pty via `pty.openpty()`, hands the
slave end to socat, and pipes whatever the master fd yields into a file:

    # scripts/capture_serial.py — deployed at /tmp/capture_serial.py on PVE

It reuses the EXACT PVE invocation (`socat ... STDIO,raw,echo=0`) so
`tcgetattr` succeeds, and writes captured bytes to a file.

Verified flow:

1. SSH in, run the helper for the duration you need, **before** you trigger
   any reset/start. The helper holds the chardev connection open.
2. From a SECOND shell, `qm reset <vmid>` (NOT `qm stop` — stopping tears
   down the chardev and you'd capture EOF immediately).
3. The capture file fills with OVMF/SeaBIOS banner → GRUB → kernel printk
   → initramfs messages.

Total example (for VM 951, PVE host `kvm.bruj0.net`, ssh on port 6022):

    # shell 1
    ssh -o BatchMode=yes -p 6022 root@kvm.bruj0.net \
        'python3 /tmp/capture_serial.py --vmid 951 --out /tmp/cap.log --duration 90'

    # shell 2 (run a few seconds later)
    ssh -o BatchMode=yes -p 6022 root@kvm.bruj0.net 'qm reset 951'

After 60s, `/tmp/cap.log` on the PVE host contains ~50 KB of OCR-friendly
kernel boot log (use `cat -v | head -c N` to read it without CR/ESC noise).

## 3. Mistakes that don't work (documented so we don't re-try)

These were tried and all gave zero captured bytes despite the chardev being
reachable:

- `socat ... PIPE:/tmp/x.pipe,ignoreeof` — works in principle, but if the
  pipe reader subprocess dies (because the parent shell exited) the writer
  gets `Connection reset by peer`. Don't background the reader.
- `socat ... OPEN:/tmp/x.log,append,create` — socat opens the file for both
  reading and writing. Reading an empty regular file hits EOF, which causes
  socat to shut down both sides cleanly with rc=0. Symptom: instant exit,
  zero bytes in the log.
- A reader process that does `cat /tmp/x.pipe > log` outside the ssh
  session — when the ssh session closes, the reader is killed by the kernel
  (SIGHUP via pgrp), the pipe breaks, the writer exits, no data captured.
- `&`-backgrounding the Python helper inside a single-`ssh` heredoc: the
  helper inherits stdout/stderr from the bash that ran the heredoc; when
  the bash exits, the helper is killed before it writes anything.
- `setsid`, `nohup ... &`, `screen -dmS`, `systemd-run` — all variants of
  detaching from the ssh session failed the same way: the helper died with
  the closing of its controlling tty/session.

## 4. Why the second shell matters

`qm stop <vmid>` destroys the QEMU process and the unix chardev socket. Any
socat connected to it gets EOF immediately. So you must capture AFTER
start, not around a stop+start cycle. `qm reset` re-executes the VM in place
and the chardev survives.

If you only have one shell available, the trick is:

1. Spawn the capture as a real long-running process (via tmux or a systemd
   unit), or
2. Use `qm reset` from the same shell where capture is running.

In practice tmux/systemd hosts require extra setup; we've shipped
`scripts/capture_serial.py` to be invoked in a one-shot ssh from shell 1
and then triggered from shell 2 with `qm reset`.

## 5. Operational checks

After capture completes, confirm:

    ls -la /tmp/cap.log      # file size > 1 KB for a successful boot
    cat -v /tmp/cap.log | head    # first line should be SeaBIOS/OVMF banner

Useful greps:

    grep -aE 'Command line|Kernel command|initramfs|ALERT|Gave up|RAMDISK' \
        /tmp/cap.log

## 6. What we know now about the Ubuntu Noble cloud image on this host

Captured boot of a freshly-imported `noble-server-cloudimg-amd64.img` on
`data1` (lvm-thin) with OVMF + `vga: serial0` + `serial0: socket`:

- SeaBIOS banner appears (we're using the default BIOS, not OVMF, for VM 951).
- Kernel `Command line: BOOT_IMAGE=/vmlinuz-6.8.0-124-generic root=LABEL=cloudimg-rootfs ro console=tty1 console=ttyS0` is **printed correctly**.
- `RAMDISK: ...` and `Trying to unpack rootfs image as initramfs...` appear.
- `Begin: Mounting root file system ... Begin: Running /scripts/local-top ... done.` — boot then **hangs** here.

The hang at `local-top` is the cloud-init seed hook (the cloud-init
initramfs-local-top script tries to find the CDROM seed). This is the next
thing to chase, but distinct from "kernel can't find rootfs" — the kernel
and ramdisk load fine.

The user's earlier report of `LABEL=cloudimg-rootfs does not exist` was
from a different boot attempt — likely one where the seed ISO was attached
and the kernel tried to mount `LABEL=cloudimg-rootfs` directly without
going through cloud-init's normal path.
