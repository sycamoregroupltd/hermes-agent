#!/usr/bin/env python3
"""nonspawnable-fleet-alert-guard.py — fleet alert for tasks parked on
non-existent (non-spawnable) profiles — the silent ``skipped_nonspawnable`` class.

WHY THIS EXISTS (t_2b273303, child of incident 2026-07-24-ceo-jarvisos-absdeath)
  ``dispatch_once`` buckets ready tasks whose assignee is not a real Hermes
  profile into ``DispatchResult.skipped_nonspawnable`` and NEVER spawns them
  (kanban_db.py:8249 ``profile_exists`` guard — a hard, intentional skip).
  On multi-lane setups the assignee is a *terminal lane* (e.g. ``orion-cc``,
  or the ``fable``/``codex``/``grok`` Frank-activated seats) which is expected
  and not operator-actionable.

  BUT there is a THIRD, silent class: a task assigned to a non-existent
  profile that is NOT a recognized terminal lane (e.g. assignee='worker', 'a',
  'dev', 'pm', 'frank'). The dispatcher parks it forever (never dispatched,
  never double-executed — bounded) with NO alert and NO fleet telemetry. The
  deadpid guard only watches the 'pid N not alive' fingerprint, so this class
  is completely invisible. Left alone, these tasks sit at ``ready`` forever and
  look like a "stuck" backlog with no signal.

  This guard is a restart-free, read-only watchdog (mirroring
  deadpid-fleet-alert-guard.py) that surfaces that class as a single deduped
  fleet alert so the operator can either create the profile, reassign the
  task, or confirm the assignee is an intentional terminal lane.

WHAT IT ALERTS ON (the silent-park signature — mirrors dispatch's skip exactly)
  Any task on any board where:
    status = 'ready'                       # still in the dispatch pool
       AND claim_lock IS NULL              # not already pulled by a terminal
       AND assignee IS NOT NULL            # has an owner
       AND profile_exists(assignee) = False  # assignee is not a real profile dir
    AND NOT an excluded terminal lane      # see EXCLUDED_EXACT / EXCLUDED_PREFIX

  This is the exact predicate dispatch uses to append to skipped_nonspawnable
  (kanban_db.py:8136 ready_rows query + 8253 profile_exists guard). We do NOT
  recompute default_assignee logic — that only applies to *unassigned* tasks,
  and an unassigned task is a different (already-handled) class.

EXCLUSIONS (intentionally-parked terminal lanes — must NOT alert)
  Exact:  fable, codex, grok, operator  (Frank-activated terminal seats + operator lane)
  Prefix: orion-*                       (Claude Code / control-plane lanes)
  operator: Frank-operator lane — work only Frank (or a blessed operator seat) can do.
            Registered 2026-08-02 (jarvis-os/t_e08cdceb). Cards on this lane should carry
            block_kind=frank_gate + RESUME_GATE=frank-approval, never sit silently 'ready'.
  These are the recognized multi-lane setups the dispatcher intentionally
  never auto-spawns; an alert there would be pure noise.

DELIVERY: self-delivers via ``hermes send`` to #fleet-reports (verified path).
  The cron-layer ``deliver: discord`` path is fleet-wide broken, so this script
  does NOT rely on the scheduler to forward stdout — it sends directly, same as
  the sibling deadpid guard.

DEDUP: only re-alerts when the SET of offending task ids changes (added/removed)
  or every ALERT_MIN_INTERVAL_SECONDS. A stable parked set = one message, no spam.

DRY RUN: set NONSPAWNABLE_DRY_RUN=1 to print the candidate set + what would be
  alerted WITHOUT delivering or writing state. Used for the pre-ship verification.

OUTPUT: prints the alert to stdout too (harmless; cron deliver=local captures it).
  Silent when green.

This script NEVER mutates any board. Read-only by contract.
"""
from __future__ import annotations

import glob
import json
import os
import sqlite3
import subprocess
import sys
import time

