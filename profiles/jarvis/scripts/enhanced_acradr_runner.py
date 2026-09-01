#!/usr/bin/env python3
# CANONICAL SOURCE — Enhanced ACRADR Phase 3 runner (t_00a856e5).
#
# Orchestrates the Phase 1 detector (dgx_report_anomaly_detector.py) and the
# Phase 2 ledger (anomaly_ledger.py) into a single zero-token, no_agent cron
# job that:
#   1. Runs the deterministic detector over the jarvis cron report tree.
#   2. Routes each anomaly to its DYNAMIC Discord channel (multi-channel):
#        - critical (gateway down / fusion db error / write fail / fill<85%)
#            -> discord:#critical-alerts
#        - fusion_calibration (win-rate / MCE / parsing)  -> discord:#quant-reports
#        - freshness / news_catalyst (routine)            -> discord:#fleet-reports
#   3. Drives the Anomaly State Ledger for dedupe + self-healing:
#        - first detection  -> create kanban ticket (rich git + system metadata)
#        - persists         -> update the ledger count/last-seen only (no new
#                              comment or card for a repeat)
#        - green again      -> comment RESOLVED + safely auto-complete ready,
#                              todo, or blocked cards + recovery ping; retain
#                              in-progress pointers for a later retry
#   4. Writes a liveness heartbeat file consumed by enhanced_acradr_watchdog.py
#      (which alerts discord:#jarvis-os-governance if the scanner stops ticking).
#
# Designed to run as `no_agent: true` so it never spends tokens and never hangs.
# Discord delivery is performed INTERNALLY via `hermes -p jarvis send` so a
# single cron job can fan out to 3+ distinct channels (a single cron `deliver`
# target cannot). Therefore the registering cron uses `deliver: local` — the
# runner owns all Discord traffic.
#
# Imports the detector + ledger from the canonical root scripts dir
# (/home/frank/.hermes/scripts) so there is a single source of truth. The
# profile-local file is the exact cron-executed copy; ACRADR_ROOT_SCRIPTS is an
# explicit, testable override for isolated checkouts.

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

# ── Locate the canonical root scripts dir (contains detector + ledger) ──────
# This file lives at <hermes_home>/scripts/enhanced_acradr_runner.py where
# hermes_home == /home/frank/.hermes/profiles/jarvis. The canonical sources
# live three levels up, under /home/frank/.hermes/scripts.
_THIS = Path(__file__).resolve()
_ROOT_SCRIPTS_CANDIDATES = [
    os.environ.get("ACRADR_ROOT_SCRIPTS"),
    str(_THIS.parents[3] / "scripts"),          # profiles/jarvis/scripts -> .hermes/scripts
    "/home/frank/.hermes/scripts",
]
for _cand in _ROOT_SCRIPTS_CANDIDATES:
    if _cand and Path(_cand).is_dir():
        if str(_cand) not in sys.path:
            sys.path.insert(0, str(_cand))
        break

import dgx_report_anomaly_detector as det  # noqa: E402
import anomaly_ledger as ledger            # noqa: E402

# ── Config ──────────────────────────────────────────────────────────────────
JARVIS_HOME = os.environ.get("HERMES_HOME", "/home/frank/.hermes/profiles/jarvis")
JARVIS_PROFILE = "jarvis"
HERMES_BIN = os.environ.get("HERMES_BIN", "/home/frank/.local/bin/hermes")

DEFAULT_SCAN_ROOT = det.DEFAULT_SCAN_ROOT
LEDGER_PATH = ledger.DEFAULT_LEDGER
HEARTBEAT_FILE = Path(os.environ.get(
    "ACRADR_HEARTBEAT_FILE",
    "/home/frank/.hermes/profiles/jarvis/cron/state/acradr_heartbeat.txt",
))
STATE_DIR = HEARTBEAT_FILE.parent

BOARD = "jarvis-os"
SOURCE_MAX_AGE_HOURS = ledger._source_max_age_hours()

# Discord channel names (resolved by `hermes -p jarvis send` under jarvis config)
CH_CRITICAL = "discord:critical-alerts"
CH_QUANT = "discord:quant-reports"
CH_FLEET = "discord:fleet-reports"
CH_GOV = "discord:jarvis-os-governance"

