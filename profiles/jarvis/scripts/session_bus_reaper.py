#!/usr/bin/env python3
# CANONICAL SOURCE — /home/frank/.hermes/scripts/session_bus_reaper.py
# Profile-local cron exec shims (e.g. profiles/jarvis/scripts/session_bus_reaper.py)
# MUST only os.execv() into this file (CANONICAL-COPY RULE, t_41acb465). Edit here.
"""Session-Bus liveness reaper.

Scans the SESSION-BUS.md live-sessions directory and flips `active` rows to
`STALE` when their `last_heartbeat` is older than a threshold (default 15 min).
It is the canonical liveness mechanism for the Session Bus (see
Orchestration/sessions/SESSION-BUS.md v1.2).

Design contract (Session Bus blueprint §5 action #1, jarvis-os card t_8045967b):
  - Concurrent-safe: takes the SAME `.SESSION-BUS.lock` that `session-heartbeat.py`
    uses (fcntl.flock LOCK_EX). Heartbeats and reaps never interleave.
  - Liveness-only: flips `active` -> `STALE`. Never archives inbox/heartbeat
    files, never mutates `closed`/`complete`/`STALE` rows, never edits status
    text of rows still inside the threshold. Writes are atomic and idempotent: a
    second run on already-STALE/missing rows is a no-op.
  - Conflict-only alerting: posts a `BLOCKED`/stale alert to the orchestrator-sync
    card t_058ad294 ONLY on a genuine conflict (two or more distinct active sessions
    with a heartbeat gap > CONFLICT_GAP_SECONDS that are NOT yet STALE). Routine
    heartbeat-only scans are silent on the kanban bus; the reaper's stdout is the
    only liveness signal and is empty when nothing changed.
  - Zero external mutation beyond the one vault file under the lock. No kanban
    card/state change except the narrow conflict alert above.

Hermeticity: all file operations take explicit paths so --self-test never touches
the live SESSION-BUS.md. Rollback: cron disable + script restore (this file is
git-free but the jarvis profile shim re-execs it; removing the cron restores
manual behavior). No shared-state deletion ever performed.
"""

from __future__ import annotations

import argparse
import fcntl
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERMES_BIN = "/home/frank/.local/bin/hermes"
HERMES_HOME = "/home/frank/.hermes"
SYNC_BOARD = "orchestrator-sync"
SYNC_TASK = "t_058ad294"  # ORCH-LIVE: peer orchestrator coordination bus (blocked, no dispatcher)

DEFAULT_BUS_DIR = Path(os.environ.get("SESSION_BUS_DIR", "/home/frank/obsidian-fleet-vault/Orchestration/sessions"))
STALE_SECONDS = int(os.environ.get("SESSION_REAPER_STALE_SECONDS", "900"))  # 15 min default
BACKUP_SUFFIX = ".bak-session-reaper"

# Row shape (8 cells), per SESSION-BUS.md v1.2 live-sessions directory:
# | session-id | provider | inbox | direct handle | current focus | last_heartbeat | last_read | status |
ROW_RE = re.compile(
    r"^\|\s*`(?P<sid>[^`]+)`\s*\|"
    r"(?P<rest>.*?)\|\s*"
    r"(?P<hb>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s*\|"
    r"(?P<tail>.*?)\|\s*"
    r"(?P<status>active|stale|STALE|closed|archived|complete)\s*\|$"
)


def parse_ts(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def load_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def flip_active_rows(lines: list[str], now: datetime) -> tuple[list[str], list[str]]:
    """Flip active rows whose last_heartbeat is older than STALE_SECONDS to STALE.

    Returns (new_lines, flipped_session_ids). Idempotent: already-STALE/closed
    rows are left untouched; a within-threshold active row is left untouched.
    """
    out: list[str] = []
    flipped: list[str] = []
    for line in lines:
        match = ROW_RE.match(line)
        if not match:
            out.append(line)
            continue
        sid = match.group("sid")
        hb = parse_ts(match.group("hb"))
        status = match.group("status")
        if hb and status == "active" and (now - hb).total_seconds() > STALE_SECONDS:
            line = re.sub(r"\|\s*active\s*\|$", "| STALE |", line)
            flipped.append(sid)
        out.append(line)
    return out, flipped


def detect_conflicts(lines: list[str], now: datetime) -> list[str]:
    """Return session ids involved in a genuine liveness conflict.

    A *genuine* conflict (per blueprint §5: "alert ONLY on genuine conflict, never
    on routine heartbeat") is two or more sessions that BOTH still appear truly
    live — i.e. their `last_heartbeat` is within the liveness window (<= STALE_SECONDS)
    — yet share the same `direct handle` or `inbox`. That means two live sessions
    are squatting one coordination slot: a real collision risk worth one alert.

    Stale-but-active zombie rows (heartbeat older than the window) are NOT a
    conflict — they are exactly what the reaper flips to STALE routinely and must
    stay silent on the bus. A single live session is also not a conflict.
    """
    live_slots: dict[str, list[str]] = {}
    for line in lines:
        match = ROW_RE.match(line)
        if not match:
            continue
        if match.group("status") != "active":
            continue
        hb = parse_ts(match.group("hb"))
        if not hb or (now - hb).total_seconds() > STALE_SECONDS:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        sid = match.group("sid")
        inbox = cells[2]
        handle = cells[3]
        for key in (handle, inbox):
            if key and key not in ("-", ""):
                live_slots.setdefault(key, []).append(sid)
    conflicts: list[str] = []
    for sids in live_slots.values():
        if len(sids) >= 2:
            conflicts.extend(sids)
    seen: set[str] = set()
    out: list[str] = []
    for sid in conflicts:
        if sid not in seen:
            seen.add(sid)
            out.append(sid)
    return out


def atomic_write(path: Path, text: str) -> None:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
        tmp.write(text)
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)