KANBAN_HOME = os.environ.get("HERMES_KANBAN_HOME", "/home/frank/.hermes/kanban")
PROFILES_ROOT = os.environ.get("HERMES_PROFILES_ROOT", "/home/frank/.hermes/profiles")

# Recognizing terminal lanes the dispatcher intentionally never auto-spawns.
# Exact-match assignees (lowercased) excluded from the alert.
# Intentional non-spawnable lanes the dispatcher never auto-spawns:
#   fable, codex, grok  -> Frank-activated terminal seats (Claude Code Max / Codex / Grok)
#   operator            -> Frank-operator lane: work only Frank (or an operator seat he
#                          blesses) can do. Registered 2026-08-02 via jarvis-os/t_e08cdceb
#                          after 2 ready cards (t_bc6ba1c7, t_4776f5c9) sat on the
#                          undefined 'operator' assignee. Mirrors the fable/codex/grok
#                          precedent: a seat absent from profiles/ is treated as an
#                          intentional terminal lane ONLY when registered here.
EXCLUDED_EXACT = {"fable", "codex", "grok", "operator"}
# Assignee prefixes (lowercased) excluded from the alert.
EXCLUDED_PREFIX = ("orion-",)

# Re-alert floor so a stable parked set does not spam.
ALERT_MIN_INTERVAL_SECONDS = int(
    os.environ.get("NONSPAWNABLE_ALERT_MIN_INTERVAL", "3600")
)
# How often this guard runs; state file + delivery profile below.
STATE_PATH = os.environ.get(
    "NONSPAWNABLE_ALERT_STATE",
    "/home/frank/.hermes/cron/state/nonspawnable-fleet-alert-guard.seen",
)
HERMES_BIN = os.environ.get("HERMES_BIN", "/home/frank/.local/bin/hermes")
TARGET = os.environ.get("NONSPAWNABLE_FLEET_ALERT_TARGET", "discord:#fleet-reports")
HERMES_PROFILE = os.environ.get("NONSPAWNABLE_FLEET_ALERT_PROFILE", "jarvis")
DRY_RUN = os.environ.get("NONSPAWNABLE_DRY_RUN", "0") in ("1", "true", "True")


def _profile_exists(name: str) -> bool:
    """Mirror hermes_cli.profiles.profile_exists (kanban_db.py:8250 import)."""
    if not isinstance(name, str):
        name = str(name)
    s = name.strip()
    if not s:
        return False
    if s.casefold() == "default":
        return True
    return os.path.isdir(os.path.join(PROFILES_ROOT, s.lower()))


def _is_excluded(assignee: str) -> bool:
    al = (assignee or "").lower().strip()
    if al in EXCLUDED_EXACT:
        return True
    if any(al.startswith(p) for p in EXCLUDED_PREFIX):
        return True
    return False


def _board_db_paths() -> list[str]:
    out: list[str] = []
    for db in glob.glob(os.path.join(KANBAN_HOME, "boards", "*", "kanban.db")):
        out.append(db)
    return sorted(out)


def _scan() -> list[dict]:
    """Return ready, unclaimed, assigned-to-non-profile tasks (the parked class).

    Mirrors dispatch_once's skip predicate exactly: status='ready',
    claim_lock IS NULL, assignee present, profile does not exist. Excludes
    recognized terminal lanes.
    """
    hits: list[dict] = []
    for db in _board_db_paths():
        slug = os.path.basename(os.path.dirname(db))
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            cur = con.cursor()
            for r in cur.execute(
                "SELECT id, status, assignee, claim_lock, priority "
                "FROM tasks "
                "WHERE status = 'ready' "
                "  AND claim_lock IS NULL "
                "  AND assignee IS NOT NULL"
            ):
                assignee = r["assignee"]
                if _profile_exists(assignee):
                    continue  # real profile — dispatchable, not parked
                if _is_excluded(assignee):
                    # Recognized terminal lane — intentionally never spawned.
                    continue
                hits.append(
                    {
                        "board": slug,
                        "id": r["id"],
                        "assignee": assignee,
                        "priority": r["priority"],
                    }
                )
            con.close()
        except Exception as ex:  # pragma: no cover
            print(f"  ! {db}: {ex}", file=sys.stderr)
    return hits


