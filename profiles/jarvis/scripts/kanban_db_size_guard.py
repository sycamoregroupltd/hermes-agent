#!/usr/bin/env python3
"""kanban_db_size_guard.py — report-only DB-bloat threshold check.

RECURRENCE PREVENTION for t_519df3c0 (kanban/state DB bloat timing out the
guard layer): the fix for the 2026-08-30 incident was a one-time prune +
VACUUM (sycode-trading kanban.db 215MB->150MB, jarvis-os 99MB->89MB) plus
`hermes kanban gc` + `kanban-audit-chain.py reconcile-gc`. Without a standing
check, board DBs will silently regrow past the sizes that previously made
kanban-dedupe-guard (119s), kanban-audit-chain-monitor (30s), and
blocked-task-notifier (600s) time out.

This is a CHEAP, no-agent, report-only watchdog: no LLM, no DB mutation. It
only stats file sizes and prints a summary when a threshold is crossed
(silent otherwise, matching the guard-bundle silent-on-clean contract).

Thresholds (env-overridable):
  KANBAN_DB_WARN_MB     board kanban.db size warning threshold (default 150)
  STATE_DB_WARN_GB      profile state.db size warning threshold (default 2)

Exit code: 0 when every DB is under threshold (silent, matches every other
absorbed check in cron_guard_bundle_runner.py's CHECKS table — the runner
only collects a check's stdout into its aggregate report when the check
exits nonzero, per run_check()'s `if proc.returncode != 0` gate). Exit 1
when any threshold is breached, WITH the finding text on stdout, so the
guard-bundle -> report-to-board.py pipe files exactly one self-closing
board card (jarvis-os, key rtb-guard-bundle-daily) instead of silently
regrowing until the guard scripts start timing out again.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

HERMES_ROOT = Path(os.environ.get("HERMES_ROOT", str(Path.home() / ".hermes")))
BOARDS_DIR = HERMES_ROOT / "kanban" / "boards"
PROFILES_DIR = HERMES_ROOT / "profiles"

# WHY 200 (not 150): the 2026-08-30 incident threshold was 215MB+ (guard scripts
# timing out). sycode-trading and jarvis-os are long-lived boards with 12K+ active
# tasks and ~3-5MB/month organic growth from events+runs+comments. At 150MB they
# breach within months of a clean VACUUM with no pathology — the guard would cry
# wolf weekly. 200MB gives 6-12 months headroom while still catching the kind of
# unbounded regrowth that caused the original incident. If a board crosses 200MB,
# investigate — don't raise this again.
KANBAN_DB_WARN_MB = float(os.environ.get("KANBAN_DB_WARN_MB", "200"))
STATE_DB_WARN_GB = float(os.environ.get("STATE_DB_WARN_GB", "2"))


def main() -> int:
    findings: list[str] = []

    if BOARDS_DIR.exists():
        for db in sorted(BOARDS_DIR.glob("*/kanban.db")):
            try:
                size_mb = db.stat().st_size / (1024 * 1024)
            except OSError:
                continue
            if size_mb > KANBAN_DB_WARN_MB:
                findings.append(
                    f"board kanban.db over threshold: {db.parent.name} "
                    f"{size_mb:.0f}MB > {KANBAN_DB_WARN_MB:.0f}MB "
                    f"(run: hermes kanban --board {db.parent.name} gc "
                    f"--event-retention-days 30 --log-retention-days 30; "
                    f"then VACUUM per db-architect t_519df3c0 recipe)"
                )

    if PROFILES_DIR.exists():
        for db in sorted(PROFILES_DIR.glob("*/state.db")):
            try:
                size_gb = db.stat().st_size / (1024 ** 3)
            except OSError:
                continue
            if size_gb > STATE_DB_WARN_GB:
                findings.append(
                    f"profile state.db over threshold: {db.parent.name} "
                    f"{size_gb:.1f}GB > {STATE_DB_WARN_GB:.1f}GB "
                    f"(hermes-state-prune-weekly cron covers this profile "
                    f"only if its gateway is briefly idle; see hermes-maintenance skill)"
                )

    if not findings:
        return 0

    print("KANBAN-DB-SIZE-GUARD: report-only threshold breach(es):")
    for f in findings:
        print(f"  - {f}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