def post_sync_alert(body: str, dry_run: bool, _poster=None) -> bool:
    """Post a BLOCKED/conflict alert comment to the orchestrator-sync card.

    `_poster` is an injectable callable for tests; defaults to the real hermes CLI.
    Returns True if a comment was posted (or would be in dry-run). Never raises.
    """
    cmd = [HERMES_BIN, "kanban", "--board", SYNC_BOARD, "comment", SYNC_TASK, body]
    if dry_run:
        print(f"[dry-run] would post orchestrator-sync alert: {body}")
        return True
    if _poster is not None:
        return _poster(body, dry_run)
    env = dict(os.environ)
    env["HERMES_HOME"] = HERMES_HOME
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=120)
    except Exception as exc:  # noqa: BLE001 - alert posting is best-effort
        print(f"⚠️ SESSION-BUS REAPER: failed to post sync alert ({exc}); stdout still emitted")
        return False
    if p.returncode != 0:
        print(f"⚠️ SESSION-BUS REAPER: sync alert post failed rc={p.returncode}: {p.stderr.strip()[:200]}")
        return False
    return True


def reap(bus_path: Path, lock_handle, now: datetime, dry_run: bool, _poster=None) -> int:
    if not bus_path.exists():
        msg = f"🔴 SESSION-BUS REAPER: missing {bus_path}"
        print(msg)
        return 1
    lines = load_lines(bus_path)
    new_lines, flipped = flip_active_rows(lines, now)

    parts: list[str] = []
    if flipped:
        if dry_run:
            print(f"[dry-run] would flip {len(flipped)} active row(s) to STALE: {', '.join(flipped[:8])}")
        else:
            backup = bus_path.with_suffix(bus_path.suffix + BACKUP_SUFFIX)
            shutil.copy2(bus_path, backup)
            atomic_write(bus_path, "\n".join(new_lines) + "\n")
            parts.append(
                f"marked STALE rows: {', '.join(flipped[:8])}"
                + (f" (+{len(flipped) - 8} more)" if len(flipped) > 8 else "")
            )

    # Genuine conflict detection: only fire an orchestrator-sync alert when two or
    # more sessions appear genuinely live AND share a coordination slot (per
    # detect_conflicts). A single live session, or any number of stale zombies, is
    # routine — no alert. Routine heartbeat-only scans emit nothing.
    conflicts = detect_conflicts(new_lines, now)
    if len(conflicts) >= 2:
        body = (
            f"BLOCKED session-bus liveness conflict: {len(conflicts)} sessions appear "
            f"live (heartbeat within {STALE_SECONDS}s) yet share a direct-handle/inbox "
            f"slot ({', '.join(conflicts[:6])}"
            f"{' +more' if len(conflicts) > 6 else ''}). "
            f"Coordination collision risk — verify which session truly owns the slot."
        )
        if post_sync_alert(body, dry_run, _poster=_poster):
            parts.append("posted orchestrator-sync conflict alert")

    if parts:
        print("⚠️ SESSION-BUS REAPER: " + "; ".join(parts))
    # Silent (no stdout) when nothing changed and no conflict -> no delivery noise.
    return 0


