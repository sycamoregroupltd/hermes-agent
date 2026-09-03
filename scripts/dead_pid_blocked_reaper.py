#!/usr/bin/env python3
"""Standing dead-PID blocked-card reaper + needs_input digest consumer.

Structural fix for kanban t_51d5a38b. Runs inside the existing jarvis hygiene
cron (kanban-classify-failure-cron fe49f09f4e53) every tick and does two things:

1. DEAD-PID REAPER
   Find status='blocked' tasks whose last_failure_error matches 'pid N not
   alive' AND block_kind is NULL or 'transient'. Verify the PID is actually
   gone (os.kill(pid,0) -> ProcessLookupError). Requeue via the native
   `hermes kanban unblock --reason 'dead-pid reaper'` CLI. Capped at
   --cap (default 10) requeues per tick. Idempotent: only touches rows that are
   still status='blocked' with the dead-PID marker at the moment of the write.

2. NEEDS_INPUT DIGEST
   Find needs_input blocked cards older than --needs-input-days (default 14).
   Emit/refresh ONE digest note in the fleet vault Orchestration/ dir — never
   per-card pings. Consumer = Jarvis voice line + Frank (the digest reader).

SAFETY (A3 / review gates):
  - Skip any card whose title OR any comment contains 'FRANK', 'A3', 'review',
    'REVIEW', 'approval', 'approve', 'gate', 'escalat', 'credential', 'secret',
    'deploy', 'live', 'spend', 'money', 'trading' markers. Reaping only applies
    to genuine process-death blocks, never to cards that carry a real gate.
  - Dry-run by default. Pass --apply to mutate (requeue / write digest).

DRAIN-GATE: when --drain-gate is set (default via KANBAN_DEAD_PID_DRAIN_GATE=1),
the reaper skips requeuing while an open card titled 'DRAIN: jarvis-os ...' still
exists on the target board, so backlog draining (t_573abdb9) runs first and the
weekly count baseline starts clean. The digest note is still emitted.

Output: empty on clean no-op; otherwise a compact machine-parseable report.
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

BOARD_ROOT = Path("/home/frank/.hermes/kanban/boards")
HERMES = os.environ.get("HERMES_BIN", "/home/frank/.local/bin/hermes")
DIGEST_PATH = Path(
    os.environ.get(
        "DEAD_PID_DIGEST_PATH",
        "/home/frank/obsidian-fleet-vault/Orchestration/dead-pid-needs-input-digest.md",
    )
)
DRAIN_TITLE_PREFIX = "DRAIN: jarvis-os"
REAPER_REASON = "dead-pid reaper"
PID_RE = re.compile(r"pid\s+(\d+)\s+not alive")
GATE_MARKERS = re.compile(
    r"FRANK|A3|\breview\b|REVIEW|approval|approve|\bgate\b|escalat|credential|secret|"
    r"\bdeploy\b|\blive\b|\bspend\b|\bmoney\b|\btrading\b",
    re.I,
)

sys.path.insert(0, "/home/frank/.hermes/scripts")


def triage_boards() -> list[str]:
    """Board slugs flagged 'triage' in the fleet boards manifest (data, t_911a916c)."""
    try:
        from fleet_boards import boards_for

        return list(boards_for("triage"))
    except Exception:
        return ["jarvis-os", "sycode-trading", "sycode-ai", "upero", "yorkstone-supplies"]


def connect(db: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def pid_is_gone(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False  # exists (owned by another user)
    except OSError:
        return True
    return False


def card_has_gate(db: Path, task_id: str, title: str) -> bool:
    """Skip cards whose title or any comment carries a real A3/review marker."""
    if GATE_MARKERS.search(title or ""):
        return True
    try:
        con = connect(db)
        try:
            rows = con.execute(
                "SELECT body FROM task_comments WHERE task_id = ?", (task_id,)
            ).fetchall()
        finally:
            con.close()
        return any(GATE_MARKERS.search(str(r["body"] or "")) for r in rows)
    except sqlite3.Error:
        return True  # fail closed on read error


def open_drain_card(db: Path) -> bool:
    """True while an open 'DRAIN: jarvis-os ...' card still exists on the board."""
    try:
        con = connect(db)
        try:
            row = con.execute(
                "SELECT 1 FROM tasks WHERE title LIKE ? AND status NOT IN ('done','archived') LIMIT 1",
                (DRAIN_TITLE_PREFIX + "%",),
            ).fetchone()
        finally:
            con.close()
        return row is not None
    except (OSError, sqlite3.Error):
        # Uncertainty must keep the drain gate closed: never resume reaping
        # when the board cannot be read or its SQLite database is unavailable.
        return True


def any_open_drain_card(boards: list[str]) -> bool:
    """Fleet-wide drain gate: if ANY target board still has an open 'DRAIN:
    jarvis-os ...' card (t_573abdb9 backlog drain), defer reaping on ALL boards
    so the weekly count baseline starts clean. The digest note is still emitted.
    """
    for board in boards:
        db = BOARD_ROOT / board / "kanban.db"
        # A missing requested board is indistinguishable from an unreadable
        # board at this gate: keep reaping closed until a later tick can read
        # every requested board successfully.
        if not db.exists():
            return True
        if open_drain_card(db):
            return True
    return False


def reap_board(db: Path, board: str, cap: int, apply: bool, drain_open: bool) -> list[str]:
    """Requeue dead-PID null/transient blocked cards on one board. Returns log lines."""
    lines: list[str] = []
    if drain_open:
        return ["REAP_SKIPPED drain-gate: open 'DRAIN: jarvis-os' card present (waiting for t_573abdb9)"]
    con = connect(db)
    try:
        rows = con.execute(
            """
            SELECT id, title, last_failure_error
            FROM tasks
            WHERE status = 'blocked'
              AND last_failure_error LIKE '%pid % not alive%'
              AND (block_kind IS NULL OR block_kind = 'transient')
            ORDER BY created_at ASC
            """,
        ).fetchall()
    finally:
        con.close()

    requeued = 0
    for row in rows:
        if requeued >= cap:
            lines.append(f"CAP hit at {cap}/tick on {board}")
            break
        tid = str(row["id"])
        title = str(row["title"] or "")
        err = str(row["last_failure_error"] or "")
        m = PID_RE.search(err)
        pid = int(m.group(1)) if m else None
        if pid is None or not pid_is_gone(pid):
            continue
        if card_has_gate(db, tid, title):
            lines.append(f"SKIP_GATE {board} {tid} (marker in title/comments)")
            continue
        if not apply:
            lines.append(f"DRY_UNBLOCK {board} {tid} pid={pid} :: {title[:70]}")
            requeued += 1
            continue
        cp = subprocess.run(
            [
                HERMES, "kanban", "--board", board, "unblock", "--reason",
                REAPER_REASON, tid,
            ],
            capture_output=True, text=True, timeout=60, check=False,
        )
        if cp.returncode == 0:
            lines.append(f"UNBLOCKED {board} {tid} pid={pid} :: {title[:70]}")
            requeued += 1
        else:
            lines.append(f"UNBLOCK_FAIL {board} {tid} rc={cp.returncode}: {(cp.stdout + cp.stderr).strip()[:160]}")
    return lines


def needs_input_cards(db: Path, older_than_days: int) -> list[dict]:
    """needs_input blocked cards older than N days. Returns list of rows."""
    cutoff = int(time.time()) - older_than_days * 86400
    con = connect(db)
    try:
        rows = con.execute(
            """
            SELECT id, title, created_at
            FROM tasks
            WHERE status = 'blocked' AND block_kind = 'needs_input' AND created_at < ?
            ORDER BY created_at ASC
            """,
            (cutoff,),
        ).fetchall()
        out = []
        for r in rows:
            out.append(
                {
                    "board": None,
                    "id": str(r["id"]),
                    "title": str(r["title"] or "")[:140],
                    "age_days": int((time.time() - int(r["created_at"])) // 86400),
                }
            )
        return out
    finally:
        con.close()


def render_digest(boards: list[tuple[str, list[dict]]]) -> str:
    lines = [
        "---",
        "title: \"Dead-PID + needs_input standing digest\"",
        "type: decision",
        "status: active",
        f"created: {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
        f"updated: {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
        "confidence: medium",
        "tags: [kanban, board-hygiene, reaper, needs-input]",
        "sources: [\"https://kanban/jarvis-os\"]",
        "project: control-plane",
        "owners: [jarvis]",
        "---",
        "",
        "# Dead-PID blocked-card reaper + needs_input digest",
        "",
        f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} by "
        "kanban-classify-failure-cron (dead_pid_blocked_reaper.py).",
        "This note is the ONE digest — it is refreshed, never per-card pings. "
        "Consumer: Jarvis voice line + Frank.",
        "",
        "## needs_input cards older than 14 days (blocked)",
        "",
    ]
    total = 0
    for board, cards in boards:
        total += len(cards)
        lines.append(f"### {board} ({len(cards)})")
        for c in cards:
            lines.append(f"- {c['id']} age={c['age_days']}d — {c['title']}")
    lines.append("")
    lines.append(f"Total needs_input >14d across boards: {total}")
    lines.append("")
    lines.append("Note: this is a standing digest. Live counts are always read from the "
                 "kanban board; this note is a routing/visibility aid, not a second source of truth.")
    return "\n".join(lines)


def _digest_comparison_content(content: str) -> str:
    """Return semantic digest content, ignoring volatile presentation metadata."""
    normalized: list[str] = []
    in_frontmatter = False
    for line in content.splitlines():
        if line == "---":
            in_frontmatter = not in_frontmatter
            normalized.append(line)
            continue
        if line.startswith("Generated "):
            continue
        if in_frontmatter and (line.startswith("created:") or line.startswith("updated:")):
            continue
        normalized.append(line)
    return "\n".join(normalized)


def write_digest(path: Path, content: str, apply: bool) -> str:
    if not apply:
        return f"DRY_DIGEST would write {path} ({len(content)} bytes)"
    if path.exists():
        try:
            if _digest_comparison_content(path.read_text(encoding="utf-8")) == _digest_comparison_content(content):
                return f"DIGEST_UNCHANGED {path} (content identical; no rewrite)"
        except OSError:
            pass
    try:
        from second_brain_writer import write_text_atomic

        write_text_atomic(path, content)
    except Exception as exc:  # pragma: no cover
        return f"DIGEST_WRITE_FAIL {path}: {type(exc).__name__}: {exc}"
    return f"DIGEST_WRITTEN {path} ({len(content)} bytes)"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="perform mutations (requeue + write digest); default dry-run")
    ap.add_argument("--boards", default=None, help="comma-separated board slugs; default = triage manifest")
    ap.add_argument("--cap", type=int, default=10, help="max requeues per tick (default 10)")
    ap.add_argument("--needs-input-days", type=int, default=14)
    ap.add_argument("--drain-gate", action="store_true",
                    help="skip reaping while an open 'DRAIN: jarvis-os' card exists (t_573abdb9 sequencing)")
    args = ap.parse_args()

    drain_gate = args.drain_gate or os.environ.get("KANBAN_DEAD_PID_DRAIN_GATE") == "1"
    boards = [b.strip() for b in args.boards.split(",") if b.strip()] if args.boards else triage_boards()

    lines: list[str] = []
    digest_boards: list[tuple[str, list[dict]]] = []
    total_reaped = 0

    drain_open = any_open_drain_card(boards) if drain_gate else False

    for board in boards:
        db = BOARD_ROOT / board / "kanban.db"
        if not db.exists():
            continue
        for ln in reap_board(db, board, args.cap, args.apply, drain_open):
            lines.append(ln)
            if ln.startswith("UNBLOCKED") or ln.startswith("DRY_UNBLOCK"):
                total_reaped += 1
        cards = needs_input_cards(db, args.needs_input_days)
        for c in cards:
            c["board"] = board
        digest_boards.append((board, cards))

    digest = render_digest(digest_boards)
    lines.append(write_digest(DIGEST_PATH, digest, args.apply))
    lines.append(f"REAPER_SUMMARY boards={len(digest_boards)} requeued={total_reaped} apply={'yes' if args.apply else 'no'} drain_gate={drain_gate}")

    if lines:
        print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