def _load_seen() -> dict:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_seen(data: dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, STATE_PATH)


def _collect_alert_lines(hits: list[dict], by_board: dict, by_assignee: dict) -> list[str]:
    lines = [
        "NONSPAWNABLE FLEET ALERT (silent skipped_nonspawnable guard)",
        f"{len(hits)} ready task(s) parked on a NON-EXISTENT profile "
        f"(never dispatched, no telemetry until now)",
        "These are NOT terminal lanes (fable/codex/grok/orion-* are excluded).",
        "Root cause: dispatch_once profile_exists hard-skip (kanban_db.py:8249).",
        "Fix: create the profile, reassign the task, or confirm it is an "
        "intentional terminal lane and add it to EXCLUDED lists. See t_2b273303.",
        "---",
    ]
    for h in hits:
        lines.append(
            f"  [{h['board']}] {h['id']} assignee={h['assignee']} "
            f"priority={h['priority']}"
        )
    lines.append("---")
    lines.append("By board:    " + ", ".join(f"{b}={c}" for b, c in sorted(by_board.items())))
    lines.append("By assignee: " + ", ".join(f"{a}={c}" for a, c in sorted(by_assignee.items())))
    return lines


def _deliver(body: str) -> None:
    """Deliver the alert to #fleet-reports via `hermes send` (verified path).

    Failure to deliver is logged loudly to stderr but does NOT crash the
    watchdog (a delivery outage must not also suppress the local signal).
    """
    import shutil

    if not shutil.which(HERMES_BIN) and not os.path.exists(HERMES_BIN):
        print("nonspawnable-fleet-alert-guard: HERMES_BIN not found, cannot deliver", file=sys.stderr)
        return
    try:
        proc = subprocess.run(
            [HERMES_BIN, "-p", HERMES_PROFILE, "send", "-q", "-t",
             TARGET, "-s", "[nonspawnable-fleet-alert-guard]", body],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "")[:300]
            print(
                f"nonspawnable-fleet-alert-guard: hermes send FAILED rc={proc.returncode}: {detail}",
                file=sys.stderr,
            )
    except Exception as ex:  # pragma: no cover
        print(f"nonspawnable-fleet-alert-guard: hermes send exception: {ex}", file=sys.stderr)


def main() -> int:
    hits = _scan()
    if not hits:
        # Green: do not touch state so a future parked task always alerts.
        print("nonspawnable-fleet-alert-guard: green (0 parked-on-unknown-profile tasks)")
        return 0

    # Group for the human-readable body.
    by_board: dict[str, int] = {}
    by_assignee: dict[str, int] = {}
    for h in hits:
        by_board[h["board"]] = by_board.get(h["board"], 0) + 1
        a = h["assignee"]
        by_assignee[a] = by_assignee.get(a, 0) + 1

    current_ids = sorted(f"{h['board']}/{h['id']}" for h in hits)

    if DRY_RUN:
        body_lines = _collect_alert_lines(hits, by_board, by_assignee)
        print("[DRY_RUN] would deliver the following alert (no state written):")
        for line in body_lines:
            print(line)
        return 0

    seen = _load_seen()
    now = time.time()
    last_ids = seen.get("ids", [])
    last_alert_at = float(seen.get("last_alert_at", 0.0))

    # Re-alert only if the offending set changed OR the min interval elapsed.
    changed = set(current_ids) != set(last_ids)
    interval_ok = (now - last_alert_at) >= ALERT_MIN_INTERVAL_SECONDS
    if not (changed or interval_ok):
        # Still in the quiet window with a stable set — stay silent.
        _save_seen({"ids": current_ids, "last_alert_at": last_alert_at})
        return 0

    body_lines = _collect_alert_lines(hits, by_board, by_assignee)
    for line in body_lines:
        print(line)

    _save_seen({"ids": current_ids, "last_alert_at": now})

    # Self-deliver (cron-layer discord delivery is broken fleet-wide).
    _deliver("\n".join(body_lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