# ── Dynamic multi-channel routing ───────────────────────────────────────────
def channel_for(anomaly: "det.Anomaly") -> str:
    """Route an anomaly to its Discord channel.

    Critical severity (gateway-down, fusion db/write/fill failures) always go to
    #critical-alerts. Warnings route by report class: calibration regressions to
    #quant-reports, everything routine (freshness, news) to #fleet-reports.
    """
    if anomaly.severity == "critical":
        return CH_CRITICAL
    return {
        "fusion_calibration": CH_QUANT,
        "health_canary": CH_FLEET,      # freshness warnings
        "data_freshness": CH_FLEET,
        "news_catalyst": CH_FLEET,
    }.get(anomaly.report_class, CH_FLEET)


# Kanban assignee per the Enhanced ACRADR spec (t_03e2fea5 §5).
ASSIGNEE_BY_CLASS = {
    "health_canary": "trading-devops",
    "fusion_engine": "os-architect",          # R3 gate blocker card
    "fusion_calibration": "os-reviewer",
    "news_catalyst": "integration-builder",
    "data_freshness": "trading-devops",
}


# ── Harness: real kanban + Discord, scoped to the jarvis profile ────────────
class JarvisACRADRHarness(ledger.KanbanHarness):
    """KanbanHarness variant that always targets the jarvis profile for both
    kanban and Discord delivery (so the right config / channel map is used)."""

    def _kanban(self, *args: str):
        env = os.environ.copy()
        env["HERMES_HOME"] = JARVIS_HOME
        cmd = [HERMES_BIN, "-p", JARVIS_PROFILE, "kanban"]
        if self.board:
            cmd += ["--board", self.board]
        cmd += list(args)
        return subprocess.run(cmd, capture_output=True, text=True, timeout=180, env=env)

    def create_ticket(self, title, body, assignee, priority=5, board=None):
        board = board or self.board
        env = os.environ.copy()
        env["HERMES_HOME"] = JARVIS_HOME
        cmd = [HERMES_BIN, "-p", JARVIS_PROFILE, "kanban", "--board", board,
               "create", "--assignee", assignee, "--priority", str(priority),
               "--json", "--body", body, title]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=180, env=env)
        if res.returncode != 0:
            raise RuntimeError(
                f"kanban create failed (rc={res.returncode}): "
                f"{(res.stdout + res.stderr).strip()[:400]}")
        tid = ledger._extract_task_id(res.stdout)
        if not tid:
            raise RuntimeError(
                f"kanban create returned no id: {(res.stdout + res.stderr).strip()[:400]}")
        return tid

    def send_alert(self, target: str, message: str) -> bool:
        env = os.environ.copy()
        env["HERMES_HOME"] = JARVIS_HOME
        try:
            res = subprocess.run(
                [HERMES_BIN, "-p", JARVIS_PROFILE, "send", "-q", "-t", target,
                 "-s", "ACRADR", message],
                capture_output=True, text=True, timeout=120, env=env,
            )
            return res.returncode == 0
        except Exception as e:
            sys.stderr.write(f"ACRADR alert send failed to {target}: {e}\n")
            return False


# ── Message formatting ───────────────────────────────────────────────────────
def _fmt_metadata(anomaly: "det.Anomaly") -> str:
    bits = []
    if anomaly.git_context:
        bits.append("recent commits:")
        for c in anomaly.git_context[:5]:
            bits.append(f"  • {c}")
    sm = anomaly.system_metrics or {}
    sysbits = []
    if "cpu_percent" in sm:
        sysbits.append(f"cpu={sm['cpu_percent']}%")
    if "mem_percent" in sm:
        sysbits.append(f"mem={sm['mem_percent']}% ({sm['mem_used_gb']}/{sm['mem_total_gb']}GB)")
    if "gpus" in sm:
        for g in sm["gpus"]:
            sysbits.append(f"gpu{g['gpu']}={g['util_percent']}% vram={g['vram_used_mb']}/{g['vram_total_mb']}MB")
    if sysbits:
        bits.append("system: " + ", ".join(sysbits))
    return "\n".join(bits)


def _discord_msg(anomaly: "det.Anomaly", occurrence: int) -> str:
    sev = "🚨 CRITICAL" if anomaly.severity == "critical" else "⚠️ WARNING"
    occ = f" (occurrence #{occurrence})" if occurrence and occurrence > 1 else ""
    body = (
        f"{sev} — ACRADR anomaly{occ}\n"
        f"[{anomaly.report_class}] {anomaly.rule_id}\n"
        f"file: {anomaly.source_file}"
        + (f":{anomaly.source_line}" if anomaly.source_line else "")
        + f"\nmatched: `{anomaly.snippet[:180]}`"
    )
    meta = _fmt_metadata(anomaly)
    if meta:
        body += f"\n\n{meta}"
    if anomaly.fallback_used:
        body += "\n\n⚠ zero-token fallback was active (provider enrichment disabled)."
    return body


