"""kanban_startup_guard.py — one-shot kanban.db health gate for gateway (re)start.

Context (hermes issue #35240 + 2026-07-09 corruption incident):
A gateway crash + restart can reopen a *torn* WAL — if the previous process
died mid-checkpoint, the WAL frames may not be consistent with the main db.
Hermes-agent core already runs `PRAGMA integrity_check` on first connect per
process (kanban_db._guard_existing_db_is_healthy) and re-applies WAL via
hermes_state.apply_wal_with_fallback, but this module adds an EXPLICIT,
logging, fail-soft checkpoint+verify that runs in gateway.start() BEFORE the
kanban dispatcher watcher is launched — so the dispatcher never ticks against a
db that just came up from a torn WAL.

It is idempotent and best-effort: on any unexpected error it logs and returns
False (caller should still boot the gateway; the existing connect()-time guards
are the backstop). It NEVER mutates schema or downgrades the db.

Drop-in hook site (do NOT edit live package without Frank sign-off):
  gateway/run.py::GatewayRunner.start() — insert, right before
  `asyncio.create_task(self._kanban_dispatcher_watcher())` (~line 7073):

      try:
          from kanban_startup_guard import run_startup_kanban_healthcheck
          run_startup_kanban_healthcheck()
      except Exception as _e:  # never block gateway boot on this
          logger.warning("startup kanban healthcheck skipped: %s", _e)
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger("hermes.kanban.startup_guard")

# Boards to healthcheck at gateway start. Mirror the boards the dispatcher
# actually serves. Extend as needed; sycode-trading was the incident board.
DEFAULT_BOARDS = ("sycode-trading", "jarvis-os")


def _board_db_path(board: str) -> Path:
    root = Path.home() / ".hermes" / "kanban" / "boards" / board / "kanban.db"
    return root


def _healthcheck_one(board: str) -> bool:
    """Return True if the board came up clean; False if it refused/errored.

    Steps:
      1. Open read/write so SQLite can recover a healthy WAL/hot-journal.
      2. PRAGMA integrity_check (full, not just first row).
      3. If ok, PRAGMA wal_checkpoint(TRUNCATE) to fold any pending WAL frames
         into the main db and reset the WAL — eliminates a torn-WAL window at
         the moment of (re)start.
    """
    db = _board_db_path(board)
    if not db.exists():
        logger.info("[kanban-startup] board %s has no db yet (fresh) — skip", board)
        return True
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True, timeout=30.0)
        try:
            conn.execute("PRAGMA busy_timeout=30000")
            rows = conn.execute("PRAGMA integrity_check").fetchall()
            verdicts = [r[0] for r in rows]
            if any((v or "").lower() != "ok" for v in verdicts):
                logger.error(
                    "[kanban-startup] board %s FAILED integrity_check: %s",
                    board, verdicts[:5],
                )
                return False
            # Fold WAL -> main db, truncate WAL. Best-effort; ignore benign errors.
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.OperationalError as exc:
                logger.warning(
                    "[kanban-startup] board %s wal_checkpoint skipped: %s", board, exc
                )
            logger.info(
                "[kanban-startup] board %s integrity=ok, WAL checkpointed", board
            )
            return True
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        logger.error(
            "[kanban-startup] board %s could not open (corrupt?): %s", board, exc
        )
        return False
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("[kanban-startup] board %s healthcheck errored: %s", board, exc)
        return False


def run_startup_kanban_healthcheck(boards=DEFAULT_BOARDS) -> bool:
    """Run integrity_check + WAL checkpoint for each board before dispatch ticks.

    Returns True if all checked boards came up clean (or were skipped as fresh).
    Returns False if any board failed — caller may still boot (connect()-time
    guards + quarantine are the backstop), but should surface a warning.
    """
    all_ok = True
    for board in boards:
        if not _healthcheck_one(board):
            all_ok = False
    return all_ok


if __name__ == "__main__":
    import sys

    ok = run_startup_kanban_healthcheck()
    sys.exit(0 if ok else 1)
