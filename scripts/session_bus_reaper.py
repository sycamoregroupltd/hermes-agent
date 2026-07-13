#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
"""Session-bus reaper/marker.

Daily no-agent sweep for the Obsidian session bus:
- mark live session table rows STALE when last_heartbeat is older than 12h;
- archive stale session/inbox/heartbeat files with mtime older than 24h;
- silent when no rows/files change; stdout is the alert/audit line.
"""

from __future__ import annotations

import re
import shutil
import os
from datetime import datetime, timezone
from pathlib import Path

BUS_DIR = Path(os.environ.get("SESSION_BUS_DIR", "/home/frank/obsidian-fleet-vault/Orchestration/sessions"))
BUS = BUS_DIR / "SESSION-BUS.md"
ARCHIVE = BUS_DIR / "archive"
NOW = datetime.now(timezone.utc)
STALE_SECONDS = 12 * 3600
ARCHIVE_SECONDS = 24 * 3600
ROW_RE = re.compile(r"^\| `(?P<sid>[^`]+)` \|(?P<rest>.*)\| (?P<hb>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z) \|(?P<tail>.*)\| (?P<status>active|stale|STALE|closed|archived) \|\s*$")


def parse_ts(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def mark_stale_rows() -> list[str]:
    if not BUS.exists():
        print(f"🔴 SESSION-BUS REAPER: missing {BUS}")
        return []
    lines = BUS.read_text().splitlines()
    changed: list[str] = []
    out: list[str] = []
    for line in lines:
        match = ROW_RE.match(line)
        if match:
            hb = parse_ts(match.group("hb"))
            status = match.group("status")
            if hb and (NOW - hb).total_seconds() > STALE_SECONDS and status == "active":
                line = line.rsplit("| active |", 1)[0] + "| STALE |"
                changed.append(match.group("sid"))
        out.append(line)
    if changed:
        backup = BUS.with_suffix(BUS.suffix + ".bak-session-reaper")
        shutil.copy2(BUS, backup)
        BUS.write_text("\n".join(out) + "\n")
    return changed


def archive_old_files() -> list[str]:
    archived: list[str] = []
    ARCHIVE.mkdir(exist_ok=True)
    now_ts = NOW.timestamp()
    for pattern in ("inbox-*.md", "heartbeat-*.txt"):
        for path in BUS_DIR.glob(pattern):
            if path.parent == ARCHIVE or not path.is_file():
                continue
            age = now_ts - path.stat().st_mtime
            if age <= ARCHIVE_SECONDS:
                continue
            dest = ARCHIVE / path.name
            if dest.exists():
                dest = ARCHIVE / f"{path.stem}-{NOW.strftime('%Y%m%dT%H%M%SZ')}{path.suffix}"
            shutil.move(str(path), str(dest))
            archived.append(path.name)
    return archived


def main() -> None:
    stale = mark_stale_rows()
    archived = archive_old_files()
    if stale or archived:
        parts = []
        if stale:
            parts.append(f"marked STALE rows: {', '.join(stale[:8])}" + (f" (+{len(stale)-8} more)" if len(stale) > 8 else ""))
        if archived:
            parts.append(f"archived old files: {', '.join(archived[:8])}" + (f" (+{len(archived)-8} more)" if len(archived) > 8 else ""))
        print("⚠️ SESSION-BUS REAPER: " + "; ".join(parts))


if __name__ == "__main__":
    main()
