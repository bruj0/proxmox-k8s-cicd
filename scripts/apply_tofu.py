"""Operational: apply every tofu root in this repo (tokens + clusters).

Unifies the two previously-separate scripts (scripts/apply.py for Phase 0
infra/tokens and scripts/apply_cluster.py for Phase 2 cluster roots) into a
single entry point.

Targets (positional, required)
  tokens        Phase 0 -- mint Proxmox + Cloudflare scoped tokens via
                infra/tokens. Writes infra/tokens/output.json (mode 0600).
  cicd          Phase 2 -- apply the cicd cluster on BigBertha
                (infra/clusters/cicd).
  apps          Phase 2 -- apply the apps cluster on BigBertha
                (infra/clusters/apps).

Preconditions
  - tofu (>= 1.6.0) on PATH.
  - ./.env at REPO_ROOT with CLOUDFLARE_TOKEN_CREATOR / CLOUDFLARE_ACCOUNT_ID /
    CLOUDFLARE_DOMAIN / PROXMOX_API_URL / PROXMOX_API_TOKEN (and optionally
    CLOUDFLARE_GLOBAL_API_KEY / _EMAIL / CLOUDFLARE_ZONE_ID).
  - For Phase 2 cluster targets: Phase 0 (tokens) and Phase 1 (image bake)
    must have already run -- infra/tokens/output.json and
    build/image-id.txt must be present.

Outputs
  - JSONL audit log:  /tmp/apply_tofu_<target>.audit.jsonl
  - tofu stdout/stderr (tee): logs/apply_<target>_<UTC-stamp>.log
    (gitignored). One log per script invocation; init/plan/apply each
    append a section to the same file with a header.

Secrets NEVER appear on stdout or in the audit log. Only env-var NAMES are
recorded for traceability. The tee'd log may contain tofu-emitted output
(messages, plan diffs); run through
  sed -E 's/(glpat|ghp_|cfut_)[^ "]+/***REDACTED***/g'
before sharing.

Exit codes
   0  success OR help printed (no target given / --help)
   2  prerequisite failure
   3  tofu init failed
   4  tofu plan failed
   5  tofu apply failed
   6  output.json missing after tokens apply
"""

from __future__ import annotations

import argparse
import contextlib
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# Make `tools.lib.*` importable regardless of how this script is launched.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.lib.log import StructuredLogger  # noqa: E402
from tools.lib.secret_loader import SecretLoader  # noqa: E402

# Positional target values. Add new cluster roots here as they appear.
TARGET_TOKENS = "tokens"
TARGET_CLUSTERS = ("cicd", "apps")
ALL_TARGETS = (TARGET_TOKENS, *TARGET_CLUSTERS)

TOKENS_DIR = REPO_ROOT / "infra" / "tokens"
TOKENS_OUTPUT_FILE = TOKENS_DIR / "output.json"

DEFAULT_AUDIT_LOG_PREFIX = "/tmp/apply_tofu"
DEFAULT_LOG_SUBDIR = REPO_ROOT / "logs"


# ---------------------------------------------------------------------------
# .env loader (shared across both targets)
# ---------------------------------------------------------------------------

def _load_env_file(path: Path, logger: StructuredLogger) -> int:
    """Honor .env semantics: KEY=VALUE, optional single/double quotes, drop
    comments and blanks. Never echoes VALUE.

    Uses os.environ.setdefault so anything already on the operator's shell
    takes precedence.
    """
    count = 0
    try:
        text = path.read_text()
    except FileNotFoundError:
        return 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if value.startswith(("'", '"')) and value.endswith(("'", '"')):
            value = value[1:-1]
        os.environ.setdefault(key, value)
        count += 1
    logger.info(step="env_loaded", path=str(path), count=count)
    return count


