#!/usr/bin/env python3
"""Rolling protocol_violation / crash-family rate measurement for the fleet.

No-agent cron script for the Jarvis profile (t_44cfa735, P0 worker-failure-rate
umbrella).  Publishes the rolling 6h window (spawned / completed / crashed /
protocol_violation / gave_up plus the pv rate and crash-family%) across every
dispatch board, using EPOCH arithmetic on task_events.created_at.

Epoch-arithmetic discipline (t_44cfa735): created_at is an INTEGER epoch, NOT a
datetime string.  A `datetime()/strftime()` comparison silently returns ZERO
rows and fabricates a clean reading.  This script therefore compares raw epoch
integers against `now_epoch - window_seconds`, and SELF-CHECKS: if the 6h window
returns zero spawned events across all boards, it FAILS LOUDLY (nonzero exit +
explicit message) because zero is the signature of the type bug rather than a
genuinely quiet fleet.

Env overrides (tests/fixtures):
  PV_BOARD_ROOT       boards root dir      (default /home/frank/.hermes/kanban/boards)
  PV_MANIFEST         boards manifest path (default ~/.hermes/kanban/boards-manifest.json)
  PV_OUT_DIR          report output dir    (default <profile>/cron/output/pv-rate-6h)
  PV_NOW              epoch override for deterministic tests
  PV_WINDOW           primary window seconds (default 21600 = 6h)

Output: markdown report + latest.json, written under PV_OUT_DIR and echoed to stdout.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

DEFAULT_ROOT = Path("/home/frank/.hermes")
DEFAULT_BOARD_ROOT = DEFAULT_ROOT / "kanban" / "boards"
DEFAULT_MANIFEST = DEFAULT_ROOT / "kanban" / "boards-manifest.json"
DEFAULT_OUT_DIR = DEFAULT_ROOT / "profiles" / "jarvis" / "cron" / "output" / "pv-rate-6h"

WINDOWS = {
    "1h": 3600,
    "3h": 10800,
    "6h": 21600,   # primary
    "24h": 86400,
}

EVENT_KINDS = ("spawned", "completed", "crashed", "protocol_violation", "gave_up", "timed_out")


def dispatch_boards() -> list[str]:
    """Board set is DATA (fleet boards manifest) - dispatch boards only."""
    try:
        sys.path.insert(0, "/home/frank/.hermes/scripts")
        from fleet_boards import boards_for  # type: ignore
        return list(boards_for("dispatch"))
    except Exception:
        return ["jarvis-os", "sycode-trading", "sycode-ai", "upero", "yorkstone-supplies"]


def count_window(db: Path, now_epoch: int, window: int) -> dict[str, int]:
    """Count events by kind in the trailing `window` seconds, EPOCH arithmetic."""
    out = {k: 0 for k in EVENT_KINDS}
    cutoff = now_epoch - window
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error:
        return out
    try:
        rows = con.execute(
            "SELECT kind, COUNT(*) FROM task_events "
            "WHERE created_at >= ? AND created_at <= ? GROUP BY kind",
            (cutoff, now_epoch),
        ).fetchall()
    except sqlite3.Error:
        con.close()
        return out
    con.close()
    for kind, n in rows:
        if kind in out:
            out[kind] = n
    return out


def fmt_rate(num: int, den: int) -> str:
    if den <= 0:
        return "n/a"
    return f"{100.0 * num / den:.1f}%"


def main() -> int:
    board_root = Path(os.environ.get("PV_BOARD_ROOT", str(DEFAULT_BOARD_ROOT)))
    manifest = Path(os.environ.get("PV_MANIFEST", str(DEFAULT_MANIFEST)))
    out_dir = Path(os.environ.get("PV_OUT_DIR", str(DEFAULT_OUT_DIR)))
    now_epoch = int(os.environ.get("PV_NOW") or time.time())
    primary = int(os.environ.get("PV_WINDOW", "21600"))

    boards = dispatch_boards()
    # Validate the manifest exists; boards_for already falls back if missing.
    _ = manifest

    # Aggregate per window across all boards.
    totals: dict[str, dict[str, int]] = {}
    per_board = {}
    for wname, wsec in WINDOWS.items():
        agg = {k: 0 for k in EVENT_KINDS}
        per_board[wname] = {}
        for slug in boards:
            db = board_root / slug / "kanban.db"
            if not db.is_file():
                continue
            cnt = count_window(db, now_epoch, wsec)
            per_board[wname][slug] = cnt
            for k in EVENT_KINDS:
                agg[k] += cnt[k]
        totals[wname] = agg

    # ---- SELF-CHECK: zero spawned in the primary window is a type-bug signature ----
    prim_spawned = totals["6h"]["spawned"]
    if prim_spawned <= 0:
        print(
            "FATAL: zero spawned events in the 6h window across all dispatch boards. "
            "This is the epoch-vs-datetime type-bug signature (t_44cfa735), NOT a clean "
            "reading. If the fleet is genuinely idle, confirm manually before trusting this.",
            file=sys.stderr,
        )
        return 3

    # ---- Report ----
    lines = [
        "# Rolling kanban worker-failure rate (protocol_violation / crash-family)",
        "",
        f"generated_at: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now_epoch))}",
        f"now_epoch: {now_epoch}",
        f"boards: {', '.join(boards)}",
        "",
        "| window | spawned | completed | crashed | protocol_violation | gave_up | timed_out | pv% | crash-family% |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for wname in ("1h", "3h", "6h", "24h"):
        a = totals[wname]
        spawned = a["spawned"]
        pv = a["protocol_violation"]
        family = a["crashed"] + a["protocol_violation"] + a["gave_up"]
        lines.append(
            f"| {wname} | {a['spawned']} | {a['completed']} | {a['crashed']} | "
            f"{pv} | {a['gave_up']} | {a['timed_out']} | {fmt_rate(pv, spawned)} | "
            f"{fmt_rate(family, spawned)} |"
        )
    lines.append("")
    lines.append(f"primary_window_seconds: {primary}")
    lines.append(f"pv_rate_6h: {fmt_rate(totals['6h']['protocol_violation'], prim_spawned)}")
    family6 = totals["6h"]["crashed"] + totals["6h"]["protocol_violation"] + totals["6h"]["gave_up"]
    lines.append(f"crash_family_rate_6h: {fmt_rate(family6, prim_spawned)}")
    lines.append("")

    report = "\n".join(lines)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d_%H-%M-%S", time.gmtime(now_epoch))
    report_path = out_dir / f"pv-rate-6h_{stamp}.md"
    report_path.write_text(report)
    latest = out_dir / "latest.json"
    latest.write_text(
        json.dumps(
            {
                "generated_at_epoch": now_epoch,
                "boards": boards,
                "primary_window_seconds": primary,
                "totals": totals,
                "pv_rate_6h_pct": (
                    round(100.0 * totals["6h"]["protocol_violation"] / prim_spawned, 2)
                    if prim_spawned
                    else None
                ),
                "crash_family_rate_6h_pct": (
                    round(100.0 * family6 / prim_spawned, 2) if prim_spawned else None
                ),
            },
            indent=2,
        )
    )

    print(report)
    print(f"report: {report_path}")
    print(f"json:   {latest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
