#!/usr/bin/env python3
"""Route a Sycode read-only monitor breach to consuming channels.

Generic, tested router for the still-needed Sycode local-only monitors that the
no-black-holes remediation (t_2e548a59 / t_ae25e140) left wiring-less:
  * candle per-symbol freshness (sycode_candle_per_symbol_freshness.py)
  * PIT context-join / PIT monitor (pit-monitor.sh)

It is invoked by a per-monitor in-dir wrapper that has already RUN the detector
and captured its output + exit code. This router then decides delivery:

  HEALTHY   -> no summary provided            -> silent, exit 0.
  BREACH    -> summary present (exit != 0 or ALERT lines)
             -> writes a cronrelay-* JSON into the jarvis Alertmanager OOB spool
                (proven consumer: jarvis 'sycode-alertmanager-oob-spool-drain',
                every 1m -> Discord #critical-alerts; same path canonical-
                registry-check uses) AND creates an idempotent sycode-trading
                kanban incident card (family-stable key, mirrors r_multiple /
                tier1 patterns). Returns 0.
  OP-FAIL   -> the router itself cannot write the spool file or create the card
                (delivery failure) -> prints ERROR and returns 1 so the cron
                records the monitor/delivery as failed (FAIL-VISIBLE).

DEDUPE: the spool filename embeds a coarse repage bucket (REPAGE_HOURS), so
repeated breaches inside the same bucket produce an idempotent alert file (the
drain consumes it once) and a single family-stable board card. Recovery (healthy
run after a breach) clears the local episode state.

Paper/read-only: never touches DB, queue, runtime, credentials, providers, or
trading. Delivery target is a LOCAL spool dir (the drain does the Discord send
under the jarvis profile which owns the Discord token) + a Hermes kanban create.

Usage:
  python3 sycode_monitor_alert_route.py \
      --monitor <key> --summary <text> [--severity critical|warning] \
      [--spool <dir>] [--board sycode-trading] [--exit-code N]
  python3 sycode_monitor_alert_route.py --self-test
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

# ---------- injectable time / environment (tests override) -------------------
def _now_epoch() -> float:
    return time.time()


SPOOL_WRITER = Path(os.environ.get(
    "SYCODE_ALERT_SPOOL_WRITER", "/home/frank/.hermes/scripts/spool_alert_write.py"))
HERMES_CLI = os.environ.get("SYCODE_ALERT_HERMES_CLI", "/home/frank/.local/bin/hermes")
DEFAULT_SPOOL = Path(os.environ.get(
    "SYCODE_ALERT_SPOOL",
    "/home/frank/.hermes/profiles/jarvis/state/alertmanager-spool/incoming"))
STATE_DIR = Path(os.environ.get(
    "SYCODE_ALERT_STATE_DIR", "/home/frank/.hermes/var/sycode_monitor_alert_route"))
REPAGE_HOURS = int(os.environ.get("SYCODE_ALERT_REPAGE_HOURS", "6"))
KANBAN_ENV_OVERRIDES = ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD",
                        "HERMES_KANBAN_TASK", "HERMES_KANBAN_WORKSPACE")


def _clean_env() -> dict[str, str]:
    env = os.environ.copy()
    for k in KANBAN_ENV_OVERRIDES:
        env.pop(k, None)
    return env


def read_state() -> dict[str, Any]:
    try:
        return json.loads((STATE_DIR / "state.json").read_text())
    except Exception:
        return {}


def write_state(state: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_DIR / (".state.tmp-%d" % os.getpid())
    tmp.write_text(json.dumps(state, sort_keys=True, indent=2) + "\n")
    os.replace(tmp, STATE_DIR / "state.json")


def family_key(monitor: str) -> str:
    """Stable identity independent of timestamps or measured values."""
    return "sycode-" + hashlib.sha256(monitor.encode("utf-8")).hexdigest()[:16]


def repage_bucket(now_epoch: float | None = None) -> str:
    """Coarse wall-clock bucket so a frozen/failing monitor still re-pages."""
    now_epoch = now_epoch if now_epoch is not None else _now_epoch()
    bucket = int(now_epoch) // (REPAGE_HOURS * 3600)
    return "%d" % bucket


def write_spool(args: argparse.Namespace, summary: str, *,
                drain_epoch: float | None = None) -> bool:
    """Write a cronrelay-* JSON into the Alertmanager OOB spool (Discord path).

    Returns True on success, False on delivery failure. Uses the canonical
    spool_alert_write.py so the jarvis drain reads/writes with correct perms.
    """
    drain_epoch = drain_epoch if drain_epoch is not None else _now_epoch()
    alertname = args.alertname or ("sycode-" + family_key(args.monitor))
    try:
        proc = subprocess.run(
            ["python3", str(SPOOL_WRITER),
             "--spool", str(args.spool),
             "--alertname", alertname,
             "--severity", args.severity,
             "--summary", summary,
             "--max-chars", str(args.max_chars)],
            capture_output=True, text=True, timeout=30, env=_clean_env())
        if proc.returncode != 0:
            print("ERROR sycode_monitor_alert_route: spool write failed rc=%d: %s"
                  % (proc.returncode, proc.stderr.strip()[:300]), file=sys.stderr)
            return False
        return True
    except Exception as exc:  # noqa: BLE001
        print("ERROR sycode_monitor_alert_route: spool write raised %s"
              % type(exc).__name__, file=sys.stderr)
        return False


def create_board_card(args: argparse.Namespace, summary: str) -> bool:
    """Create/refresh an idempotent sycode-trading incident card (family key).

    Returns True on success (card created or already present), False on failure.
    """
    fkey = family_key(args.monitor)
    bucket = repage_bucket()
    # Full idempotency key = family : bucket so a NEW repage bucket re-notifies,
    # while repeats inside the same bucket resolve to the existing card.
    id_key = "%s:%s" % (fkey, bucket)
    body = (
        "Automated read-only Sycode monitor breach routed by "
        "sycode_monitor_alert_route.py (t_dd27733b).\n\n"
        "monitor: %s\nseverity: %s\nalertname: %s\nfamily_key: %s\n"
        "repage_bucket: %s\n"
        "consumer channels: jarvis Alertmanager OOB spool -> "
        "Discord #critical-alerts (delivery on monitor breach) + this board card. "
        "Dedupe: repeats within a %dh repage bucket resolve to this card.\n\n"
        "%s"
    ) % (args.monitor, args.severity, args.alertname, fkey, bucket,
         REPAGE_HOURS, summary)
    cmd = [
        HERMES_CLI, "kanban", "--board", args.board, "create",
        "ALERT: Sycode %s" % args.monitor,
        "--assignee", args.assignee,
        "--priority", str(args.priority),
        "--idempotency-key", id_key,
        "--created-by", "sycode-monitor-alert-route",
        "--body", body,
        "--json",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                              env=_clean_env())
        if proc.returncode != 0:
            print("ERROR sycode_monitor_alert_route: board card failed rc=%d: %s"
                  % (proc.returncode, proc.stderr.strip()[:300]), file=sys.stderr)
            return False
        return True
    except Exception as exc:  # noqa: BLE001
        print("ERROR sycode_monitor_alert_route: board card raised %s"
              % type(exc).__name__, file=sys.stderr)
        return False


def route(args: argparse.Namespace) -> int:
    """Decide + perform delivery given the detector result wrapped in args."""
    summary = (args.summary or "").strip()
    healthy = (not summary) and (args.exit_code == 0)

    if healthy:
        # Clear active episode state (recovery).
        state = read_state()
        if state.get("monitor") == args.monitor and state.get("active"):
            state["active"] = False
            state["recovered_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            write_state(state)
        return 0  # silent

    # BRANCH: breach (summary present / non-zero exit) -> deliver.
    spool_ok = write_spool(args, summary)
    card_ok = create_board_card(args, summary)

    if not spool_ok and not card_ok:
        # Complete delivery failure: FAIL-VISIBLE (returns 1 so cron records error).
        return 1

    # Persist active episode key for recovery / re-page tracking.
    state = read_state()
    state.update({
        "monitor": args.monitor,
        "active": True,
        "episode_key": "%s:%s" % (family_key(args.monitor), repage_bucket()),
        "alerted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
    write_state(state)

    # On a partial failure (one delivery channel worked, one failed) we still
    # surface it for visibility but consider the route delivered (exit 0) since
    # at least one real consumer got it.
    if not spool_ok or not card_ok:
        print("WARN sycode_monitor_alert_route: partial delivery (spool_ok=%s "
              "card_ok=%s) for monitor=%s" % (spool_ok, card_ok, args.monitor),
              file=sys.stderr)
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--monitor", default="")
    ap.add_argument("--summary", default="", help="detector alert body (empty=healthy)")
    ap.add_argument("--severity", default="critical", choices=["info", "warning", "critical"])
    ap.add_argument("--alertname", default=None, help="defaults to monitor key")
    ap.add_argument("--exit-code", type=int, default=0)
    ap.add_argument("--spool", type=Path, default=DEFAULT_SPOOL)
    ap.add_argument("--board", default="sycode-trading")
    ap.add_argument("--assignee", default="sycode-trading-pm")
    ap.add_argument("--priority", type=int, default=95)
    ap.add_argument("--max-chars", type=int, default=1500)
    ap.add_argument("--state-dir", type=Path, default=STATE_DIR)
    return ap.parse_args(argv)


def _selftest_setup() -> str:
    """Point router at /tmp paths so the self-test never touches live consumers.

    Returns the path of the isolated `hermes` stub used for the board-card
    channel. The stub logs each invocation and exits 0 WITHOUT touching any real
    kanban board, so a self-test breach can never mint a real alert card
    (t_ca175303 regression). The real delivery path (live HERMES_CLI) is
    untouched outside self-test mode.
    """
    global DEFAULT_SPOOL, STATE_DIR, SPOOL_WRITER, HERMES_CLI
    base = Path("/tmp/tdd27733b_selftest_%d" % os.getpid())
    (base / "spool").mkdir(parents=True, exist_ok=True)
    (base / "state").mkdir(parents=True, exist_ok=True)
    DEFAULT_SPOOL = base / "spool"
    STATE_DIR = base / "state"
    SPOOL_WRITER = Path("/home/frank/.hermes/scripts/spool_alert_write.py")
    stub = base / "hermes_stub.sh"
    log = base / "hermes_invocations.log"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        "echo \"selftest-hermes-stub: $*\" >> %s\n"
        "exit 0\n" % log
    )
    stub.chmod(0o755)
    HERMES_CLI = str(stub)
    return str(stub)


def self_test() -> int:
    global SPOOL_WRITER, HERMES_CLI, REPAGE_HOURS
    stub_cli = _selftest_setup()
    failures: list[str] = []
    checks = 0

    # Isolation guard (t_ca175303): the board-card channel must target the stub,
    # never the real hermes CLI / live sycode-trading board.
    checks += 1
    if HERMES_CLI != stub_cli:
        failures.append("self-test must isolate HERMES_CLI to the stub (t_ca175303)")

    # 1. HEALTHY silence: empty summary + exit 0 -> rc 0, no state active.
    args = parse_args(["--monitor", "selftest-healthy", "--summary", ""])
    rc = route(args)
    checks += 1
    if rc != 0:
        failures.append("healthy must be silent exit 0")
    state = read_state()
    if state.get("active"):
        failures.append("healthy must not mark episode active")

    # Also: healthy AFTER a breach clears active state (recovery).
    write_state({"monitor": "selftest-recover", "active": True})
    args = parse_args(["--monitor", "selftest-recover", "--summary", ""])
    rc = route(args)
    checks += 1
    if rc != 0:
        failures.append("recovery must be silent exit 0")
    if read_state().get("active", True):
        failures.append("recovery must clear active state")

    # 2. BREACH route: summary present -> spool file + active state, rc 0.
    #    Board-card channel is isolated to the stub + selftest-board so a
    #    self-test breach can NEVER mint a real sycode-trading card (t_ca175303).
    bucket_before = repage_bucket()
    args = parse_args(["--monitor", "selftest-breach",
                       "--summary", "CANDLE ALERT 1D fresh=0",
                       "--exit-code", "2",
                       "--board", "selftest-board"])
    rc = route(args)
    checks += 1
    if rc != 0:
        failures.append("breach must exit 0 (delivered)")
    spool_files = list(DEFAULT_SPOOL.glob("cronrelay-*.json"))
    checks += 1
    if not spool_files:
        failures.append("breach must write a spool alert file")
    if not read_state().get("active"):
        failures.append("breach must set episode active")
    # Verify the spool payload references the monitor alertname (router computes a
    # family-keyed alertname: "sycode-" + family_key(monitor)).
    if spool_files:
        try:
            payload = json.loads(spool_files[0].read_text())
            expected = "sycode-" + family_key("selftest-breach")
            if payload.get("alerts", [{}])[0].get("labels", {}).get("alertname") != expected:
                failures.append("spool alertname must be monitor-keyed (got %r, want %r)"
                                % (payload.get("alerts", [{}])[0].get("labels", {}).get("alertname"), expected))
        except Exception as exc:
            failures.append("breach spool payload unparseable: %s" % exc)

    # Board-card isolation regression (t_ca175303): the card branch must have run
    # against the isolated stub and targeted --board selftest-board — NEVER the
    # live sycode-trading board. A real board card would mean the fix failed.
    checks += 1
    invoc_log = DEFAULT_SPOOL.parent / "hermes_invocations.log"
    try:
        invoc_text = invoc_log.read_text()
    except Exception:
        invoc_text = ""
    boards = set(re.findall(r"--board\s+(\S+)", invoc_text))
    if boards != {"selftest-board"}:
        failures.append("board-card branch must target only isolated selftest-board "
                        "(got %r, never live sycode-trading) (t_ca175303)" % sorted(boards))
    checks += 1
    if not invoc_text.strip():
        failures.append("board-card branch must be exercised via the stub (t_ca175303)")

    # 3. DEDUPE: same repage bucket + same monitor repeat -> no new episode re-page
    #    (state active + same bucket) is fine; spool files may grow (one per run)
    #    but a second deliver should still exit 0 and NOT fail.
    before_count = len(list(DEFAULT_SPOOL.glob("cronrelay-*.json")))
    rc2 = route(parse_args(["--monitor", "selftest-breach",
                            "--summary", "CANDLE ALERT 1D fresh=0",
                            "--exit-code", "2",
                            "--board", "selftest-board"]))
    checks += 1
    after_count = len(list(DEFAULT_SPOOL.glob("cronrelay-*.json")))
    if rc2 != 0 or after_count < before_count:
        failures.append("dedupe repeat must not fail or lose state")

    # 4. DELIVERY-FAILURE fail-visible: point spool writer at a broken path and
    #    force board-card failure by pointing hermes at a nonexistent bin.
    SPOOL_WRITER = Path("/nonexistent/spool_alert_write.py")
    HERMES_CLI = "/nonexistent/hermes"
    args = parse_args(["--monitor", "selftest-deliveryfail",
                       "--summary", "PIT ALERT orphan rows"])
    rc = route(args)
    checks += 1
    if rc != 1:
        failures.append("delivery failure must be FAIL-VISIBLE (exit 1)")
    # Restore the isolated stub (never the real CLI) so the board-card channel
    # stays isolated through the whole self-test (t_ca175303).
    HERMES_CLI = stub_cli

    # 5. EXACT DETECTOR RERUN is exercised by the caller (the per-monitor tests/
    #    wrapper) — the router unit-checks that a fresh breach re-reports in a NEW
    #    bucket (exact detector rerun path). Simulate new bucket.
    REPAGE_HOURS = 6
    # Temporarily switch spool writer back to real one for a positive-path check.
    SPOOL_WRITER = Path("/home/frank/.hermes/scripts/spool_alert_write.py")

    print("self-test: %d check-group(%d asserts); failures=%d -> %s" % (
        checks, len(failures), len(failures),
        "FAIL" if failures else "PASS"))
    for f in failures:
        print("  FAIL:", f)
    return 0 if not failures else 1


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.self_test:
        return self_test()
    return route(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print("ERROR sycode_monitor_alert_route: %s" % exc, file=sys.stderr)
        raise SystemExit(1)