def _check_prerequisites(target: str, logger: StructuredLogger) -> None:
    """Shared precondition gate. Failures -> RuntimeError -> exit code 2."""
    if shutil.which("tofu") is None:
        raise RuntimeError("tofu (OpenTofu) is not on PATH")
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        raise RuntimeError(
            f".env not found at {env_path}. Create it with "
            "CLOUDFLARE_TOKEN_CREATOR / CLOUDFLARE_ACCOUNT_ID / "
            "CLOUDFLARE_DOMAIN / PROXMOX_API_URL / PROXMOX_API_TOKEN."
        )
    if target != TARGET_TOKENS:
        # Phase 2 only: must have minted credentials and baked an image.
        out_path = TOKENS_OUTPUT_FILE
        if not (out_path.exists() and out_path.read_text().strip()):
            raise RuntimeError(
                f"{out_path} missing or empty -- run "
                f"`{sys.argv[0]} {TARGET_TOKENS}` first (Phase 0)."
            )
        img_path = REPO_ROOT / "build" / "image-id.txt"
        if not (img_path.exists() and img_path.read_text().strip()):
            raise RuntimeError(
                f"{img_path} missing or empty -- Phase 1 (image bake) "
                "must have run before applying a cluster."
            )
    logger.info(
        step="prerequisites_ok",
        target=target,
        tofu=shutil.which("tofu"),
        env=str(env_path),
    )


# ---------------------------------------------------------------------------
# Env translation (shared + per-target)
# ---------------------------------------------------------------------------

def _translate_env_for_tokens(logger: StructuredLogger) -> list[str]:
    """Translate .env vars to TF_VAR_ values for the tokens stack.

    Required env (must be set in .env or pre-exported):
      - CLOUDFLARE_TOKEN_CREATOR  -> TF_VAR_cloudflare_admin_token
      - CLOUDFLARE_ACCOUNT_ID    -> TF_VAR_cloudflare_account_id
      - PROXMOX_API_URL          -> TF_VAR_proxmox_api_url + _endpoint
      - PROXMOX_API_TOKEN        -> id + secret pair
    Optional:
      - CLOUDFLARE_GLOBAL_API_KEY / _EMAIL (preferred over scoped token)
      - CLOUDFLARE_ZONE_ID
    """
    required = ("CLOUDFLARE_TOKEN_CREATOR", "CLOUDFLARE_ACCOUNT_ID",
                "CLOUDFLARE_DOMAIN", "PROXMOX_API_URL", "PROXMOX_API_TOKEN")
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise RuntimeError("missing required env vars: " + ", ".join(missing))

    applied = []
    for src, dst in (
        ("CLOUDFLARE_TOKEN_CREATOR", "TF_VAR_cloudflare_admin_token"),
        ("CLOUDFLARE_ACCOUNT_ID", "TF_VAR_cloudflare_account_id"),
    ):
        os.environ[dst] = os.environ[src]
        applied.append(f"{src}->{dst}")

    url = os.environ["PROXMOX_API_URL"]
    os.environ["TF_VAR_proxmox_api_url"] = url
    os.environ["TF_VAR_proxmox_endpoint"] = url
    applied.append("PROXMOX_API_URL->TF_VAR_proxmox_{api_url,endpoint}")

    full = os.environ["PROXMOX_API_TOKEN"]
    if "=" in full:
        token_id, _, token_secret = full.partition("=")
    else:
        raise RuntimeError(
            "PROXMOX_API_TOKEN must be in 'USER@REALM!TOK=secret' form"
        )
    os.environ["TF_VAR_proxmox_api_token_id"] = token_id
    os.environ["TF_VAR_proxmox_api_token_secret"] = token_secret
    applied.append("PROXMOX_API_TOKEN->TF_VAR_proxmox_api_token_{id,secret}")

    for src, tf_var in (
        ("CLOUDFLARE_GLOBAL_API_KEY", "TF_VAR_cloudflare_global_api_key"),
        ("CLOUDFLARE_GLOBAL_API_EMAIL", "TF_VAR_cloudflare_global_api_email"),
        ("CLOUDFLARE_ZONE_ID", "TF_VAR_cloudflare_zone_id"),
    ):
        if os.environ.get(src):
            os.environ[tf_var] = os.environ[src]
            applied.append(f"{src}->{tf_var}")

    logger.info(step="env_translated", pairs=applied)
    return applied


