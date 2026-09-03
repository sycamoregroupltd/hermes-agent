"""Side-effect-free regression tests for the dead-PID reaper drain gate."""
from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).parents[1] / "dead_pid_blocked_reaper.py"
SPEC = importlib.util.spec_from_file_location("dead_pid_blocked_reaper", SCRIPT)
assert SPEC and SPEC.loader
reaper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reaper)


def test_open_drain_card_fails_closed_on_sqlite_read_error() -> None:
    db = Path("/unavailable/kanban.db")
    with patch.object(reaper, "connect", side_effect=sqlite3.OperationalError("database is malformed")):
        assert reaper.open_drain_card(db) is True


def test_open_drain_card_fails_closed_on_os_read_error() -> None:
    db = Path("/unavailable/kanban.db")
    with patch.object(reaper, "connect", side_effect=OSError("permission denied")):
        assert reaper.open_drain_card(db) is True


def test_read_error_keeps_reaping_closed_without_unblock() -> None:
    db = Path("/unavailable/kanban.db")
    with patch.object(reaper, "connect", side_effect=sqlite3.DatabaseError("malformed")):
        drain_open = reaper.any_open_drain_card(["jarvis-os"])
    with patch.object(reaper.subprocess, "run") as unblock:
        lines = reaper.reap_board(db, "jarvis-os", cap=10, apply=True, drain_open=drain_open)
    assert drain_open is True
    assert lines == ["REAP_SKIPPED drain-gate: open 'DRAIN: jarvis-os' card present (waiting for t_573abdb9)"]
    unblock.assert_not_called()


def test_digest_dry_run_has_no_write_side_effect(tmp_path: Path) -> None:
    digest = tmp_path / "digest.md"
    original = "prior digest\n"
    digest.write_text(original, encoding="utf-8")
    result = reaper.write_digest(digest, "new digest\n", apply=False)
    assert result.startswith("DRY_DIGEST would write")
    assert digest.read_text(encoding="utf-8") == original
