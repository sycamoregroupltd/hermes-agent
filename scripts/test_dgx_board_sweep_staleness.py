#!/usr/bin/env python3
"""test_dgx_board_sweep_staleness.py — regression test for t_a578e65d.

Bug: probe_board() treated EVERY missing kanban.db as a generic
"db_missing" error, which main() always maps to a non-zero exit code
(DB_ACCESS_ERROR). But the fleet boards manifest deliberately declares
some boards `dormant` with no on-disk kanban.db (e.g. sycode-ai — a
permanent alias of upero; legacy-yss — superseded by yorkstone-supplies).
Because dgx_board_sweep_staleness.py's own BOARDS list includes dormant
boards (sweep=true is set for them so they still get read-only reporting),
the probe exited 2 on every single run forever, regardless of whether any
real dispatch gap existed. That produced the recurring
dgx-board-staleness-dispatch-gap cron auto-error card (t_e339f8a7 ->
t_a578e65d).

Fix: a missing DB on a board whose manifest `state` is "dormant" is
expected and must not flip the exit code; only a missing DB on an
active/unknown board is a genuine access error.
"""
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

import dgx_board_sweep_staleness as mod  # noqa: E402


def test_probe_board_dormant_missing_db_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "KANBAN_HOME", str(tmp_path))
    monkeypatch.setattr(mod, "_state_for", lambda board: "dormant")

    entry = mod.probe_board("sycode-ai", now=1000)

    assert entry["present"] is False
    assert entry["error"] == "db_missing_dormant_expected"
    assert entry["dispatch_gap"] is False


def test_probe_board_active_missing_db_is_still_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "KANBAN_HOME", str(tmp_path))
    monkeypatch.setattr(mod, "_state_for", lambda board: "active")

    entry = mod.probe_board("sycode-trading", now=1000)

    assert entry["present"] is False
    assert entry["error"] == "db_missing"


def test_probe_board_unknown_missing_db_is_still_an_error(tmp_path, monkeypatch):
    # No manifest entry at all (fleet_boards import failed, or board absent
    # from the manifest) must stay conservative and surface as an error,
    # never silently swallowed as "dormant".
    monkeypatch.setattr(mod, "KANBAN_HOME", str(tmp_path))
    monkeypatch.setattr(mod, "_state_for", lambda board: "unknown")

    entry = mod.probe_board("some-undeclared-board", now=1000)

    assert entry["present"] is False
    assert entry["error"] == "db_missing"


def test_main_exit_code_zero_when_only_dormant_boards_are_missing(monkeypatch, tmp_path):
    """End-to-end: main() must exit 0 when the only 'error' entries are
    expected dormant-board absences and no board has a real dispatch gap —
    reproducing the exact steady-state that used to exit 2 forever."""
    monkeypatch.setattr(mod, "BOARDS", ["dormant-board-1", "active-board-1"])
    monkeypatch.setattr(mod, "KANBAN_HOME", str(tmp_path))

    def fake_state_for(board):
        return "dormant" if board == "dormant-board-1" else "active"

    monkeypatch.setattr(mod, "_state_for", fake_state_for)

    # active-board-1's DB exists and is empty (no ready/running rows) so it
    # reports cleanly with no gap; dormant-board-1 has no DB at all.
    import sqlite3

    db_dir = tmp_path / "active-board-1"
    db_dir.mkdir(parents=True)
    con = sqlite3.connect(str(db_dir / "kanban.db"))
    con.execute("CREATE TABLE tasks (status TEXT, created_at INTEGER)")
    con.commit()
    con.close()

    monkeypatch.setattr(sys, "argv", ["dgx_board_sweep_staleness.py"])
    with mock.patch("builtins.open", mock.mock_open()):
        rc = mod.main()

    assert rc == 0


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