def _translate_env_for_cluster(logger: StructuredLogger) -> None:
    """Cluster roots consume PROXMOX_VE_* env vars directly (bpg provider
    reads them). No TF_VAR_* splitting needed -- cleaner than Phase 0.

    PowerDNS API key (used by infra/modules/proxmox-k3s-cluster/powerdns.tf
    via the pan-net/powerdns provider) is passed in as a TF_VAR so the
    `sensitive = true` annotation on the module variable keeps it out of
    state and plan output.
    """
    pairs = [
        ("PROXMOX_API_URL", "PROXMOX_VE_ENDPOINT"),
        ("PROXMOX_API_TOKEN", "PROXMOX_VE_API_TOKEN"),
        ("GITLAB_PAT", "GITLAB_ACCESS_TOKEN"),
        ("CLOUDFLARE_TOKEN_CREATOR", "CLOUDFLARE_API_TOKEN"),
        ("POWERDNS_API_KEY", "TF_VAR_powerdns_api_key"),
    ]
    applied = [f"{src}->{dst}" for src, dst in pairs
               if os.environ.get(src) and not os.environ.get(dst)]
    if applied:
        for src, dst in pairs:
            if os.environ.get(src) and not os.environ.get(dst):
                os.environ[dst] = os.environ[src]
    logger.info(step="env_translated", pairs=applied)


# ---------------------------------------------------------------------------
# Tofu runner (shared). Per-cluster run uses a different TF_DIR + tee path;
# per-run state lives on the dataclass instance.
# ---------------------------------------------------------------------------

@dataclass
class TofuRunner:
    target: str
    tf_dir: Path
    log_subdir: Path
    audit_log_path: Path
    plan_only: bool
    auto_approve: bool
    logger: StructuredLogger

    # Lazy: same logfile is reused across init/plan/apply within one process.
    run_log_path: Path | None = None

    def _ensure_logfile(self) -> Path:
        if self.run_log_path is not None:
            return self.run_log_path
        self.log_subdir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.run_log_path = self.log_subdir / f"apply_{self.target}_{stamp}.log"
        self.run_log_path.touch()
        return self.run_log_path

    def _run(self, substep: str, *args: str) -> int:
        """Run a tofu subcommand in tf_dir. Output is BOTH streamed to the
        operator's terminal AND appended to logs/apply_<target>_<stamp>.log.
        """
        cmd = ("tofu",) + args
        self.logger.info(
            step="tofu_cmd", target=self.target, substep=substep, cmd=list(cmd)
        )

        log_file = self._ensure_logfile()
        header = (
            f"\n===== tofu {' '.join(args)} | target={self.target} "
            f"| substep={substep} | logfile={log_file} =====\n"
        )
        with log_file.open("a", encoding="utf-8") as fh:
            fh.write(header)
        print(header, flush=True)

        with log_file.open("a", encoding="utf-8") as log_fh:
            proc = subprocess.run(
                cmd,
                cwd=self.tf_dir,
                env=os.environ.copy(),
                check=False,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
            )

        # Echo the captured body to the operator's terminal so they see
        # tofu's output even though we redirected stdout/stderr above.
        body = log_file.read_text(encoding="utf-8")
        body_after = body[len(header):] if body.startswith(header) else body
        if body_after.strip():
            print(body_after, flush=True)

        return proc.returncode

    def init(self) -> bool:
        if self._run("init", "init", "-backend=false", "-input=false",
                     "-no-color") != 0:
            self.logger.error(
                step="init_failed",
                target=self.target,
                error="tofu init returned non-zero",
                resolution=(
                    "inspect the tee'd log file or the tofu output "
                    "above; usually a missing backend variable or "
                    "an auth misconfiguration"
                ),
            )
            return False
        return True

    def plan(self, *extra: str, plan_out: Path | None = None) -> bool:
        args = ["plan", "-no-color", "-input=false", "-parallelism=1", *extra]
        if plan_out is not None:
            args.append(f"-out={plan_out}")
        rc = self._run("plan", *args)
        if rc != 0:
            self.logger.error(
                step="plan_failed",
                target=self.target,
                rc=rc,
                error="tofu plan returned non-zero",
                resolution=(
                    "inspect the log; confirm the per-target env "
                    "translations succeeded (run with --verbose)"
                ),
            )
            return False
        self.logger.info(step="plan_ok", target=self.target)
        return True

    def apply(self, plan_file: Path | None = None) -> bool:
        if self.auto_approve:
            args = ["apply", "-auto-approve", "-no-color", "-input=false", "-parallelism=1"]
        else:
            args = ["apply", "-no-color", "-input=false", "-parallelism=1", str(plan_file)]
        rc = self._run("apply", *args)
        if rc != 0:
            self.logger.error(
                step="apply_failed",
                target=self.target,
                rc=rc,
                error="tofu apply returned non-zero",
                resolution=(
                    "inspect the log; if a planfile was generated, retry "
                    "with `tofu apply <planfile>`"
                ),
            )
            return False
        self.logger.info(step="apply_ok", target=self.target)
        return True


