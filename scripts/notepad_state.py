#!/usr/bin/env python3
"""notepad_state.py — durable per-job state bridge via Hermes cron notepad.

ADOPT item 5: consolidate scattered single-consumer state files into the
native cron notepad (durable per-job KV, ~/.hermes/profiles/<p>/cron/notepad.db).

This module wraps `hermes cron notepad <job_id> set/get/list` so a cron job's
script can read/write its own durable state through the notepad instead of a
loose JSON file. It is the *single* bridge the migrated jobs call.

Usage (import):
    from notepad_state import NotepadStore
    ns = NotepadStore(job_id, hermes_home)
    ns.set("cursor:sycode-trading", "1788106209")
    val = ns.get("cursor:sycode-trading")      # None when unset
    entries = ns.list_notes()                  # [{key,value,...}, ...]

Contract notes:
  - HERMES_HOME must point at the profile whose cron store owns the job
    (notepad.db is profile-local). Pass the absolute path explicitly.
  - Value cap 16KB/key, 64KB/job total (enforced by the CLI/store).
  - Never call this for multi-consumer registry files — those stay on disk.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any, Dict, List, Optional

_HERMES_BIN = os.environ.get(
    "HERMES_BIN", "/home/frank/.local/bin/hermes"
)
_SET_MSG = "Set notepad key"


def _run(job_id: str, hermes_home: str, args: List[str]) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["HERMES_HOME"] = hermes_home
    cmd = [_HERMES_BIN, "cron", "notepad", str(job_id)] + args
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=env)


class NotepadStore:
    def __init__(self, job_id: str, hermes_home: str) -> None:
        self.job_id = str(job_id)
        self.hermes_home = hermes_home
        if not os.path.isdir(hermes_home):
            raise ValueError(f"hermes_home not a dir: {hermes_home}")
        if not shutil.which(_HERMES_BIN) and not os.path.exists(_HERMES_BIN):
            raise RuntimeError(f"hermes CLI not found at {_HERMES_BIN}")

    def get(self, key: str) -> Optional[str]:
        """Return the value for key, or None if unset/empty."""
        p = _run(self.job_id, self.hermes_home, ["get", key])
        out = (p.stdout or "").strip()
        if p.returncode != 0:
            raise RuntimeError(
                f"notepad get failed for {self.job_id}:{key}: "
                f"{p.stderr.strip() or out or 'CLI returned nonzero'}"
            )
        if out.startswith("No notepad key") or out.startswith("Notepad for job"):
            return None
        if not out:
            raise RuntimeError(
                f"unexpected empty notepad get output for {self.job_id}:{key}"
            )
        return out

    def set(self, key: str, value: str) -> str:
        """Upsert a value. Raises on notepad full / CLI failure."""
        p = _run(self.job_id, self.hermes_home, ["set", key, value])
        if p.returncode != 0:
            raise RuntimeError(f"notepad set failed: {p.stderr.strip() or p.stdout.strip()}")
        if _SET_MSG not in (p.stdout or ""):
            raise RuntimeError(f"unexpected notepad set output: {p.stdout.strip()!r}")
        return value

    def delete(self, key: str) -> bool:
        p = _run(self.job_id, self.hermes_home, ["delete", key])
        return p.returncode == 0

    def list_notes(self) -> List[Dict[str, Any]]:
        p = _run(self.job_id, self.hermes_home, ["list"])
        out = (p.stdout or "").strip()
        if not out or out.startswith("Notepad for job") and " is empty" in out:
            return []
        notes: List[Dict[str, Any]] = []
        # CLI list prints "- key: value" lines per render; fall back to the
        # notepad DB directly for exact key/value pairs.
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("- ") and ": " in line[2:]:
                k, _, v = line[2:].partition(": ")
                notes.append({"key": k, "value": v})
        if notes:
            return notes
        return self._list_from_db()

    def _list_from_db(self) -> List[Dict[str, Any]]:
        import sqlite3
        db = os.path.join(self.hermes_home, "cron", "notepad.db")
        if not os.path.exists(db):
            return []
        conn = sqlite3.connect(db)
        try:
            rows = conn.execute(
                "SELECT key, value FROM cron_notepad WHERE job_id=? ORDER BY key",
                (self.job_id,),
            ).fetchall()
        finally:
            conn.close()
        return [{"key": k, "value": v} for k, v in rows]


def as_json(v: Any) -> str:
    return json.dumps(v, separators=(",", ":"))


def from_json(s: Optional[str], default: Any = None) -> Any:
    if s is None:
        return default
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        return default