def _ticket_body(anomaly: "det.Anomaly") -> str:
    lines = [
        f"**ACRADR Anomaly — [{anomaly.report_class}] {anomaly.rule_id}**",
        f"Severity: {anomaly.severity}",
        f"Source: `{anomaly.source_file}`"
        + (f":{anomaly.source_line}" if anomaly.source_line else ""),
        f"Match: `{anomaly.snippet[:300]}`",
        "",
        "## Diagnostic context",
    ]
    if anomaly.git_context:
        lines.append("Recent commits (last 24h):")
        for c in anomaly.git_context[:8]:
            lines.append(f"- {c}")
    meta = _fmt_metadata(anomaly)
    if meta:
        lines.append("")
        lines.append(meta)
    if anomaly.fallback_used:
        lines.append("\n> Zero-token fallback active: provider/LLM enrichment disabled; "
                     "deterministic regex parsing used for 100% coverage.")
    return "\n".join(lines)


# ── Core orchestration (injectable harness for tests) ────────────────────────
def core_run(
    scan_root: Path = DEFAULT_SCAN_ROOT,
    ledger_path: Path = LEDGER_PATH,
    harness: "ledger.KanbanHarness" = None,  # type: ignore[assignment]
    heartbeat_file: Path = HEARTBEAT_FILE,
    simulate_outage: bool = False,
    quiet: bool = False,
    source_max_age_hours: Optional[float] = SOURCE_MAX_AGE_HOURS,
) -> dict:
    """Run a full ACRADR cycle: detect -> route -> ledger -> self-heal -> heartbeat.

    Returns a summary dict. Side effects (kanban + Discord) go through `harness`,
    so callers may inject a mock harness for tests.
    """
    if harness is None:
        harness = JarvisACRADRHarness(board=BOARD)

    anomalies = det.run_detection(Path(scan_root), provider_outage=simulate_outage)
    book = ledger.load_ledger(ledger_path)

    created, deduped, suppressed = [], [], []
    current_keys = set()
    # A suppressed source is not evidence that its prior finding resolved. Keep
    # those keys separate so self-heal and orphan reconciliation fail closed.
    suppressed_keys = set()
    admitted_this_run = {}
    for a in anomalies:
        key = ledger._entry_key(a.report_class, a.source_file, a.rule_id)
        legacy_key = ledger._legacy_entry_key(a.report_class, a.source_file)
        guard = ledger.admit_source(
            book, a.source_file, max_age_hours=source_max_age_hours,
            admitted_this_run=admitted_this_run,
        )
        if not guard["accepted"]:
            suppressed.append(guard)
            suppressed_keys.add(key)
            # Protect a pre-rule ledger entry and orphan card too when this
            # source is suppressed before the rule-aware key can be admitted.
            suppressed_keys.add(legacy_key)
            continue
        current_keys.add(key)
        if legacy_key in book.get("entries", {}):
            current_keys.add(legacy_key)
        channel = channel_for(a)
        assignee = ASSIGNEE_BY_CLASS.get(a.report_class, "trading-devops")
        title = f"[ACRADR] {a.severity.upper()} {a.report_class}: {a.rule_id}"
        body = _ticket_body(a)
        res = ledger.record_anomaly(
            book,
            report_class=a.report_class,
            source=a.source_file,
            rule_id=a.rule_id,
            fingerprint=f"{a.rule_id}:{a.snippet[:80]}",
            title=title,
            body=body,
            assignee=assignee,
            channel=channel,
            harness=harness,
            _create_fn=harness.create_ticket,
            _comment_fn=harness.comment,
        )
        if res["action"] == "created":
            harness.send_alert(channel, _discord_msg(a, 1))
            created.append(res["task_id"])
        elif res["action"] == "deduped":
            deduped.append(res["task_id"])

    # Suppressed keys are explicitly protected: stale/unreadable/high-water
    # evidence must never prove an active finding green.
    resolved = []
    for entry in ledger.list_active(book):
        key = ledger._entry_key(entry["report_class"], entry["source"], entry.get("rule_id"))
        if key in current_keys or key in suppressed_keys:
            continue
        r = ledger.resolve_anomaly(
            book,
            report_class=entry["report_class"],
            source=entry["source"],
            rule_id=entry.get("rule_id"),
            channel=entry.get("channel"),
            harness=harness,
            _comment_fn=harness.comment,
            _alert_fn=harness.send_alert,
            _status_fn=harness.status,
        )
        if r["action"] == "resolved":
            resolved.append(r["task_id"])

    # Reconcile orphaned ACRADR cards (defect-c t_36d0acad): the ledger may have
    # lost a card (resolve used to drop the pointer on a blocked/unclosable card),
    # leaving a green condition's card open for days. Re-locate open [ACRADR]
    # cards and close any whose key is no longer anomalous this run.
    # A suppressed source is not green evidence for orphan reconciliation
    # either: preserve any matching open card until a fresh revision is seen.
    orphan_closed = ledger.reconcile_orphan_acradr(
        current_keys | suppressed_keys,
        board=BOARD,
        harness=harness,
        _list_open_fn=getattr(harness, "list_open_acradr", None),
    )

    ledger.save_ledger(book, ledger_path)

    # Liveness heartbeat
    try:
        heartbeat_file = Path(heartbeat_file)
        heartbeat_file.parent.mkdir(parents=True, exist_ok=True)
        heartbeat_file.write_text(
            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") + "\n")
    except Exception as e:
        sys.stderr.write(f"ACRADR heartbeat write failed: {e}\n")

    summary = {
        "scanned_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "anomaly_count": len(anomalies),
        "critical_count": sum(1 for a in anomalies if a.severity == "critical"),
        "created_tickets": created,
        "deduped_tickets": deduped,
        "suppressed_sources": suppressed,
        "resolved_tickets": resolved,
        "orphan_closed": orphan_closed,
        "active_tickets": ledger.count_active(book),
    }
    if not quiet:
        print(det.render_report(anomalies, simulate_outage))
        print("\n--- ACRADR routing summary ---")
        print(f"created={len(created)} deduped={len(deduped)} "
              f"suppressed={len(suppressed)} resolved={len(resolved)} "
              f"orphan_closed={len(orphan_closed)} active={summary['active_tickets']}")
        for item in suppressed:
            print(f"suppressed source={item.get('identity')} reason={item.get('reason')}")
    return summary


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Enhanced ACRADR Phase 3 runner")
    p.add_argument("--scan-root", default=str(DEFAULT_SCAN_ROOT))
    p.add_argument("--ledger", default=str(LEDGER_PATH))
    p.add_argument("--heartbeat-file", default=str(HEARTBEAT_FILE))
    p.add_argument("--simulate-provider-outage", action="store_true")
    p.add_argument("--dry-run", action="store_true",
                   help="Use an in-memory mock harness (no real kanban/Discord).")
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args(argv)

    harness = None
    if args.dry_run:
        harness = _MockHarness()

    # Imported lazily so --dry-run works even without kanban side effects.
    from types import SimpleNamespace  # noqa

    summary = core_run(
        scan_root=Path(args.scan_root),
        ledger_path=Path(args.ledger),
        harness=harness,
        heartbeat_file=Path(args.heartbeat_file),
        simulate_outage=args.simulate_provider_outage,
        quiet=args.quiet,
    )
    return 2 if summary["critical_count"] > 0 else 0


class _MockHarness:
    """In-memory harness for --dry-run: records calls, performs no I/O."""

    def __init__(self):
        self.created = []
        self.comments = []
        self.completed = []
        self.alerts = []

    def create_ticket(self, title, body, assignee, priority=5, board=None):
        tid = f"t_mock{len(self.created)}"
        self.created.append({"id": tid, "title": title, "assignee": assignee,
                             "body": body})
        return tid

    def comment(self, task_id, body, board=None):
        self.comments.append({"id": task_id, "body": body})

    def complete(self, task_id, summary, board=None):
        self.completed.append({"id": task_id, "summary": summary})

    def send_alert(self, target, message):
        self.alerts.append({"target": target, "message": message})
        return True

    def status(self, task_id, board=None):
        return "ready"

    def list_open_acradr(self, board=None):
        # Dry-run reconcile no-op: no orphaned cards by default. Tests inject
        # a configured list when exercising defect-c reconciliation.
        return getattr(self, "open_acradr", [])


if __name__ == "__main__":
    sys.exit(main())