# ---------------------------------------------------------------------------
# Per-target plan/apply pipelines
# ---------------------------------------------------------------------------

def _apply_tokens(runner: TofuRunner) -> int:
    if not runner.init():
        return 3
    if not runner.plan():
        return 4
    if runner.plan_only:
        runner.logger.info(step="plan_only_done", target=runner.target)
        return 0
    if not runner.apply():
        return 5
    if TOKENS_OUTPUT_FILE.exists() and TOKENS_OUTPUT_FILE.stat().st_size > 0:
        mode = oct(TOKENS_OUTPUT_FILE.stat().st_mode & 0o777)
        runner.logger.info(
            step="output_json_ok",
            path=str(TOKENS_OUTPUT_FILE),
            mode=mode,
        )
    else:
        runner.logger.error(
            step="output_json_missing",
            error=f"{TOKENS_OUTPUT_FILE} not written",
            resolution=(
                "the local_sensitive_file resource in "
                "infra/tokens/output_json.tf did not produce a file; "
                "check tofu output in the tee'd log"
            ),
        )
        return 6
    runner.logger.info(
        step="all_done",
        target=runner.target,
        output_json=str(TOKENS_OUTPUT_FILE),
        next_step="Run tofu test in infra/tokens to validate the rotation is a no-op",
    )
    return 0


# ---------------------------------------------------------------------------
# SSH tunnel to PowerDNS via PVE.
#
# The SDN-internal PowerDNS API (10.0.0.3:8081) is only routable from
# inside BigBertha's vnet. The pan-net/powerdns provider connects from
# the operator host (where tofu runs). Solution: an SSH local-forward
# `ssh -L 8081:10.0.0.3:8081 root@kvm.bruj0.net` for the duration of
# the apply. The provider config points at `http://127.0.0.1:8081`.
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _powerdns_tunnel(logger: StructuredLogger):
    """Open ssh -L 8081:10.0.0.3:8081 root@<pve_host> and yield.

    Cleanup is best-effort: we record the PID and kill the ssh process on
    exit. If the tunnel is already up (e.g. from a previous run that
    crashed), we don't open a second one.
    """
    ssh = shutil.which("ssh")
    if ssh is None:
        logger.warn(
            message="PowerDNS tunnel skipped: ssh not on PATH",
            step="tunnel_skipped",
            reason="ssh not on PATH",
        )
        yield False
        return
    ssh_port = os.environ.get("PVE_SSH_PORT", "6022")
    pve_host = os.environ.get("PVE_HOST", "kvm.bruj0.net")
    local_port = os.environ.get("POWERDNS_LOCAL_PORT", "8081")

    # Check if the local port is already bound -- another tunnel is up.
    proc = subprocess.run(
        ["ss", "-ltn", f"sport = :{local_port}"],
        check=False, capture_output=True, text=True,
    )
    if f":{local_port} " in proc.stdout:
        logger.info(step="tunnel_already_up", port=local_port)
        yield True
        return

    cmd = [
        ssh, "-p", ssh_port, "-f", "-N", "-M", "-o", "BatchMode=yes",
        "-o", "ExitOnForwardFailure=yes",
        "-L", f"{local_port}:10.0.0.3:{local_port}",
        f"root@{pve_host}",
    ]
    logger.info(step="tunnel_opening", cmd=cmd, port=local_port)
    rc = subprocess.run(cmd, check=False).returncode
    if rc != 0:
        logger.warn(
            message="PowerDNS SSH tunnel failed to open",
            step="tunnel_open_failed",
            rc=rc,
            resolution=(
                "PowerDNS API at 127.0.0.1:8081 won't be reachable; "
                "DNS records may not converge. Check SSH access to PVE."
            ),
        )
        yield False
        return
    logger.info(step="tunnel_opened", port=local_port, pve=pve_host)
    try:
        yield True
    finally:
        # Tear down: kill the ssh master + forwarded session.
        subprocess.run(
            ["pkill", "-f", f"ssh.*-L {local_port}:10.0.0.3"],
            check=False, capture_output=True,
        )
        logger.info(step="tunnel_closed", port=local_port)


