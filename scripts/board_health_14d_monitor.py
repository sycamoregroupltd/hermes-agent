#!/usr/bin/env python3
"""board_health_14d_monitor.py — daily post-WAL kanban board health monitor (t_c267360e).

Canonical source for the 14-day post-WAL corruption-free probe started when
t_41f5b873 switched all live boards to journal_mode=WAL + synchronous=FULL
(2026-08-28 16:44:15Z). Window ends 2026-09-11 16:44:15Z.

Per run:
  - For each live (non-archived) board with kanban.db: PRAGMA integrity_check
    (full), PRAGMA journal_mode, PRAGMA synchronous — all read-only.
  - Appends one JSON line to the run log (append-only evidence trail).
  - On any malformation: prints ESCALATE, writes an escalation marker file,
    and best-effort mints a high-priority jarvis-os kanban card (idempotent
    per board+date) assigned to jarvis-os-pm. Exits 3.
  - On/after the window end: writes a deterministic final report markdown
    summarizing every logged day (used by the one-shot completion cron).
  - Prints a one-line summary for cron delivery.

Usage: python3 board_health_14d_monitor.py [--boards b1,b2,...] [--dry-run]

Board enumeration default: scan <HERMES_HOME>/kanban/boards/*/kanban.db,
excluding names starting with '_' (matches engine list_boards(include_archived=False)).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

# Canonical paths — do NOT trust $HERMES_HOME here: in profile/cron contexts it
# points at the profile dir (e.g. ~/.hermes/profiles/fleet-analyst), but the
# kanban boards always live under the real hermes home.
HERMES_HOME = Path("/home/frank/.hermes")
BOARDS_DIR = HERMES_HOME / "kanban" / "boards"
LOG_DIR = HERMES_HOME / "var" / "log" / "board-health-14d"
HERMES_BIN = "/home/frank/.local/bin/hermes"
JARVIS_OS_BOARD = "jarvis-os"
ESCALATION_ASSIGNEE = "jarvis-os-pm"

WINDOW_START = _dt.datetime(2026, 8, 28, 16, 44, 15, tzinfo=_dt.timezone.utc)
WINDOW_END = _dt.datetime(2026, 9, 11, 16, 44, 15, tzinfo=_dt.timezone.utc)


def utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def live_boards() -> list[str]:
    boards = []
    if not BOARDS_DIR.is_dir():
        return boards
    for p in sorted(BOARDS_DIR.iterdir()):
        if p.name.startswith(("_", ".")):
            continue
        if (p / "kanban.db").exists():
            boards.append(p.name)
    return boards


def check_board(board: str) -> dict:
    db = BOARDS_DIR / board / "kanban.db"
    out = {"board": board, "integrity": "ok", "journal": None, "sync": None,
           "error": None, "detail": []}
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10.0)
        try:
            con.execute("PRAGMA busy_timeout=10000")
            rows = con.execute("PRAGMA integrity_check").fetchall()
            verdicts = [r[0] for r in rows]
            out["detail"] = verdicts[:5]
            if any((v or "").strip().lower() != "ok" for v in verdicts):
                out["integrity"] = "MALFORMED"
            jm = con.execute("PRAGMA journal_mode").fetchone()
            sy = con.execute("PRAGMA synchronous").fetchone()
            out["journal"] = jm[0] if jm else None
            out["sync"] = sy[0] if sy else None
            if out["journal"] != "wal":
                out["detail"].append(f"journal_mode={out['journal']} (expected wal)")
            if out["sync"] != 2:
                out["detail"].append(f"synchronous={out['sync']} (expected 2)")
        finally:
            con.close()
    except sqlite3.DatabaseError as exc:
        out["integrity"] = "MALFORMED"
        out["error"] = f"DatabaseError: {exc}"
    except Exception as exc:  # noqa: BLE001 - monitor must never crash
        out["integrity"] = "ERROR"
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def run_one(board: str, dry_run: bool) -> dict:
    return check_board(board)


def append_log(entry: dict) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_DIR / "monitor.jsonl", "a") as fh:
        fh.write(json.dumps(entry, sort_keys=True) + "\n")


def write_escalation_marker(board: str, entry: dict) -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    marker = LOG_DIR / f"ESCALATION-{board}-{entry['ts'][:10]}.json"
    marker.write_text(json.dumps(entry, sort_keys=True, indent=2))
    return marker


def mint_escalation_card(board: str, entry: dict, dry_run: bool) -> str | None:
    if dry_run:
        return "dry-run: skipped card mint"
    date_part = str(entry["ts"])[:10]
    key = f"t_c267360e-escalation-{board}-{date_part}"
    marker_name = f"ESCALATION-{board}-{date_part}.json"
    title = f"ESCALATION: board {board} malformed in post-WAL 14d monitor (t_c267360e)"
    body = (
        f"Post-WAL board-health monitor (t_c267360e) detected malformation on "
        f"{board} at {entry['ts']} UTC.\n\n"
        f"integrity={entry['integrity']} journal={entry['journal']} "
        f"sync={entry['sync']}\ndetail: {entry['detail']}\n\n"
        f"Full run log: {LOG_DIR / 'monitor.jsonl'}\n"
        f"Escalation marker: {LOG_DIR / marker_name}"
    )
    try:
        cp = subprocess.run(
            [HERMES_BIN, "kanban", "--board", JARVIS_OS_BOARD, "create", title,
             "--body", body, "--assignee", ESCALATION_ASSIGNEE,
             "--priority", "90", "--idempotency-key", key, "--json"],
            text=True, capture_output=True, timeout=30,
        )
        if cp.returncode != 0:
            return f"mint failed rc={cp.returncode}: {cp.stderr[:200]}"
        try:
            created = json.loads(cp.stdout)
            return f"minted {created.get('id') or created.get('task_id') or '?'}"
        except Exception:
            return f"minted (parse: {cp.stdout[:120]})"
    except Exception as exc:  # noqa: BLE001
        return f"mint error: {exc}"


def write_final_report(entries: list[dict]) -> Path:
    """Deterministic end-of-window report from the full log.

    Log entry shape: {"ts": ..., "window_day": ..., "boards_checked": N,
    "boards_ok": M, "results": [ {board, integrity, journal, sync, error,
    detail}, ... ]}. Flatten nested per-board results for the report.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    report = LOG_DIR / "FINAL-REPORT.md"
    days: dict[str, dict] = {}
    malformed = []
    for e in entries:
        d = str(e.get("ts", ""))[:10]
        day = days.setdefault(d, {"boards": 0, "ok": 0, "bad": []})
        results = e.get("results") or []
        if not results:
            # Old/incomplete shape: fall back to scalar fields.
            results = [{"board": e.get("board"), "integrity": e.get("integrity"),
                        "journal": e.get("journal"), "sync": e.get("sync"),
                        "error": e.get("error"), "detail": e.get("detail")}]
        for r in results:
            day["boards"] += 1
            if r.get("integrity") == "ok":
                day["ok"] += 1
            else:
                day["bad"].append(r)
                malformed.append({**r, "ts": e.get("ts")})
    lines = [
        "# 14-day post-WAL board health report (t_c267360e)",
        "",
        f"- Window: {WINDOW_START.isoformat()} .. {WINDOW_END.isoformat()} UTC",
        f"- Report generated: {utcnow().isoformat()} UTC",
        f"- Log: {LOG_DIR / 'monitor.jsonl'}",
        f"- Days with checks: {len(days)}",
        f"- Total board-checks: {sum(v['boards'] for v in days.values())}",
        f"- Malformed findings: {len(malformed)}",
        "",
        "## Per-day summary",
        "",
        "| date | boards | ok | malformed |",
        "|---|---|---|---|",
    ]
    for d in sorted(days):
        v = days[d]
        lines.append(f"| {d} | {v['boards']} | {v['ok']} | {len(v['bad'])} |")
    if malformed:
        lines += ["", "## Incidents", ""]
        for r in malformed:
            lines.append(f"- **{r.get('ts')}** {r.get('board')}: "
                         f"integrity={r.get('integrity')} journal={r.get('journal')} "
                         f"sync={r.get('sync')} detail={r.get('detail')} error={r.get('error')}")
    else:
        lines += ["", "## Incidents", "", "None — every board returned ok for the whole window."]
    lines.append("")
    report.write_text("\n".join(lines))
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--boards", help="comma-separated board override")
    ap.add_argument("--dry-run", action="store_true", help="no side effects")
    args = ap.parse_args()

    boards = [b.strip() for b in args.boards.split(",")] if args.boards else live_boards()
    now = utcnow()
    ts = now.isoformat()
    day = (now - WINDOW_START).days + 1
    window_done = now >= WINDOW_END

    results = [run_one(b, args.dry_run) for b in boards]
    ok = [r for r in results if r["integrity"] == "ok"]
    bad = [r for r in results if r["integrity"] != "ok"]

    entry = {
        "ts": ts,
        "window_day": day,
        "window_done": window_done,
        "boards_checked": len(results),
        "boards_ok": len(ok),
        "results": results,
    }
    append_log(entry)

    if bad:
        for r in bad:
            marker = write_escalation_marker(r["board"], entry)
            mint = mint_escalation_card(r["board"], entry, args.dry_run)
            print(f"ESCALATE: {r['board']} integrity={r['integrity']} "
                  f"journal={r.get('journal')} sync={r.get('sync')} "
                  f"detail={r.get('detail')} marker={marker.name} card={mint}")
        print(f"RESULT: {len(ok)}/{len(results)} boards ok — MALFORMATION PRESENT "
              f"(window day {day}, {ts})")
        return 3

    report_path = None
    if window_done:
        entries = []
        if (LOG_DIR / "monitor.jsonl").exists():
            with open(LOG_DIR / "monitor.jsonl") as fh:
                entries = [json.loads(ln) for ln in fh if ln.strip()]
        report_path = write_final_report(entries)
        print(f"FINAL_REPORT: {report_path}")

    print(f"RESULT: {len(ok)}/{len(results)} boards ok (window day {day}, {ts})")
    if report_path:
        print(f"FINAL_REPORT: {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