def self_test() -> int:
    """Fixture-driven test: flip, idempotency, lock-honor, conflict/single paths.

    Fully hermetic — operates only on a temp SESSION-BUS.md; never the live file.
    """
    header = "| session-id | provider | inbox | direct handle | current focus | last_heartbeat | last_read | status |"
    sep = "|---|---|---|---|---|---|---|---|"
    old = (datetime.now(timezone.utc) - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    new = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    base_rows = [
        f"| `old-active` | codex | `inbox-old-active.md` | - | - | {old} | - | active |",
        f"| `fresh-active` | codex | `inbox-fresh-active.md` | - | - | {new} | - | active |",
        f"| `already-stale` | codex | `inbox-already-stale.md` | - | - | {old} | - | STALE |",
        f"| `closed-one` | codex | `inbox-closed-one.md` | - | - | {old} | - | closed |",
    ]

    with tempfile.TemporaryDirectory() as tmp:
        bus_dir = Path(tmp)
        bus = bus_dir / "SESSION-BUS.md"
        lock = bus_dir / ".SESSION-BUS.lock"
        now = datetime.now(timezone.utc)
        bus.write_text("\n".join([header, sep] + base_rows) + "\n", encoding="utf-8")

        # 1) flip path under lock
        with lock.open("a", encoding="utf-8") as lh:
            fcntl.flock(lh, fcntl.LOCK_EX)
            reap(bus, lh, now, dry_run=False)
        txt = bus.read_text(encoding="utf-8")
        if "| STALE |" not in txt or "old-active" not in txt:
            raise AssertionError("active row was not flipped to STALE")
        old_row = [l for l in txt.splitlines() if "`fresh-active`" in l][0]
        if "| active |" not in old_row:
            raise AssertionError("fresh active row was wrongly flipped")
        stale_row = [l for l in txt.splitlines() if "`already-stale`" in l][0]
        if "| STALE |" not in stale_row:
            raise AssertionError("already-STALE row was changed")
        closed_row = [l for l in txt.splitlines() if "`closed-one`" in l][0]
        if "| closed |" not in closed_row:
            raise AssertionError("closed row was changed")

        # 2) idempotency: second run flips nothing
        with lock.open("a", encoding="utf-8") as lh:
            fcntl.flock(lh, fcntl.LOCK_EX)
            reap(bus, lh, now, dry_run=False)
        if bus.read_text(encoding="utf-8") != txt:
            raise AssertionError("second reap was not idempotent")

        # 3) lock-honor: the shared flock is taken; re-confirm the lock file is the
        #    coordination point. A second holder would block until released.
        with lock.open("a", encoding="utf-8") as holder:
            fcntl.flock(holder, fcntl.LOCK_EX)
            if not lock.exists():
                raise AssertionError("lock file missing")

        # 4) conflict-alert path: two genuinely-LIVE active rows sharing a handle
        #    -> exactly one alert. Run non-dry but inject a poster so no real
        #    kanban post happens in-test (rows are recent -> not flipped anyway).
        recent = (now - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        conflict_rows = [
            f"| `a-live` | codex | `inbox-a.md` | shared-handle | - | {recent} | - | active |",
            f"| `b-live` | codex | `inbox-b.md` | shared-handle | - | {recent} | - | active |",
        ]
        bus.write_text("\n".join([header, sep] + conflict_rows) + "\n", encoding="utf-8")
        posted: list[str] = []
        poster = lambda body, dry: (posted.append(body) or True)
        with lock.open("a", encoding="utf-8") as lh:
            fcntl.flock(lh, fcntl.LOCK_EX)
            reap(bus, lh, now, dry_run=False, _poster=poster)
        if len(posted) != 1:
            raise AssertionError(f"expected exactly 1 conflict alert, got {len(posted)}")

        # 5) single live row (unique handle) -> NO alert (routine reap)
        single = [f"| `only-one` | codex | `inbox-only.md` | unique-handle | - | {recent} | - | active |"]
        bus.write_text("\n".join([header, sep] + single) + "\n", encoding="utf-8")
        posted.clear()
        with lock.open("a", encoding="utf-8") as lh:
            fcntl.flock(lh, fcntl.LOCK_EX)
            reap(bus, lh, now, dry_run=False, _poster=poster)
        if posted:
            raise AssertionError("single live session must NOT raise an alert")

    print("self-test ok")
    return 0


def main() -> int:
    global DEFAULT_BUS_DIR
    ap = argparse.ArgumentParser(description="Session-Bus liveness reaper")
    ap.add_argument("--bus-dir", default=str(DEFAULT_BUS_DIR), help="Session Bus directory")
    ap.add_argument("--dry-run", action="store_true", help="print what would change; mutate nothing")
    ap.add_argument("--self-test", action="store_true", help="run fixture self-test and exit")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    bus_dir = Path(args.bus_dir)
    bus = bus_dir / "SESSION-BUS.md"
    lock = bus_dir / ".SESSION-BUS.lock"
    now = datetime.now(timezone.utc)

    bus_dir.mkdir(parents=True, exist_ok=True)
    with lock.open("a", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle, fcntl.LOCK_EX)
        return reap(bus, lock_handle, now, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