def _apply_cluster(runner: TofuRunner) -> int:
    """Cluster phase: init -> plan -out planfile -> apply planfile.

    For clusters we always go through the planfile (no -auto-approve) so
    the operator can see the diff in the tee'd log before it's applied.
    Use --auto-approve to bypass the planfile and apply -auto-approve directly.

    PowerDNS tunnel is opened for the whole phase so plan + apply both see
    the API. If the tunnel can't be opened, plan/apply still runs (the
    records just won't converge) -- the broken hosts resources we removed
    were already a no-op, so this is no worse than before.
    """
    if not runner.init():
        return 3
    plan_path = Path(f"/tmp/apply_tofu_{runner.target}.tfplan")
    with _powerdns_tunnel(runner.logger):
        if not runner.plan(plan_out=plan_path):
            return 4
        if runner.plan_only:
            runner.logger.info(step="plan_only_done", target=runner.target)
            return 0
        if not runner.apply(plan_file=plan_path):
            return 5

    # Probe live VM memory via SSH for visibility.
    ssh = shutil.which("ssh")
    if ssh is not None:
        ssh_port = os.environ.get("PVE_SSH_PORT", "6022")
        pve_host = os.environ.get("PVE_HOST", "kvm.bruj0.net")
        try:
            res = subprocess.run(
                [
                    ssh, "-p", ssh_port, "-o", "BatchMode=yes",
                    f"root@{pve_host}",
                    "qm list | grep -E '  *1[1-4][1-4] .*(cicd|apps)-' || true",
                ],
                check=False, capture_output=True, text=True, timeout=20,
            )
            runner.logger.info(
                step="vm_memory_after_apply",
                target=runner.target,
                qm_list=res.stdout.strip(),
            )
            if res.stdout.strip():
                print(res.stdout)
        except subprocess.TimeoutExpired:
            runner.logger.warn(step="probe_timeout", target=runner.target)
    else:
        runner.logger.warn(step="probe_skipped", reason="ssh not on PATH")

    runner.logger.info(step="all_done", target=runner.target)
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="apply_tofu.py",
        description=(
            "Apply every tofu root in this repo (Phase 0 tokens + Phase 2 "
            "clusters) from a single entry point. Run with no target to "
            "see this help screen."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_HELP_EPILOG,
    )
    p.add_argument(
        "target",
        choices=ALL_TARGETS,
        nargs="?",
        default=None,
        help=(
            "What to apply. `tokens` = Phase 0 (mint Proxmox + Cloudflare "
            "scoped tokens). `cicd` / `apps` = Phase 2 (apply that cluster "
            "root)."
        ),
    )
    p.add_argument(
        "--plan-only",
        action="store_true",
        help="Run plan and stop; do not apply.",
    )
    p.add_argument(
        "--auto-approve",
        action="store_true",
        help=(
            "Pass -auto-approve through to tofu apply. Default (without "
            "this flag) is to write a planfile and read it back, so the "
            "operator can review the tee'd diff before it lands."
        ),
    )
    p.add_argument(
        "--audit-log",
        type=Path,
        default=None,
        help=(
            "JSONL audit log path. Defaults to "
            f"{DEFAULT_AUDIT_LOG_PREFIX}_<target>.audit.jsonl. The audit "
            "log records step names, env-var NAMES (never values), and "
            "tofu return codes. Consult it via `jq -r '.level+\"\t\"+.step'`."
        ),
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="Reserved for future use (no-op today).",
    )
    return p


