"""Unit tests for route_models_kanban_pin.py (kanban t_4051b1cd).

Covers the two safety-critical behaviours the owning task called out:
  1. A seat below the shim's own 10% headroom floor is never chosen, even
     if route_models' own (looser, 5%) floor would still call it "available".
  2. When every seat for a tier is exhausted, the shim reports and returns
     0 without touching any board (never a pause/resume, never a crash).
Board-read and CLI-apply paths are exercised against a throwaway sqlite
file / a stubbed subprocess so no real board or process is touched.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
import route_models_kanban_pin as pin  # noqa: E402
import route_models  # noqa: E402


def _seat(model, provider, tiers, headroom, status="available", reserved_for=None,
          stale_age_hours=None):
    return {
        "model": model,
        "provider": provider,
        "tiers": tiers,
        "rank": 0,
        "reserved_for": reserved_for,
        "used_percent": 100 - headroom if headroom is not None else None,
        "headroom": headroom,
        "reset_at": None,
        "plan": None,
        "status": status,
        "note": "",
        "depleted": None,
        "stale_age_hours": stale_age_hours,
    }


def test_seat_below_shim_floor_is_excluded_even_if_route_models_would_route_it():
    # 9% headroom: route_models.HEADROOM_FLOOR (5%) would still call this
    # "available", but the shim's own 10% floor must reject it.
    state = {
        "seats": [_seat("claude-sonnet-5", "anthropic", ["mid"], headroom=9.0)],
    }
    assert pin.choose_for_pin(state, "mid") is None


def test_seat_at_exactly_the_floor_is_excluded():
    # Rejection item 4: boundary is inclusive on the excluded side. Spec says
    # exclude at >=90% used (<=10% headroom); a seat with EXACTLY 10.0%
    # headroom (90.0% used) must be excluded, not admitted. Previously this
    # test passed 9.99 and never exercised the real boundary — fixed to
    # assert on the literal 10.0 value the docstring claims.
    state = {"seats": [_seat("claude-sonnet-5", "anthropic", ["mid"], headroom=10.0)]}
    assert pin.choose_for_pin(state, "mid") is None


def test_seat_just_above_floor_is_routable():
    # 10.01% headroom (89.99% used) sits just inside the routable side.
    state = {"seats": [_seat("claude-sonnet-5", "anthropic", ["mid"], headroom=10.01)]}
    pick = pin.choose_for_pin(state, "mid")
    assert pick is not None
    assert pick["model"] == "claude-sonnet-5"


def test_seat_above_floor_is_routable():
    state = {"seats": [_seat("claude-sonnet-5", "anthropic", ["mid"], headroom=40.0)]}
    pick = pin.choose_for_pin(state, "mid")
    assert pick is not None
    assert pick["model"] == "claude-sonnet-5"


def test_nous_never_chosen_even_with_headroom():
    state = {"seats": [_seat("nous-flash", "nous", ["mid"], headroom=80.0)]}
    assert pin.choose_for_pin(state, "mid") is None


def test_no_usage_api_seat_with_unknown_headroom_is_still_routable():
    # xai-oauth reports no usage API -> headroom is None, not a failure.
    state = {"seats": [_seat("grok-4.6", "xai-oauth", ["mid"], headroom=None)]}
    pick = pin.choose_for_pin(state, "mid")
    assert pick is not None
    assert pick["model"] == "grok-4.6"


def test_failing_or_throttled_seat_excluded_regardless_of_headroom():
    for bad_status in ("failing", "depleted", "throttled"):
        state = {"seats": [_seat("claude-sonnet-5", "anthropic", ["mid"], headroom=90.0, status=bad_status)]}
        assert pin.choose_for_pin(state, "mid") is None


def test_reserved_seat_not_chosen_for_a_different_tier():
    state = {
        "seats": [
            _seat("claude-fable-5", "anthropic", ["orchestrator"], headroom=90.0, reserved_for="orchestrator"),
        ],
    }
    assert pin.choose_for_pin(state, "mid") is None


def test_tier_rank_governs_ordering_when_multiple_seats_route(monkeypatch):
    # grok-4.6 is rank 0 for "mid" per route_models.TIER_RANK; sonnet-5 rank 1.
    state = {
        "seats": [
            _seat("claude-sonnet-5", "anthropic", ["mid"], headroom=90.0),
            _seat("grok-4.6", "xai-oauth", ["mid"], headroom=10.5),
        ],
    }
    pick = pin.choose_for_pin(state, "mid")
    assert pick["model"] == "grok-4.6"


def test_all_seats_exhausted_reports_and_does_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(pin, "dispatch_boards", lambda: ["fake-board"])
    monkeypatch.setattr(
        route_models, "build_state",
        lambda do_probe=False: {"seats": [_seat("claude-sonnet-5", "anthropic", ["mid"], headroom=0.0, status="exhausted")]},
    )
    apply_mock = mock.Mock()
    monkeypatch.setattr(pin, "apply_pin", apply_mock)

    monkeypatch.setattr(sys, "argv", ["route_models_kanban_pin.py", "--dry-run"])
    rc = pin.main()

    out = capsys.readouterr().out
    assert rc == 0
    assert "ALL SEATS EXHAUSTED" in out
    apply_mock.assert_not_called()


def test_ready_unpinned_cards_reads_only_null_or_empty_override(tmp_path, monkeypatch):
    board_dir = tmp_path / "fake-board"
    board_dir.mkdir()
    db_path = board_dir / "kanban.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, model_override TEXT)"
    )
    conn.executemany(
        "INSERT INTO tasks VALUES (?, ?, ?)",
        [
            ("t_ready_unpinned", "ready", None),
            ("t_ready_empty", "ready", ""),
            ("t_ready_pinned", "ready", "claude-sonnet-5"),
            ("t_not_ready", "todo", None),
        ],
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(pin, "BOARDS_DIR", tmp_path)
    got = set(pin.ready_unpinned_cards("fake-board"))

    assert got == {"t_ready_unpinned", "t_ready_empty"}


def test_missing_board_db_returns_empty_list_not_error(tmp_path, monkeypatch):
    monkeypatch.setattr(pin, "BOARDS_DIR", tmp_path)
    assert pin.ready_unpinned_cards("does-not-exist") == []


def test_apply_pin_strips_delegated_child_context_env(monkeypatch):
    captured = {}

    def fake_run(cmd, capture_output, text, timeout, env, cwd):
        captured["env"] = env
        captured["cmd"] = cmd

        class R:
            returncode = 0
            stdout = "ok"
            stderr = ""

        return R()

    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", "1")
    monkeypatch.setattr(pin.subprocess, "run", fake_run)

    ok, msg = pin.apply_pin("jarvis-os", "t_abc", "grok-4.6", "xai-oauth")

    assert ok is True
    assert "HERMES_DELEGATED_CHILD_CONTEXT" not in captured["env"]
    assert captured["cmd"][:4] == ["hermes", "kanban", "--board", "jarvis-os"]


def test_apply_pin_strips_kanban_db_and_board_env_cross_board_hazard(monkeypatch):
    # Rejection blocker 2: HERMES_KANBAN_DB outranks --board inside
    # _board_path, so any ambient board pin from this cron's own process env
    # silently redirects (or fails) a set-model call meant for a DIFFERENT
    # board. Proven live against sycode-trading card t_b9cd957b while the
    # process carried jarvis-os's DB path. Both must be stripped from the
    # child env unconditionally.
    captured = {}

    def fake_run(cmd, capture_output, text, timeout, env, cwd):
        captured["env"] = env

        class R:
            returncode = 0
            stdout = "ok"
            stderr = ""

        return R()

    monkeypatch.setenv("HERMES_KANBAN_DB", "/home/frank/.hermes/kanban/boards/jarvis-os/kanban.db")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "jarvis-os")
    monkeypatch.setattr(pin.subprocess, "run", fake_run)

    ok, msg = pin.apply_pin("sycode-trading", "t_b9cd957b", "grok-4.6", "xai-oauth")

    assert ok is True
    assert "HERMES_KANBAN_DB" not in captured["env"]
    assert "HERMES_KANBAN_BOARD" not in captured["env"]


def test_stale_cache_reading_beyond_ttl_is_not_routed_on():
    # Rejection item 3: stale_age_hours was computed but never consulted.
    # An epoch-0 cache entry (age ~496,952.5h) was routed on as if fresh.
    # A reading older than MAX_STALE_CACHE_AGE_HOURS must be excluded.
    state = {
        "seats": [
            _seat("claude-sonnet-5", "anthropic", ["mid"], headroom=58.0,
                  status="available", stale_age_hours=496952.5),
        ],
    }
    assert pin.choose_for_pin(state, "mid") is None


def test_stale_cache_reading_within_ttl_is_still_routable():
    state = {
        "seats": [
            _seat("claude-sonnet-5", "anthropic", ["mid"], headroom=58.0,
                  status="available", stale_age_hours=1.5),
        ],
    }
    pick = pin.choose_for_pin(state, "mid")
    assert pick is not None
    assert pick["model"] == "claude-sonnet-5"


def test_fresh_reading_with_no_stale_age_is_routable():
    # Live (non-stale) readings never set stale_age_hours -> None -> no TTL
    # rejection.
    state = {
        "seats": [
            _seat("claude-sonnet-5", "anthropic", ["mid"], headroom=58.0,
                  status="available", stale_age_hours=None),
        ],
    }
    pick = pin.choose_for_pin(state, "mid")
    assert pick is not None


def test_lock_blocks_concurrent_tick(tmp_path, monkeypatch):
    # Rejection item 5: no concurrency guard. A held lock must cause a second
    # concurrent invocation to back off (acquired=False) instead of racing
    # the same board writes.
    monkeypatch.setattr(pin, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(pin, "LOCK_PATH", tmp_path / "route_models_kanban_pin.lock")

    with pin._pin_lock() as first:
        assert first is True
        with pin._pin_lock() as second:
            assert second is False

    # Lock released after the outer context exits -> acquirable again.
    with pin._pin_lock() as third:
        assert third is True


def test_all_seats_exhausted_still_respects_lock_and_skip_message(tmp_path, monkeypatch, capsys):
    # main() end-to-end: a held lock produces a SKIPPED message and rc 0,
    # never attempts to read boards/apply pins.
    monkeypatch.setattr(pin, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(pin, "LOCK_PATH", tmp_path / "route_models_kanban_pin.lock")
    monkeypatch.setattr(sys, "argv", ["route_models_kanban_pin.py", "--dry-run"])

    run_mock = mock.Mock()
    monkeypatch.setattr(pin, "_run", run_mock)

    import fcntl

    # Simulate a held lock by acquiring it in this process first (flock is
    # per-fd, not per-process, so a second fd from the same process still
    # contends for the advisory lock on Linux with LOCK_NB).
    pin.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    held_fd = os.open(str(pin.LOCK_PATH), os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(held_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        rc = pin.main()
    finally:
        fcntl.flock(held_fd, fcntl.LOCK_UN)
        os.close(held_fd)

    out = capsys.readouterr().out
    assert rc == 0
    assert "SKIPPED" in out
    run_mock.assert_not_called()

