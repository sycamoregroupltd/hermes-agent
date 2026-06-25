"""Persistent run tracking for ``hermes agent`` subprocesses."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home


_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_runs (
    run_id TEXT PRIMARY KEY,
    profile TEXT NOT NULL,
    prompt TEXT NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    pid INTEGER,
    cwd TEXT,
    command_json TEXT NOT NULL,
    context_files_json TEXT NOT NULL,
    env_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    started_at REAL,
    ended_at REAL,
    returncode INTEGER,
    stdout TEXT,
    stderr TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_runs_created_at ON agent_runs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_runs_status ON agent_runs(status);
"""

_JSON_FIELDS = {"command", "context_files", "env"}


class AgentRunStore:
    """SQLite-backed agent-run registry scoped to the active HERMES_HOME."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (get_hermes_home() / "agent_runs.db")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @staticmethod
    def new_run_id() -> str:
        return f"ar_{uuid.uuid4().hex[:16]}"

    def create(
        self,
        *,
        run_id: str,
        profile: str,
        prompt: str,
        mode: str,
        status: str,
        command: list[str],
        context_files: list[str] | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        pid: int | None = None,
    ) -> None:
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO agent_runs (
                    run_id, profile, prompt, mode, status, pid, cwd,
                    command_json, context_files_json, env_json,
                    created_at, started_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    profile,
                    prompt,
                    mode,
                    status,
                    pid,
                    cwd,
                    json.dumps(command),
                    json.dumps(context_files or []),
                    json.dumps(env or {}),
                    now,
                    now,
                ),
            )

    def mark_finished(
        self,
        run_id: str,
        *,
        status: str,
        returncode: int | None,
        stdout: str | None,
        stderr: str | None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE agent_runs
                   SET status = ?, returncode = ?, stdout = ?, stderr = ?, ended_at = ?
                 WHERE run_id = ?
                """,
                (status, returncode, stdout, stderr, time.time(), run_id),
            )

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM agent_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["command"] = json.loads(data.pop("command_json") or "[]")
        data["context_files"] = json.loads(data.pop("context_files_json") or "[]")
        data["env"] = json.loads(data.pop("env_json") or "{}")
        return data