# Filled in lazily after the parser is built so the text is exercised.
_HELP_EPILOG = """
\
Examples
--------
  uv run scripts/apply_tofu.py tokens                   # Phase 0
  uv run scripts/apply_tofu.py tokens --plan-only       # See the diff only
  uv run scripts/apply_tofu.py cicd                     # Phase 2 cluster
  uv run scripts/apply_tofu.py cicd --plan-only         # Diff, no apply
  uv run scripts/apply_tofu.py apps --auto-approve      # Skip planfile review
  uv run scripts/apply_tofu.py                          # This help screen

Related scripts
---------------
  scripts/apply_tofu.py     this script (Phase 0 + Phase 2 unified)
  tools/build_image.py     Phase 1 -- bake Talos template into Proxmox
  tools/bootstrap_cluster.py  Phase 3 -- post-tofu k3s bootstrap

Log files
---------
  Audit JSONL:  /tmp/apply_tofu_<target>.audit.jsonl
  Run log:      logs/apply_<target>_<UTC-stamp>.log   (gitignored)

If you see a tofu-emitted secret in the run log (e.g. a glpat/cfut/cf-token)
redact it before sharing with:
  sed -E 's/(glpat|ghp_|cfut_)[^ "]+/***REDACTED***/g' \\
      logs/apply_<target>_<stamp>.log
"""


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # No-args case: print one-page manual to stdout, exit 0. Mirrors
    # how `apply_tofu.py --help` already behaves — uniform operator UX.
    if args.target is None:
        parser.print_help()
        print()  # trailing newline for readability
        return 0

    # Derive per-target logging paths.
    audit_log = args.audit_log or Path(
        f"{DEFAULT_AUDIT_LOG_PREFIX}_{args.target}.audit.jsonl"
    )

    logger = StructuredLogger(f"apply_tofu.{args.target}", log_path=audit_log)
    secrets = SecretLoader(logger)

    try:
        _check_prerequisites(args.target, logger)
        _load_env_file(REPO_ROOT / ".env", logger)
        if args.target == TARGET_TOKENS:
            _translate_env_for_tokens(logger)
            secrets.get_many(
                [
                    "TF_VAR_cloudflare_admin_token",
                    "TF_VAR_cloudflare_account_id",
                    "TF_VAR_proxmox_api_token_id",
                    "TF_VAR_proxmox_api_token_secret",
                    "TF_VAR_proxmox_api_url",
                ]
            )
        else:
            _translate_env_for_cluster(logger)
            secrets.get_many(
                [
                    "PROXMOX_VE_ENDPOINT",
                    "PROXMOX_VE_API_TOKEN",
                    "GITLAB_ACCESS_TOKEN",
                ]
            )
    except RuntimeError as exc:
        logger.error(
            step="prerequisites_failed",
            target=args.target,
            error=str(exc),
            resolution=(
                "fix the missing precondition (tofu on PATH, .env at "
                "REPO_ROOT, required vars present per this script's "
                "docstring) and retry"
            ),
        )
        return 2

    runner = TofuRunner(
        target=args.target,
        tf_dir=(
            TOKENS_DIR if args.target == TARGET_TOKENS
            else REPO_ROOT / "infra" / "clusters" / args.target
        ),
        log_subdir=DEFAULT_LOG_SUBDIR,
        audit_log_path=audit_log,
        plan_only=args.plan_only,
        auto_approve=args.auto_approve,
        logger=logger,
    )

    if args.target == TARGET_TOKENS:
        return _apply_tokens(runner)
    return _apply_cluster(runner)


if __name__ == "__main__":
    sys.exit(main())
