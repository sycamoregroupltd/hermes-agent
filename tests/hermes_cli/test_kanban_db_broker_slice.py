"""Tests for the native broker/orchestrator control-loop slice.

Authority under test is the native per-board DB only: ``task_events`` for
ordering and dedup, ``task_runs`` for completions, ``tasks`` for routing
inputs. There is no parallel queue, lease file, JSON cursor or Markdown
anywhere in these paths, and nothing here spawns a worker or invokes a
provider.

Every test operates on a **disposable** DB: either a freshly initialised one
under ``tmp_path``, or a snapshot of a real board taken through SQLite's backup
API from a read-only source connection. No test writes to a live board.
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import threading
import types
import time
from pathlib import Path
from unittest import mock

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import claude_executor as ce
from hermes_cli.broker_shadow import CanonicalShadowBroker, ShadowBrokerDisabled
from hermes_cli.claude_executor import ClaudeResumeExecutor

LIVE_BOARD = Path("/home/frank/.hermes/kanban/boards/orchestrator-sync/kanban.db")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _fresh_db(path: Path) -> Path:
    kb.init_db(path)
    return path


def _seed_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    status: str = "running",
    session_id: str | None = None,
    provider: str | None = None,
    failures: int = 0,
    max_retries: int | None = None,
) -> None:
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO tasks (id, title, status, created_at, workspace_kind,"
            " consecutive_failures, max_retries, session_id, provider_override,"
            " goal_mode) VALUES (?,?,?,?,?,?,?,?,?,0)",
            (
                task_id,
                "seeded",
                status,
                1_700_000_000,
                "scratch",
                failures,
                max_retries,
                session_id,
                provider,
            ),
        )


def _seed_run(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    outcome: str | None = "completed",
    status: str = "done",
    ended: int | None = 1_700_000_100,
    started: int = 1_700_000_000,
    worker_session_id: str | None = None,
    profile: str | None = "worker",
    worker_session_source: str | None = None,
) -> int:
    with kb.write_txn(conn):
        cur = conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, started_at, ended_at,"
            " outcome, worker_session_id, worker_session_source)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (task_id, profile, status, started, ended, outcome, worker_session_id,
             worker_session_source),
        )
    return int(cur.lastrowid)


def _fold_and_route(conn, run_id: int, **route_kwargs):
    """Route through the REAL flow: fold the run, read the stored payload, route.

    Deliberately not hand-built kwargs — N1 was a fail-open that only the
    canonical path exposed.
    """
    kb.record_worker_completion_events(conn)
    payload = _completion_payload(conn, run_id)
    task_id = payload["task_id"]
    return kb.decide_route(
        completion=payload, task_row=_task_row(conn, task_id), **route_kwargs
    )


def _task_row(conn: sqlite3.Connection, task_id: str):
    return conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()


def _completion_payload(conn: sqlite3.Connection, run_id: int) -> dict:
    row = conn.execute(
        "SELECT payload FROM task_events WHERE run_id = ? AND kind = ?",
        (run_id, kb.BROKER_EVENT_WORKER_COMPLETION),
    ).fetchone()
    return json.loads(row["payload"])


@pytest.fixture
def db(tmp_path):
    """A disposable, freshly-initialised board DB."""
    path = _fresh_db(tmp_path / "board-a" / "kanban.db")
    conn = kb.connect(path)
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------


class TestSchema:
    def test_broker_sub_table_exists_with_expected_shape(self, db):
        cols = {r["name"]: r for r in db.execute("PRAGMA table_info(kanban_broker_subs)")}
        assert set(cols) == {
            "consumer", "last_event_id", "created_at", "updated_at", "token_sha256",
        }
        assert cols["last_event_id"]["type"].upper() == "INTEGER"
        assert cols["last_event_id"]["notnull"] == 1
        assert cols["consumer"]["pk"] == 1

    def test_completion_dedup_index_is_a_partial_unique_index(self, db):
        row = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' "
            "AND name='idx_events_completion_once'"
        ).fetchone()
        assert row is not None, "the exactly-once guard must exist in the schema"
        sql = row["sql"].lower()
        assert "unique" in sql
        assert "where" in sql and "worker_completion_observed" in sql

    def test_the_dedup_guard_is_enforced_by_the_database(self, db):
        """Not an application-side ledger: the DB itself refuses the second row."""
        _seed_task(db, "t_a")
        run_id = _seed_run(db, "t_a")
        for _ in range(2):
            with pytest.raises(sqlite3.IntegrityError):
                with kb.write_txn(db):
                    for _dup in range(2):
                        db.execute(
                            "INSERT INTO task_events (task_id, run_id, kind, payload,"
                            " created_at) VALUES (?,?,?,?,?)",
                            ("t_a", run_id, kb.BROKER_EVENT_WORKER_COMPLETION, "{}", 1),
                        )

    def test_other_event_kinds_are_unconstrained(self, db):
        _seed_task(db, "t_a")
        run_id = _seed_run(db, "t_a")
        with kb.write_txn(db):
            for _ in range(3):
                db.execute(
                    "INSERT INTO task_events (task_id, run_id, kind, payload, created_at)"
                    " VALUES (?,?,?,?,?)",
                    ("t_a", run_id, "commented", None, 1),
                )
        n = db.execute(
            "SELECT COUNT(*) c FROM task_events WHERE kind='commented'"
        ).fetchone()["c"]
        assert n == 3


# ---------------------------------------------------------------------------
# typed event validation
# ---------------------------------------------------------------------------


class TestTypedValidation:
    def _completion(self, **over):
        base = {
            "run_id": 1,
            "task_id": "t_a",
            "outcome": "completed",
            "run_status": "done",
        }
        base.update(over)
        return base

    def _route(self, **over):
        base = {
            "run_id": 1,
            "task_id": "t_a",
            "route": kb.ROUTE_REVIEW,
            "reason": "x",
            "outcome": "completed",
            "spawn": False,
        }
        base.update(over)
        return base

    def test_a_valid_completion_normalises(self):
        out = kb.validate_broker_event_payload(
            kb.BROKER_EVENT_WORKER_COMPLETION, self._completion()
        )
        assert out["run_id"] == 1 and out["outcome"] == "completed"

    def test_unknown_kind_is_rejected(self):
        with pytest.raises(kb.BrokerEventValidationError, match="not a broker event kind"):
            kb.validate_broker_event_payload("commented", {})

    def test_missing_required_field_is_rejected(self):
        payload = self._completion()
        del payload["outcome"]
        with pytest.raises(kb.BrokerEventValidationError, match="missing required field"):
            kb.validate_broker_event_payload(kb.BROKER_EVENT_WORKER_COMPLETION, payload)

    def test_unknown_field_is_rejected(self):
        with pytest.raises(kb.BrokerEventValidationError, match="unknown field"):
            kb.validate_broker_event_payload(
                kb.BROKER_EVENT_WORKER_COMPLETION, self._completion(surprise=1)
            )

    def test_wrong_type_is_rejected(self):
        with pytest.raises(kb.BrokerEventValidationError, match="must be int"):
            kb.validate_broker_event_payload(
                kb.BROKER_EVENT_WORKER_COMPLETION, self._completion(run_id="1")
            )

    def test_bool_is_not_accepted_as_int(self):
        with pytest.raises(kb.BrokerEventValidationError, match="must be int, got bool"):
            kb.validate_broker_event_payload(
                kb.BROKER_EVENT_WORKER_COMPLETION, self._completion(run_id=True)
            )

    def test_int_is_not_accepted_as_bool(self):
        with pytest.raises(kb.BrokerEventValidationError, match="must be bool"):
            kb.validate_broker_event_payload(kb.BROKER_EVENT_ROUTE_DECIDED, self._route(spawn=1))

    def test_a_non_terminal_outcome_is_rejected(self):
        with pytest.raises(kb.BrokerEventValidationError, match="is not terminal"):
            kb.validate_broker_event_payload(
                kb.BROKER_EVENT_WORKER_COMPLETION, self._completion(outcome="running")
            )

    def test_an_unknown_route_is_rejected(self):
        with pytest.raises(kb.BrokerEventValidationError, match="unknown route"):
            kb.validate_broker_event_payload(kb.BROKER_EVENT_ROUTE_DECIDED, self._route(route="spawn"))

    def test_a_spawning_decision_cannot_be_expressed(self):
        """Structural inertness guard, not a policy comment."""
        with pytest.raises(kb.BrokerEventValidationError, match="spawn must be False"):
            kb.validate_broker_event_payload(kb.BROKER_EVENT_ROUTE_DECIDED, self._route(spawn=True))

    def test_a_non_dict_payload_is_rejected(self):
        with pytest.raises(kb.BrokerEventValidationError, match="must be a dict"):
            kb.validate_broker_event_payload(kb.BROKER_EVENT_WORKER_COMPLETION, None)


# ---------------------------------------------------------------------------
# terminal / non-terminal folding + exactly-once
# ---------------------------------------------------------------------------


class TestCompletionFolding:
    def test_a_terminal_run_folds_into_one_typed_event(self, db):
        _seed_task(db, "t_a")
        run_id = _seed_run(db, "t_a", outcome="completed")

        recorded = kb.record_worker_completion_events(db)
        assert recorded == [run_id]

        payload = _completion_payload(db, run_id)
        assert payload["task_id"] == "t_a"
        assert payload["outcome"] == "completed"
        assert payload["run_id"] == run_id

    def test_folding_is_exactly_once_across_repeated_passes(self, db):
        _seed_task(db, "t_a")
        run_id = _seed_run(db, "t_a")

        first = kb.record_worker_completion_events(db)
        second = kb.record_worker_completion_events(db)
        third = kb.record_worker_completion_events(db)

        assert first == [run_id]
        assert second == [] and third == []
        n = db.execute(
            "SELECT COUNT(*) c FROM task_events WHERE kind = ?",
            (kb.BROKER_EVENT_WORKER_COMPLETION,),
        ).fetchone()["c"]
        assert n == 1

    def test_folding_is_exactly_once_across_concurrent_consumers(self, tmp_path):
        path = _fresh_db(tmp_path / "board" / "kanban.db")
        setup = kb.connect(path)
        _seed_task(setup, "t_a")
        run_id = _seed_run(setup, "t_a")
        setup.close()

        results: list[list[int]] = []
        lock = threading.Lock()
        barrier = threading.Barrier(6)

        def worker() -> None:
            conn = kb.connect(path)
            try:
                barrier.wait()
                got = kb.record_worker_completion_events(conn)
            finally:
                conn.close()
            with lock:
                results.append(got)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sum(len(r) for r in results) == 1, f"exactly one writer may win: {results}"
        conn = kb.connect(path)
        try:
            n = conn.execute(
                "SELECT COUNT(*) c FROM task_events WHERE kind = ?",
                (kb.BROKER_EVENT_WORKER_COMPLETION,),
            ).fetchone()["c"]
        finally:
            conn.close()
        assert n == 1
        assert run_id > 0

    def test_a_still_running_run_is_not_a_completion(self, db):
        _seed_task(db, "t_a")
        _seed_run(db, "t_a", outcome=None, status="running", ended=None)
        assert kb.record_worker_completion_events(db) == []

    def test_an_ended_run_without_an_outcome_is_not_a_completion(self, db):
        _seed_task(db, "t_a")
        _seed_run(db, "t_a", outcome=None, status="done", ended=1_700_000_100)
        assert kb.record_worker_completion_events(db) == []

    def test_an_unrecognised_outcome_is_left_alone(self, db):
        _seed_task(db, "t_a")
        _seed_run(db, "t_a", outcome="teleported")
        assert kb.record_worker_completion_events(db) == []

    def test_every_terminal_outcome_folds(self, db):
        expected = []
        for i, outcome in enumerate(sorted(kb.TERMINAL_RUN_OUTCOMES)):
            _seed_task(db, f"t_{i}")
            expected.append(_seed_run(db, f"t_{i}", outcome=outcome))
        assert sorted(kb.record_worker_completion_events(db)) == sorted(expected)

    def test_folding_is_bounded(self, db):
        _seed_task(db, "t_a")
        runs = [_seed_run(db, "t_a") for _ in range(12)]

        first = kb.record_worker_completion_events(db, limit=5)
        second = kb.record_worker_completion_events(db, limit=5)
        third = kb.record_worker_completion_events(db, limit=5)

        assert len(first) == 5 and len(second) == 5 and len(third) == 2
        assert sorted(first + second + third) == sorted(runs), "nothing may be lost"


# ---------------------------------------------------------------------------
# consumer cursor + atomic claim
# ---------------------------------------------------------------------------


class TestBrokerCursor:
    def test_cursor_starts_at_zero_and_is_created_on_demand(self, db):
        assert kb.broker_cursor(db, consumer="loop") == 0
        assert kb.ensure_broker_sub(db, consumer="loop") == 0
        row = db.execute(
            "SELECT * FROM kanban_broker_subs WHERE consumer='loop'"
        ).fetchone()
        assert row is not None

    def test_an_unregistered_consumer_claims_nothing(self, db):
        _seed_task(db, "t_a")
        _seed_run(db, "t_a")
        kb.record_worker_completion_events(db)
        assert kb.claim_unseen_events_for_broker(db, consumer="ghost") == (0, 0, [])

    def test_a_claim_advances_the_cursor_atomically(self, db):
        kb.ensure_broker_sub(db, consumer="loop")
        _seed_task(db, "t_a")
        _seed_run(db, "t_a")
        kb.record_worker_completion_events(db)

        old, new, events = kb.claim_unseen_events_for_broker(db, consumer="loop")
        assert old == 0 and new > 0 and len(events) == 1
        assert kb.broker_cursor(db, consumer="loop") == new

        again = kb.claim_unseen_events_for_broker(db, consumer="loop")
        assert again[2] == [], "an advanced cursor must not re-deliver"

    def test_the_cursor_is_board_wide_not_per_task(self, db):
        kb.ensure_broker_sub(db, consumer="loop")
        for i in range(3):
            _seed_task(db, f"t_{i}")
            _seed_run(db, f"t_{i}")
        kb.record_worker_completion_events(db)

        _old, _new, events = kb.claim_unseen_events_for_broker(
            db, consumer="loop", kinds=[kb.BROKER_EVENT_WORKER_COMPLETION]
        )
        assert {e.task_id for e in events} == {"t_0", "t_1", "t_2"}

    def test_kind_filtering(self, db):
        kb.ensure_broker_sub(db, consumer="loop")
        _seed_task(db, "t_a")
        run_id = _seed_run(db, "t_a")
        with kb.write_txn(db):
            db.execute(
                "INSERT INTO task_events (task_id, run_id, kind, payload, created_at)"
                " VALUES (?,?,?,?,?)",
                ("t_a", run_id, "commented", None, 1),
            )
        kb.record_worker_completion_events(db)

        _o, _n, events = kb.claim_unseen_events_for_broker(
            db, consumer="loop", kinds=[kb.BROKER_EVENT_WORKER_COMPLETION]
        )
        assert [e.kind for e in events] == [kb.BROKER_EVENT_WORKER_COMPLETION]

    def test_fetch_and_drain_are_bounded(self, db):
        kb.ensure_broker_sub(db, consumer="loop")
        _seed_task(db, "t_a")
        for _ in range(9):
            _seed_run(db, "t_a")
        kb.record_worker_completion_events(db)

        seen = []
        for _ in range(4):
            _o, _n, events = kb.claim_unseen_events_for_broker(db, consumer="loop", limit=4)
            seen.extend(e.id for e in events)
        assert len(seen) == 9
        assert seen == sorted(seen), "delivery must stay in event order"
        assert len(set(seen)) == 9, "bounded passes must not duplicate"

    def test_concurrent_consumers_split_the_stream_without_overlap(self, tmp_path):
        path = _fresh_db(tmp_path / "board" / "kanban.db")
        setup = kb.connect(path)
        kb.ensure_broker_sub(setup, consumer="loop")
        _seed_task(setup, "t_a")
        for _ in range(20):
            _seed_run(setup, "t_a")
        kb.record_worker_completion_events(setup, limit=100)
        setup.close()

        claimed: list[int] = []
        lock = threading.Lock()
        barrier = threading.Barrier(5)

        def worker() -> None:
            conn = kb.connect(path)
            try:
                barrier.wait()
                got: list[int] = []
                for _ in range(6):
                    _o, _n, events = kb.claim_unseen_events_for_broker(
                        conn, consumer="loop", limit=4
                    )
                    got.extend(e.id for e in events)
            finally:
                conn.close()
            with lock:
                claimed.extend(got)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(claimed) == len(set(claimed)), "an event may be claimed once only"
        assert len(claimed) == 20


class TestRestartAndCasRetry:
    def test_a_failed_pass_rewinds_and_redelivers(self, db):
        kb.ensure_broker_sub(db, consumer="loop")
        _seed_task(db, "t_a")
        _seed_run(db, "t_a")
        kb.record_worker_completion_events(db)

        old, new, events = kb.claim_unseen_events_for_broker(db, consumer="loop")
        assert len(events) == 1

        assert kb.rewind_broker_cursor(
            db, consumer="loop", claimed_cursor=new, old_cursor=old
        )
        assert kb.broker_cursor(db, consumer="loop") == old

        _o2, _n2, redelivered = kb.claim_unseen_events_for_broker(db, consumer="loop")
        assert [e.id for e in redelivered] == [e.id for e in events]

    def test_a_rewind_is_cas_guarded_against_newer_progress(self, db):
        kb.ensure_broker_sub(db, consumer="loop")
        _seed_task(db, "t_a")
        _seed_run(db, "t_a")
        kb.record_worker_completion_events(db)

        old, new, _events = kb.claim_unseen_events_for_broker(db, consumer="loop")
        kb.advance_broker_cursor(db, consumer="loop", new_cursor=new + 500)

        assert not kb.rewind_broker_cursor(
            db, consumer="loop", claimed_cursor=new, old_cursor=old
        ), "a stale rewind must not clobber newer progress"
        assert kb.broker_cursor(db, consumer="loop") == new + 500

    def test_the_cursor_survives_a_reconnect(self, tmp_path):
        path = _fresh_db(tmp_path / "board" / "kanban.db")
        first = kb.connect(path)
        kb.ensure_broker_sub(first, consumer="loop")
        _seed_task(first, "t_a")
        _seed_run(first, "t_a")
        kb.record_worker_completion_events(first)
        _o, new, events = kb.claim_unseen_events_for_broker(first, consumer="loop")
        assert len(events) == 1
        first.close()

        second = kb.connect(path)
        try:
            assert kb.broker_cursor(second, consumer="loop") == new
            assert kb.claim_unseen_events_for_broker(second, consumer="loop")[2] == []
        finally:
            second.close()

    def test_folding_after_a_restart_does_not_duplicate(self, tmp_path):
        path = _fresh_db(tmp_path / "board" / "kanban.db")
        first = kb.connect(path)
        _seed_task(first, "t_a")
        _seed_run(first, "t_a")
        assert len(kb.record_worker_completion_events(first)) == 1
        first.close()

        second = kb.connect(path)
        try:
            assert kb.record_worker_completion_events(second) == []
            n = second.execute(
                "SELECT COUNT(*) c FROM task_events WHERE kind = ?",
                (kb.BROKER_EVENT_WORKER_COMPLETION,),
            ).fetchone()["c"]
        finally:
            second.close()
        assert n == 1


# ---------------------------------------------------------------------------
# cross-board isolation
# ---------------------------------------------------------------------------


class TestCrossBoardIsolation:
    def test_cursors_and_events_do_not_leak_between_boards(self, tmp_path):
        path_a = _fresh_db(tmp_path / "board-a" / "kanban.db")
        path_b = _fresh_db(tmp_path / "board-b" / "kanban.db")
        conn_a, conn_b = kb.connect(path_a), kb.connect(path_b)
        try:
            for conn, tid in ((conn_a, "t_a"), (conn_b, "t_b")):
                kb.ensure_broker_sub(conn, consumer="loop")
                _seed_task(conn, tid)
                _seed_run(conn, tid)
                kb.record_worker_completion_events(conn)

            _o, _n, events_a = kb.claim_unseen_events_for_broker(conn_a, consumer="loop")
            assert [e.task_id for e in events_a] == ["t_a"]

            # Board A's cursor advanced; board B's must be untouched.
            assert kb.broker_cursor(conn_a, consumer="loop") > 0
            assert kb.broker_cursor(conn_b, consumer="loop") == 0

            _o2, _n2, events_b = kb.claim_unseen_events_for_broker(conn_b, consumer="loop")
            assert [e.task_id for e in events_b] == ["t_b"]
        finally:
            conn_a.close()
            conn_b.close()

    def test_folding_on_one_board_leaves_the_other_untouched(self, tmp_path):
        path_a = _fresh_db(tmp_path / "board-a" / "kanban.db")
        path_b = _fresh_db(tmp_path / "board-b" / "kanban.db")
        conn_a, conn_b = kb.connect(path_a), kb.connect(path_b)
        try:
            _seed_task(conn_a, "t_a")
            _seed_run(conn_a, "t_a")
            _seed_task(conn_b, "t_b")
            _seed_run(conn_b, "t_b")

            kb.record_worker_completion_events(conn_a)
            assert (
                conn_b.execute(
                    "SELECT COUNT(*) c FROM task_events WHERE kind = ?",
                    (kb.BROKER_EVENT_WORKER_COMPLETION,),
                ).fetchone()["c"]
                == 0
            )
        finally:
            conn_a.close()
            conn_b.close()


# ---------------------------------------------------------------------------
# pure route decisions
# ---------------------------------------------------------------------------


class TestRouteDecisions:
    def _completion(
        self, outcome="crashed", run_id=1, task_id="t_a", worker_session_id="wsess-1",
        profile="worker",
    ):
        return {
            "run_id": run_id,
            "task_id": task_id,
            "outcome": outcome,
            "run_status": "done",
            "worker_session_id": worker_session_id,
            "profile": profile,
            "worker_session_source": kb.SESSION_SOURCE_DISPATCHER,
        }

    def test_a_resolvable_retryable_failure_continues(self, db):
        _seed_task(db, "t_a", provider="anthropic", failures=1)
        decision = kb.decide_route(
            completion=self._completion("crashed"), task_row=_task_row(db, "t_a")
        )
        assert decision.route == kb.ROUTE_CONTINUE
        assert decision.session_id == "wsess-1" and decision.provider == "anthropic"
        assert decision.spawn is False

    def test_the_resume_target_is_the_worker_session_not_the_originating_one(self, db):
        """tasks.session_id created the task; resuming it drives the wrong session."""
        _seed_task(db, "t_a", session_id="originating-sess", provider="anthropic")
        decision = kb.decide_route(
            completion=self._completion("crashed", worker_session_id="wsess-9"),
            task_row=_task_row(db, "t_a"),
        )
        assert decision.route == kb.ROUTE_CONTINUE
        assert decision.session_id == "wsess-9"
        assert decision.session_id != "originating-sess"

    def test_a_missing_worker_session_reviews_never_spawns(self, db):
        _seed_task(db, "t_a", session_id="originating-sess", provider="anthropic")
        decision = kb.decide_route(
            completion=self._completion("crashed", worker_session_id=None),
            task_row=_task_row(db, "t_a"),
        )
        assert decision.route == kb.ROUTE_REVIEW
        assert decision.reason == "missing_worker_session"
        assert decision.spawn is False

    def test_an_originating_session_cannot_stand_in_for_a_worker_session(self, db):
        """The failure mode this guards: 'there is a session_id, use it'."""
        _seed_task(db, "t_a", session_id="originating-sess", provider="anthropic")
        decision = kb.decide_route(
            completion=self._completion("crashed", worker_session_id=None),
            task_row=_task_row(db, "t_a"),
        )
        assert decision.session_id is None
        assert decision.route == kb.ROUTE_REVIEW

    def test_an_ambiguous_provider_mapping_reviews_never_spawns(self, db):
        _seed_task(db, "t_a", provider=None)
        decision = kb.decide_route(
            completion=self._completion("crashed"), task_row=_task_row(db, "t_a")
        )
        assert decision.route == kb.ROUTE_REVIEW
        assert decision.reason == "ambiguous_provider_mapping"
        assert decision.spawn is False

    def test_a_provider_resolver_closes_the_profile_default_gap(self, db):
        """Tasks on a profile default have no provider_override."""
        _seed_task(db, "t_a", provider=None)
        decision = kb.decide_route(
            completion=self._completion("crashed"),
            task_row=_task_row(db, "t_a"),
            provider_resolver=lambda profile: "anthropic" if profile == "worker" else None,
        )
        assert decision.route == kb.ROUTE_CONTINUE
        assert decision.provider == "anthropic"

    def test_an_explicit_override_beats_the_resolver(self, db):
        _seed_task(db, "t_a", provider="explicit")
        decision = kb.decide_route(
            completion=self._completion("crashed"),
            task_row=_task_row(db, "t_a"),
            provider_resolver=lambda profile: "from-resolver",
        )
        assert decision.provider == "explicit"

    def test_a_resolver_returning_nothing_stays_ambiguous(self, db):
        _seed_task(db, "t_a", provider=None)
        decision = kb.decide_route(
            completion=self._completion("crashed"),
            task_row=_task_row(db, "t_a"),
            provider_resolver=lambda profile: None,
        )
        assert decision.route == kb.ROUTE_REVIEW
        assert decision.reason == "ambiguous_provider_mapping"

    def test_a_raising_resolver_is_ambiguous_not_a_crash(self, db):
        _seed_task(db, "t_a", provider=None)

        def boom(profile):
            raise RuntimeError("config unavailable")

        decision = kb.decide_route(
            completion=self._completion("crashed"),
            task_row=_task_row(db, "t_a"),
            provider_resolver=boom,
        )
        assert decision.route == kb.ROUTE_REVIEW
        assert decision.reason == "ambiguous_provider_mapping"

    def test_a_blank_worker_session_counts_as_missing(self, db):
        _seed_task(db, "t_a", provider="anthropic")
        decision = kb.decide_route(
            completion=self._completion("crashed", worker_session_id="   "),
            task_row=_task_row(db, "t_a"),
        )
        assert decision.route == kb.ROUTE_REVIEW
        assert decision.reason == "missing_worker_session"

    def test_a_missing_task_reviews(self):
        decision = kb.decide_route(completion=self._completion(), task_row=None)
        assert decision.route == kb.ROUTE_REVIEW
        assert decision.reason == "task_not_found"

    def test_completed_work_awaits_a_verdict_rather_than_closing(self, db):
        _seed_task(db, "t_a", status="running", session_id="s", provider="p")
        decision = kb.decide_route(
            completion=self._completion("completed"), task_row=_task_row(db, "t_a")
        )
        assert decision.route == kb.ROUTE_REVIEW
        assert decision.reason == "completed_awaiting_verdict"

    def test_a_finished_card_closes(self, db):
        for status in ("done", "archived"):
            _seed_task(db, f"t_{status}", status=status)
            decision = kb.decide_route(
                completion=self._completion("completed", task_id=f"t_{status}"),
                task_row=_task_row(db, f"t_{status}"),
            )
            assert decision.route == kb.ROUTE_CLOSE

    def test_a_blocked_card_blocks(self, db):
        _seed_task(db, "t_a", status="blocked")
        decision = kb.decide_route(
            completion=self._completion("crashed"), task_row=_task_row(db, "t_a")
        )
        assert decision.route == kb.ROUTE_BLOCK

    def test_a_blocked_or_gave_up_run_blocks(self, db):
        for outcome in ("blocked", "gave_up"):
            _seed_task(db, f"t_{outcome}", session_id="s", provider="p")
            decision = kb.decide_route(
                completion=self._completion(outcome, task_id=f"t_{outcome}"),
                task_row=_task_row(db, f"t_{outcome}"),
            )
            assert decision.route == kb.ROUTE_BLOCK

    def test_the_failure_limit_blocks_before_any_session_check(self, db):
        _seed_task(db, "t_a", session_id="s", provider="p", failures=3)
        decision = kb.decide_route(
            completion=self._completion("crashed"), task_row=_task_row(db, "t_a")
        )
        assert decision.route == kb.ROUTE_BLOCK
        assert "failure_limit_reached" in decision.reason

    def test_a_per_task_retry_override_is_honoured(self, db):
        _seed_task(db, "t_a", session_id="s", provider="p", failures=1, max_retries=1)
        decision = kb.decide_route(
            completion=self._completion("crashed"), task_row=_task_row(db, "t_a")
        )
        assert decision.route == kb.ROUTE_BLOCK

    def test_no_decision_can_ever_spawn(self, db):
        _seed_task(db, "t_a", session_id="s", provider="p")
        row = _task_row(db, "t_a")
        for outcome in sorted(kb.TERMINAL_RUN_OUTCOMES):
            decision = kb.decide_route(
                completion=self._completion(outcome), task_row=row
            )
            assert decision.spawn is False
            assert decision.route in kb.VALID_ROUTES

    def test_decide_route_is_pure_and_writes_nothing(self, db):
        _seed_task(db, "t_a", session_id="s", provider="p")
        row = _task_row(db, "t_a")
        before = db.execute("SELECT COUNT(*) c FROM task_events").fetchone()["c"]
        for _ in range(5):
            kb.decide_route(completion=self._completion(), task_row=row)
        after = db.execute("SELECT COUNT(*) c FROM task_events").fetchone()["c"]
        assert before == after

    def test_recording_a_decision_is_the_only_writing_step(self, db):
        _seed_task(db, "t_a", session_id="s", provider="p")
        run_id = _seed_run(db, "t_a", outcome="crashed")
        kb.record_worker_completion_events(db)
        payload = _completion_payload(db, run_id)
        decision = kb.decide_route(completion=payload, task_row=_task_row(db, "t_a"))

        assert kb.record_route_decision_event(db, decision)
        row = db.execute(
            "SELECT payload FROM task_events WHERE kind = ?",
            (kb.BROKER_EVENT_ROUTE_DECIDED,),
        ).fetchone()
        stored = json.loads(row["payload"])
        assert stored["route"] == decision.route
        assert stored["spawn"] is False


# ---------------------------------------------------------------------------
# worker-session linkage + notification consumption
# ---------------------------------------------------------------------------


class TestWorkerSessionLinkage:
    def test_the_column_exists_on_task_runs(self, db):
        cols = {r["name"] for r in db.execute("PRAGMA table_info(task_runs)")}
        assert "worker_session_id" in cols

    def test_the_producer_api_records_a_worker_session(self, db):
        _seed_task(db, "t_a")
        run_id = _seed_run(db, "t_a", worker_session_id=None)
        assert kb.set_run_worker_session(db, run_id=run_id, worker_session_id="wsess-7")
        row = db.execute(
            "SELECT worker_session_id FROM task_runs WHERE id = ?", (run_id,)
        ).fetchone()
        assert row["worker_session_id"] == "wsess-7"

    def test_recording_an_unknown_run_reports_false(self, db):
        assert not kb.set_run_worker_session(db, run_id=999999, worker_session_id="x")

    def test_an_empty_worker_session_is_rejected(self, db):
        with pytest.raises(ValueError):
            kb.set_run_worker_session(db, run_id=1, worker_session_id="")

    def test_the_session_flows_from_the_run_into_the_completion_event(self, db):
        _seed_task(db, "t_a")
        run_id = _seed_run(db, "t_a", outcome="crashed", worker_session_id="wsess-3")
        kb.record_worker_completion_events(db)
        payload = _completion_payload(db, run_id)
        assert payload["worker_session_id"] == "wsess-3"

    def test_end_to_end_a_recorded_session_makes_continue_reachable(self, db):
        _seed_task(db, "t_a", provider="anthropic")
        run_id = _seed_run(db, "t_a", outcome="crashed", worker_session_id=None)
        kb.record_worker_session_provenance(
            db, run_id=run_id, worker_session_id="wsess-11",
            source=kb.SESSION_SOURCE_DISPATCHER,
        )
        kb.record_worker_completion_events(db)

        payload = _completion_payload(db, run_id)
        decision = kb.decide_route(completion=payload, task_row=_task_row(db, "t_a"))
        assert decision.route == kb.ROUTE_CONTINUE
        assert decision.session_id == "wsess-11"
        assert decision.spawn is False

    def test_without_the_producer_change_continue_is_unreachable(self, db):
        """Honest default: nothing populates the column yet, so we fail closed."""
        _seed_task(db, "t_a", session_id="originating", provider="anthropic")
        run_id = _seed_run(db, "t_a", outcome="crashed")
        kb.record_worker_completion_events(db)
        decision = kb.decide_route(
            completion=_completion_payload(db, run_id), task_row=_task_row(db, "t_a")
        )
        assert decision.route == kb.ROUTE_REVIEW
        assert decision.reason == "missing_worker_session"


class TestNotifications:
    def test_a_notification_is_one_concise_line(self, db):
        _seed_task(db, "t_a", provider="p")
        decision = kb.decide_route(
            completion={
                "run_id": 1, "task_id": "t_a", "outcome": "crashed",
                "run_status": "done", "worker_session_id": "wsess-1",
                "worker_session_source": kb.SESSION_SOURCE_DISPATCHER,
            },
            task_row=_task_row(db, "t_a"),
        )
        line = kb.render_route_notification(decision)
        assert len(line.splitlines()) == 1
        assert line.startswith("CONTINUE ")
        assert "task=t_a" in line and "session=wsess-1" in line and "spawn=false" in line

    def test_a_non_continue_line_names_no_session(self, db):
        _seed_task(db, "t_a", status="done")
        decision = kb.decide_route(
            completion={
                "run_id": 1, "task_id": "t_a", "outcome": "completed",
                "run_status": "done",
            },
            task_row=_task_row(db, "t_a"),
        )
        line = kb.render_route_notification(decision)
        assert line.startswith("CLOSE ") and "session=" not in line

    def test_the_summary_counts_each_route(self, db):
        _seed_task(db, "t_a", provider="p")
        row = _task_row(db, "t_a")
        decisions = [
            kb.decide_route(
                completion={
                    "run_id": i, "task_id": "t_a", "outcome": outcome,
                    "run_status": "done", "worker_session_id": "w",
                    "worker_session_source": kb.SESSION_SOURCE_DISPATCHER,
                },
                task_row=row,
            )
            for i, outcome in enumerate(("crashed", "blocked", "completed"), start=1)
        ]
        summary = kb.render_route_summary(decisions)
        assert "3 completion(s)" in summary
        assert "continue=1" in summary and "block=1" in summary and "review=1" in summary
        assert "inert" in summary

    def test_an_empty_pass_says_so(self):
        assert kb.render_route_summary([]) == "hermes control loop: no new completions"

    def test_decisions_are_drained_exactly_once_as_notifications(self, db):
        kb.ensure_broker_sub(db, consumer="notifier")
        _seed_task(db, "t_a", provider="p")
        run_id = _seed_run(
            db, "t_a", outcome="crashed", worker_session_id="wsess-1",
            worker_session_source=kb.SESSION_SOURCE_DISPATCHER,
        )
        kb.record_worker_completion_events(db)
        decision = kb.decide_route(
            completion=_completion_payload(db, run_id), task_row=_task_row(db, "t_a")
        )
        kb.record_route_decision_event(db, decision)

        first = kb.drain_route_notifications(db, consumer="notifier").lines
        assert len(first) == 1 and first[0].startswith("CONTINUE ")
        assert kb.drain_route_notifications(db, consumer="notifier").lines == ()

    def test_draining_ignores_completion_events(self, db):
        kb.ensure_broker_sub(db, consumer="notifier")
        _seed_task(db, "t_a")
        _seed_run(db, "t_a")
        kb.record_worker_completion_events(db)
        assert kb.drain_route_notifications(db, consumer="notifier").lines == ()

    def test_draining_is_bounded(self, db):
        kb.ensure_broker_sub(db, consumer="notifier")
        _seed_task(db, "t_a", provider="p")
        for i in range(7):
            run_id = _seed_run(db, "t_a", outcome="crashed", worker_session_id="w")
            kb.record_worker_completion_events(db)
            kb.record_route_decision_event(
                db,
                kb.decide_route(
                    completion=_completion_payload(db, run_id),
                    task_row=_task_row(db, "t_a"),
                ),
            )
        seen = []
        for _ in range(4):
            seen.extend(kb.drain_route_notifications(db, consumer="notifier", limit=3).lines)
        assert len(seen) == 7

    def test_a_malformed_decision_row_is_reported_not_interpreted(self, db):
        kb.ensure_broker_sub(db, consumer="notifier")
        _seed_task(db, "t_a")
        with kb.write_txn(db):
            db.execute(
                "INSERT INTO task_events (task_id, run_id, kind, payload, created_at)"
                " VALUES (?,?,?,?,?)",
                ("t_a", 1, kb.BROKER_EVENT_ROUTE_DECIDED, json.dumps({"bogus": 1}), 1),
            )
        lines = kb.drain_route_notifications(db, consumer="notifier").lines
        assert len(lines) == 1 and lines[0].startswith("MALFORMED ")

    def test_notifications_never_announce_a_spawn(self, db):
        _seed_task(db, "t_a", provider="p")
        row = _task_row(db, "t_a")
        for outcome in sorted(kb.TERMINAL_RUN_OUTCOMES):
            decision = kb.decide_route(
                completion={
                    "run_id": 1, "task_id": "t_a", "outcome": outcome,
                    "run_status": "done", "worker_session_id": "w",
                    "worker_session_source": kb.SESSION_SOURCE_DISPATCHER,
                },
                task_row=row,
            )
            assert "spawn=false" in kb.render_route_notification(decision)


# ---------------------------------------------------------------------------
# independent-review repairs (BLOCK items 1-7)
# ---------------------------------------------------------------------------


class TestReviewItem1MigrationGuard:
    """A missing ``task_runs`` must not prevent connect."""

    def test_migration_tolerates_a_missing_task_runs_table(self, tmp_path):
        """Isolates task_runs specifically: everything else present, it absent."""
        path = _fresh_db(tmp_path / "board" / "kanban.db")
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute("DROP TABLE task_runs")
        conn.commit()
        try:
            kb._migrate_add_optional_columns(conn)  # must not raise
        finally:
            conn.close()

    def test_a_board_missing_task_runs_still_connects(self, tmp_path):
        """The review's actual requirement: it must not prevent connect."""
        path = _fresh_db(tmp_path / "board" / "kanban.db")
        raw = sqlite3.connect(path)
        raw.execute("DROP TABLE task_runs")
        raw.commit()
        raw.close()

        # The requirement: opening must not raise.
        conn = kb.connect(path)
        conn.close()

        # And an explicit re-init (which bypasses the initialised-path cache)
        # recreates the table with the migrated column.
        kb.init_db(path)
        conn = kb.connect(path)
        try:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(task_runs)")}
            assert "worker_session_id" in cols, "recreated and migrated"
        finally:
            conn.close()

    def test_migration_is_idempotent_on_a_full_db(self, db):
        kb._migrate_add_optional_columns(db)
        kb._migrate_add_optional_columns(db)
        cols = {r["name"] for r in db.execute("PRAGMA table_info(task_runs)")}
        assert "worker_session_id" in cols


class TestReviewItem2DuplicateSafeIndex:
    """A board with legacy duplicate completion rows must still open."""

    def _legacy_board_with_duplicates(self, path: Path, dupes: int = 3) -> Path:
        """Build a DB carrying duplicate completion rows *before* the index."""
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " task_id TEXT NOT NULL, run_id INTEGER, kind TEXT NOT NULL,"
            " payload TEXT, created_at INTEGER NOT NULL)"
        )
        for i in range(dupes):
            conn.execute(
                "INSERT INTO task_events (task_id, run_id, kind, payload, created_at)"
                " VALUES (?,?,?,?,?)",
                ("t_a", 42, kb.BROKER_EVENT_WORKER_COMPLETION, f'{{"n":{i}}}', 100 + i),
            )
        conn.commit()
        conn.close()
        return path

    def test_a_board_with_duplicates_still_opens(self, tmp_path):
        """Bricking a board is worse than a missing index."""
        path = self._legacy_board_with_duplicates(tmp_path / "legacy.db")
        conn = kb.connect(path)  # must not raise
        try:
            assert conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] == 3
        finally:
            conn.close()

    def test_duplicates_are_quarantined_deterministically_not_deleted(self, tmp_path):
        path = self._legacy_board_with_duplicates(tmp_path / "legacy.db")
        conn = kb.connect(path)
        try:
            kept = conn.execute(
                "SELECT id, payload FROM task_events WHERE kind = ?",
                (kb.BROKER_EVENT_WORKER_COMPLETION,),
            ).fetchall()
            quarantined = conn.execute(
                "SELECT id FROM task_events WHERE kind = ?",
                (kb.BROKER_EVENT_WORKER_COMPLETION_DUPLICATE,),
            ).fetchall()
            total = conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]
        finally:
            conn.close()

        assert len(kept) == 1, "exactly one canonical completion survives per run"
        assert kept[0]["id"] == 1, "deterministic: the earliest observation is kept"
        assert len(quarantined) == 2
        assert total == 3, "nothing may be deleted"

    def test_the_index_exists_after_the_repair(self, tmp_path):
        path = self._legacy_board_with_duplicates(tmp_path / "legacy.db")
        conn = kb.connect(path)
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name='idx_events_completion_once'"
            ).fetchone()
            assert row is not None
            # And it is live: a fresh duplicate is now refused.
            with pytest.raises(sqlite3.IntegrityError):
                with kb.write_txn(conn):
                    conn.execute(
                        "INSERT INTO task_events (task_id, run_id, kind, payload,"
                        " created_at) VALUES (?,?,?,?,?)",
                        ("t_a", 42, kb.BROKER_EVENT_WORKER_COMPLETION, "{}", 1),
                    )
        finally:
            conn.close()

    def test_the_repair_is_idempotent(self, tmp_path):
        path = self._legacy_board_with_duplicates(tmp_path / "legacy.db")
        conn = kb.connect(path)
        try:
            assert kb.quarantine_duplicate_completion_events(conn) == 0
        finally:
            conn.close()

    def test_repairing_many_runs_keeps_one_each(self, tmp_path):
        path = tmp_path / "legacy-multi.db"
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " task_id TEXT NOT NULL, run_id INTEGER, kind TEXT NOT NULL,"
            " payload TEXT, created_at INTEGER NOT NULL)"
        )
        for run in (1, 2, 3):
            for _ in range(4):
                conn.execute(
                    "INSERT INTO task_events (task_id, run_id, kind, payload, created_at)"
                    " VALUES (?,?,?,?,?)",
                    (f"t_{run}", run, kb.BROKER_EVENT_WORKER_COMPLETION, None, 1),
                )
        conn.commit()
        conn.close()

        live = kb.connect(path)
        try:
            kept = live.execute(
                "SELECT run_id, COUNT(*) c FROM task_events WHERE kind = ?"
                " GROUP BY run_id",
                (kb.BROKER_EVENT_WORKER_COMPLETION,),
            ).fetchall()
            total = live.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]
        finally:
            live.close()
        assert {r["run_id"]: r["c"] for r in kept} == {1: 1, 2: 1, 3: 1}
        assert total == 12, "all 12 rows preserved, 9 quarantined"


class TestReviewItem3CursorCasAndMonotonicity:
    def test_a_lost_cas_yields_no_events(self, tmp_path, monkeypatch):
        """Claiming without advancing would hand one range to two owners."""
        path = _fresh_db(tmp_path / "board" / "kanban.db")
        conn = kb.connect(path)
        try:
            kb.ensure_broker_sub(conn, consumer="loop")
            _seed_task(conn, "t_a")
            _seed_run(conn, "t_a")
            kb.record_worker_completion_events(conn)

            real_execute = conn.execute

            def sabotage(sql, *args, **kwargs):
                # Simulate a peer advancing the cursor between read and CAS.
                if sql.startswith("UPDATE kanban_broker_subs SET last_event_id"):
                    class _Zero:
                        rowcount = 0
                    return _Zero()
                return real_execute(sql, *args, **kwargs)

            monkeypatch.setattr(conn, "execute", sabotage)
            old, new, events = kb.claim_unseen_events_for_broker(conn, consumer="loop")
            assert events == [], "a lost CAS must not deliver events"
            assert old == new
        finally:
            conn.close()

    def test_advance_is_monotonic_and_refuses_to_rewind(self, db):
        kb.ensure_broker_sub(db, consumer="loop")
        assert kb.advance_broker_cursor(db, consumer="loop", new_cursor=50)
        assert kb.broker_cursor(db, consumer="loop") == 50

        assert not kb.advance_broker_cursor(db, consumer="loop", new_cursor=10)
        assert kb.broker_cursor(db, consumer="loop") == 50, "advance must never rewind"

        assert kb.advance_broker_cursor(db, consumer="loop", new_cursor=60)
        assert kb.broker_cursor(db, consumer="loop") == 60

    def test_a_deliberate_rewind_still_works_and_is_cas_guarded(self, db):
        kb.ensure_broker_sub(db, consumer="loop")
        kb.advance_broker_cursor(db, consumer="loop", new_cursor=50)
        assert kb.rewind_broker_cursor(
            db, consumer="loop", claimed_cursor=50, old_cursor=10
        )
        assert kb.broker_cursor(db, consumer="loop") == 10


class TestReviewItem4DeliverySemantics:
    def _seed_one_decision(self, conn) -> None:
        kb.ensure_broker_sub(conn, consumer="notifier")
        _seed_task(conn, "t_a", provider="p")
        run_id = _seed_run(
            conn, "t_a", outcome="crashed", worker_session_id="w",
            worker_session_source=kb.SESSION_SOURCE_DISPATCHER,
        )
        kb.record_worker_completion_events(conn)
        kb.record_route_decision_event(
            conn,
            kb.decide_route(
                completion=_completion_payload(conn, run_id),
                task_row=_task_row(conn, "t_a"),
            ),
        )

    def test_a_failed_delivery_rewinds_and_redelivers(self, db):
        """At-least-once: a duplicate beats a silently lost notification."""
        self._seed_one_decision(db)

        def failing(lines):
            raise RuntimeError("gateway down")

        with pytest.raises(RuntimeError):
            kb.drain_route_notifications(db, consumer="notifier", deliver=failing)

        delivered = []
        result = kb.drain_route_notifications(
            db, consumer="notifier", deliver=delivered.extend
        )
        assert len(result.lines) == 1
        assert len(delivered) == 1, "the batch must be redelivered after a failure"

    def test_a_successful_delivery_does_not_redeliver(self, db):
        self._seed_one_decision(db)
        delivered = []
        kb.drain_route_notifications(db, consumer="notifier", deliver=delivered.extend)
        again = kb.drain_route_notifications(db, consumer="notifier", deliver=delivered.extend)
        assert len(delivered) == 1
        assert again.lines == ()

    def test_without_deliver_the_caller_owns_the_claim_window(self, db):
        """Explicitly at-most-once unless the caller rewinds."""
        self._seed_one_decision(db)
        result = kb.drain_route_notifications(db, consumer="notifier")
        assert result.claimed and len(result.lines) == 1

        # Caller's own send failed; it rewinds using the exposed window.
        assert kb.rewind_broker_cursor(
            db,
            consumer="notifier",
            claimed_cursor=result.new_cursor,
            old_cursor=result.old_cursor,
        )
        again = kb.drain_route_notifications(db, consumer="notifier")
        assert len(again.lines) == 1, "rewinding must redeliver"

    def test_an_empty_batch_never_invokes_deliver(self, db):
        kb.ensure_broker_sub(db, consumer="notifier")
        calls = []
        result = kb.drain_route_notifications(
            db, consumer="notifier", deliver=calls.append
        )
        assert result.lines == () and calls == []
        assert not result.claimed


class TestReviewItem5NativeFailureLimit:
    def test_the_default_matches_the_native_constant(self, db):
        """An invented fallback of 3 disagreed with the native default of 2."""
        assert kb.DEFAULT_FAILURE_LIMIT == 2
        assert not hasattr(kb, "DEFAULT_FAILURE_LIMIT_FALLBACK")

        _seed_task(db, "t_a", provider="p", failures=2)
        decision = kb.decide_route(
            completion={
                "run_id": 1, "task_id": "t_a", "outcome": "crashed",
                "run_status": "done", "worker_session_id": "w",
                "worker_session_source": kb.SESSION_SOURCE_DISPATCHER,
            },
            task_row=_task_row(db, "t_a"),
        )
        assert decision.route == kb.ROUTE_BLOCK
        assert "2/2" in decision.reason

    def test_a_caller_supplied_limit_is_honoured(self, db):
        _seed_task(db, "t_a", provider="p", failures=2)
        decision = kb.decide_route(
            completion={
                "run_id": 1, "task_id": "t_a", "outcome": "crashed",
                "run_status": "done", "worker_session_id": "w",
                "worker_session_source": kb.SESSION_SOURCE_DISPATCHER,
            },
            task_row=_task_row(db, "t_a"),
            failure_limit=5,
        )
        assert decision.route == kb.ROUTE_CONTINUE

    def test_per_task_max_retries_beats_the_caller_limit(self, db):
        _seed_task(db, "t_a", provider="p", failures=1, max_retries=1)
        decision = kb.decide_route(
            completion={
                "run_id": 1, "task_id": "t_a", "outcome": "crashed",
                "run_status": "done", "worker_session_id": "w",
                "worker_session_source": kb.SESSION_SOURCE_DISPATCHER,
            },
            task_row=_task_row(db, "t_a"),
            failure_limit=99,
        )
        assert decision.route == kb.ROUTE_BLOCK


class TestReviewItem6NullRunIdSemantics:
    def test_a_null_or_zero_run_id_completion_is_rejected(self):
        for bad in (0, -1):
            with pytest.raises(kb.BrokerEventValidationError, match="positive run id"):
                kb.validate_broker_event_payload(
                    kb.BROKER_EVENT_WORKER_COMPLETION,
                    {"run_id": bad, "task_id": "t_a", "outcome": "completed",
                     "run_status": "done"},
                )

    def test_a_missing_run_id_is_rejected(self):
        with pytest.raises(kb.BrokerEventValidationError, match="missing required field"):
            kb.validate_broker_event_payload(
                kb.BROKER_EVENT_WORKER_COMPLETION,
                {"task_id": "t_a", "outcome": "completed", "run_status": "done"},
            )

    def test_null_run_ids_are_outside_the_index_and_reported_as_malformed(self, db):
        """Explicit semantics: SQLite treats NULLs as distinct in a UNIQUE index."""
        kb.ensure_broker_sub(db, consumer="notifier")
        _seed_task(db, "t_a")
        with kb.write_txn(db):
            for _ in range(2):
                db.execute(
                    "INSERT INTO task_events (task_id, run_id, kind, payload, created_at)"
                    " VALUES (?,NULL,?,?,?)",
                    ("t_a", kb.BROKER_EVENT_WORKER_COMPLETION, "{}", 1),
                )
        # The index does NOT constrain them — this is the documented semantic.
        n = db.execute(
            "SELECT COUNT(*) c FROM task_events WHERE run_id IS NULL AND kind = ?",
            (kb.BROKER_EVENT_WORKER_COMPLETION,),
        ).fetchone()["c"]
        assert n == 2

        # But our own writer can never produce one, and the consumer refuses to
        # interpret a decision row without a valid run id.
        with kb.write_txn(db):
            db.execute(
                "INSERT INTO task_events (task_id, run_id, kind, payload, created_at)"
                " VALUES (?,NULL,?,?,?)",
                ("t_a", kb.BROKER_EVENT_ROUTE_DECIDED, json.dumps({
                    "run_id": 0, "task_id": "t_a", "route": "review",
                    "reason": "x", "outcome": "completed", "spawn": False,
                }), 1),
            )
        lines = kb.drain_route_notifications(db, consumer="notifier").lines
        assert any(line.startswith("MALFORMED") for line in lines)


class TestReviewItem7StatusVocabulary:
    def _completion(self):
        return {
            "run_id": 1, "task_id": "t_a", "outcome": "crashed",
            "run_status": "done", "worker_session_id": "w",
            "worker_session_source": kb.SESSION_SOURCE_DISPATCHER,
        }

    def test_the_router_vocabulary_is_a_subset_of_native_valid_statuses(self):
        assert kb._ROUTABLE_TASK_STATUSES <= kb.VALID_STATUSES
        assert "review-required" not in kb._ROUTABLE_TASK_STATUSES

    def test_every_native_status_routes_without_continuing_unexpectedly(self, db):
        for status in sorted(kb.VALID_STATUSES):
            _seed_task(db, f"t_{status}", status=status, provider="p")
            decision = kb.decide_route(
                completion={**self._completion(), "task_id": f"t_{status}"},
                task_row=_task_row(db, f"t_{status}"),
            )
            assert decision.route in kb.VALID_ROUTES
            assert decision.spawn is False

    def test_non_native_statuses_route_to_review_not_interpreted(self, db):
        for status in ("cancelled", "in_progress", "", "paused"):
            _seed_task(db, "t_x", status="running", provider="p")
            with kb.write_txn(db):
                db.execute("UPDATE tasks SET status = ? WHERE id = 't_x'", (status,))
            decision = kb.decide_route(
                completion={**self._completion(), "task_id": "t_x"},
                task_row=_task_row(db, "t_x"),
            )
            assert decision.route == kb.ROUTE_REVIEW, f"{status!r} must not be interpreted"
            assert decision.reason.startswith("unknown_task_status")
            with kb.write_txn(db):
                db.execute("DELETE FROM tasks WHERE id = 't_x'")

    def test_running_is_the_native_in_flight_status(self):
        assert "running" in kb.VALID_STATUSES
        assert "in_progress" not in kb.VALID_STATUSES
        assert "cancelled" not in kb.VALID_STATUSES


# ---------------------------------------------------------------------------
# re-review repairs (T1 / M1 / M2)
# ---------------------------------------------------------------------------


class TestM1DeliveryDurabilityBaseException:
    """M1 — ``except Exception`` missed the shutdown paths that lose a batch."""

    def _seed_one_decision(self, conn) -> None:
        kb.ensure_broker_sub(conn, consumer="notifier")
        _seed_task(conn, "t_a", provider="p")
        run_id = _seed_run(
            conn, "t_a", outcome="crashed", worker_session_id="w",
            worker_session_source=kb.SESSION_SOURCE_DISPATCHER,
        )
        kb.record_worker_completion_events(conn)
        kb.record_route_decision_event(
            conn,
            kb.decide_route(
                completion=_completion_payload(conn, run_id),
                task_row=_task_row(conn, "t_a"),
            ),
        )

    @pytest.mark.parametrize(
        "exc",
        [KeyboardInterrupt, SystemExit, BaseException],
        ids=["KeyboardInterrupt", "SystemExit", "BaseException"],
    )
    def test_a_base_exception_during_delivery_rewinds_and_redelivers(self, db, exc):
        self._seed_one_decision(db)

        def failing(lines):
            raise exc("shutdown")

        with pytest.raises(exc):
            kb.drain_route_notifications(db, consumer="notifier", deliver=failing)

        delivered: list[str] = []
        result = kb.drain_route_notifications(
            db, consumer="notifier", deliver=delivered.extend
        )
        assert len(result.lines) == 1
        assert len(delivered) == 1, "the batch must survive a BaseException path"

    def test_asyncio_cancellation_rewinds_and_redelivers(self, db):
        """CancelledError is a BaseException since 3.8 — the real shutdown case."""
        import asyncio

        self._seed_one_decision(db)

        def cancelled(lines):
            raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            kb.drain_route_notifications(db, consumer="notifier", deliver=cancelled)

        delivered: list[str] = []
        kb.drain_route_notifications(db, consumer="notifier", deliver=delivered.extend)
        assert len(delivered) == 1

    def test_the_original_exception_type_still_propagates(self, db):
        self._seed_one_decision(db)

        def boom(lines):
            raise KeyboardInterrupt("ctrl-c")

        with pytest.raises(KeyboardInterrupt, match="ctrl-c"):
            kb.drain_route_notifications(db, consumer="notifier", deliver=boom)


#: Multiprocess folder used by the M2 regression. Each child folds the same
#: board concurrently; the parent counts the resulting completion events.
#:
#: ``raw=1`` opens the DB with a bare ``sqlite3`` connection instead of
#: ``kb.connect``. That matters: ``kb.connect`` runs the migration pass and
#: therefore *recreates* a dropped dedup index, self-healing the board. The
#: genuinely-degraded state (a SQLite build that cannot create the partial
#: index) is only reachable by not running that pass.
_FOLD_WORKER = r'''
import sys, pathlib, sqlite3
repo, path, allow, raw = sys.argv[1], sys.argv[2], sys.argv[3] == "1", sys.argv[4] == "1"
sys.path.insert(0, repo)
from hermes_cli import kanban_db as kb
if raw:
    conn = sqlite3.connect(path, timeout=60)
    conn.row_factory = sqlite3.Row
else:
    conn = kb.connect(pathlib.Path(path))
try:
    if allow:
        kb.record_worker_completion_events(conn, limit=500, allow_degraded=True)
    else:
        kb.record_worker_completion_events(conn, limit=500)
except kb.BrokerUnsafeError:
    print("REFUSED")
except sqlite3.OperationalError as exc:
    print("BUSY:" + str(exc))
finally:
    conn.close()
'''


class TestM2DegradedNoIndexSafety:
    """M2 — the no-index fallback must be detectable and gated, not silent."""

    def _board_with_runs(self, tmp_path: Path, runs: int = 30) -> Path:
        path = _fresh_db(tmp_path / "board" / "kanban.db")
        conn = kb.connect(path)
        try:
            _seed_task(conn, "t_a", provider="p")
            for _ in range(runs):
                _seed_run(conn, "t_a", outcome="crashed", worker_session_id="w")
        finally:
            conn.close()
        return path

    def _drop_index(self, path: Path) -> None:
        raw = sqlite3.connect(path)
        raw.execute(f"DROP INDEX IF EXISTS {kb.COMPLETION_DEDUP_INDEX}")
        raw.commit()
        raw.close()

    def test_health_is_queryable_and_reports_healthy_by_default(self, db):
        health = kb.broker_health(db)
        assert health.dedup_index_present
        assert health.healthy and health.safe_to_schedule
        assert health.degraded_reason is None
        assert health.to_json()["safe_to_schedule"] is True

    def test_a_missing_index_is_detectable_not_silent(self, tmp_path):
        path = self._board_with_runs(tmp_path, runs=1)
        self._drop_index(path)
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            assert not kb.completion_dedup_index_present(conn)
            health = kb.broker_health(conn)
            assert not health.dedup_index_present
            assert not health.healthy
            assert not health.safe_to_schedule
            assert health.degraded_reason == "dedup_index_absent"
        finally:
            conn.close()

    def test_a_missing_index_is_a_hard_no_schedule_gate(self, tmp_path):
        path = self._board_with_runs(tmp_path, runs=1)
        self._drop_index(path)
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            with pytest.raises(kb.BrokerUnsafeError, match="refusing to schedule"):
                kb.assert_broker_safe_to_schedule(conn)
        finally:
            conn.close()

    def test_folding_refuses_to_run_without_the_index(self, tmp_path):
        path = self._board_with_runs(tmp_path, runs=1)
        self._drop_index(path)
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            with pytest.raises(kb.BrokerUnsafeError, match="allow_degraded"):
                kb.record_worker_completion_events(conn)
        finally:
            conn.close()

    def test_a_healthy_board_passes_the_gate(self, db):
        assert kb.assert_broker_safe_to_schedule(db).safe_to_schedule

    def test_duplicate_rows_are_reported_by_health(self, tmp_path):
        path = self._board_with_runs(tmp_path, runs=1)
        self._drop_index(path)
        raw = sqlite3.connect(path)
        raw.row_factory = sqlite3.Row
        try:
            for _ in range(3):
                raw.execute(
                    "INSERT INTO task_events (task_id, run_id, kind, payload,"
                    " created_at) VALUES ('t_a', 1, ?, '{}', 1)",
                    (kb.BROKER_EVENT_WORKER_COMPLETION,),
                )
            raw.commit()
            health = kb.broker_health(raw)
            assert health.duplicate_completion_rows == 2
            assert not health.healthy
        finally:
            raw.close()

    def test_multiprocess_contention_with_the_index_is_exactly_once(self, tmp_path):
        """The safe mode, proven across real processes: 30 runs -> 30 events."""
        path = self._board_with_runs(tmp_path, runs=30)
        repo_root = Path(kb.__file__).resolve().parents[1]

        procs = [
            subprocess.Popen(
                [sys.executable, "-c", _FOLD_WORKER, str(repo_root), str(path), "0", "0"],
                stdout=subprocess.PIPE, text=True,
            )
            for _ in range(6)
        ]
        for proc in procs:
            proc.communicate(timeout=180)

        conn = kb.connect(path)
        try:
            n = conn.execute(
                "SELECT COUNT(*) c FROM task_events WHERE kind = ?",
                (kb.BROKER_EVENT_WORKER_COMPLETION,),
            ).fetchone()["c"]
        finally:
            conn.close()
        assert n == 30, f"exactly-once must hold across processes, got {n}"

    def test_multiprocess_contention_without_the_index_is_gated_and_detectable(
        self, tmp_path
    ):
        """The unsafe mode must be refused by default and visible when forced."""
        path = self._board_with_runs(tmp_path, runs=30)
        self._drop_index(path)
        repo_root = Path(kb.__file__).resolve().parents[1]

        # 1. Default: every process refuses rather than degrading silently.
        refused = []
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", _FOLD_WORKER, str(repo_root), str(path), "0", "1"],
                stdout=subprocess.PIPE, text=True,
            )
            for _ in range(6)
        ]
        for proc in procs:
            out, _ = proc.communicate(timeout=180)
            refused.append("REFUSED" in (out or ""))
        assert all(refused), "every process must refuse the unsafe mode by default"

        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            n_before = conn.execute(
                "SELECT COUNT(*) c FROM task_events WHERE kind = ?",
                (kb.BROKER_EVENT_WORKER_COMPLETION,),
            ).fetchone()["c"]
        finally:
            conn.close()
        assert n_before == 0, "a refused fold must write nothing"

        # 2. Forced: the caller opts in explicitly; the result may exceed 30,
        #    and crucially that condition is *detectable* afterwards.
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", _FOLD_WORKER, str(repo_root), str(path), "1", "1"],
                stdout=subprocess.PIPE, text=True,
            )
            for _ in range(6)
        ]
        for proc in procs:
            proc.communicate(timeout=180)

        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            n_after = conn.execute(
                "SELECT COUNT(*) c FROM task_events WHERE kind = ?",
                (kb.BROKER_EVENT_WORKER_COMPLETION,),
            ).fetchone()["c"]
            health = kb.broker_health(conn)
        finally:
            conn.close()

        assert n_after >= 30
        # The degradation is never silent: health reports it, and any duplicate
        # the race produced is counted rather than hidden.
        assert not health.safe_to_schedule
        assert health.degraded_reason == "dedup_index_absent"
        assert health.duplicate_completion_rows == n_after - 30, (
            "every duplicate the unsafe mode produced must be visible in health"
        )

    def test_index_repair_is_cache_dependent_not_automatic(self, tmp_path):
        """How the degraded mode is actually reachable — measured, not assumed.

        The index is created in the migration pass, which ``connect()`` runs
        only on the **first** open of a path per process
        (``_INITIALIZED_PATHS``). So in a long-lived process that has already
        opened the board, a dropped index is **not** repaired by reconnecting —
        the board silently stays unsafe until something clears the cache
        (``init_db``) or the process restarts.

        That is precisely why the unsafe state needs a queryable health gate
        rather than trust in self-healing, and why the multiprocess regressions
        use raw connections.
        """
        path = self._board_with_runs(tmp_path, runs=1)
        self._drop_index(path)

        # Same process, path already initialised: no repair.
        conn = kb.connect(path)
        try:
            assert not kb.completion_dedup_index_present(conn), (
                "reconnecting must NOT be assumed to repair the index"
            )
            assert not kb.broker_health(conn).safe_to_schedule
        finally:
            conn.close()

        # An explicit re-init clears the cache and does repair it.
        kb.init_db(path)
        conn = kb.connect(path)
        try:
            assert kb.completion_dedup_index_present(conn)
            assert kb.broker_health(conn).safe_to_schedule
        finally:
            conn.close()

    def test_the_repair_path_restores_a_schedulable_board(self, tmp_path):
        """After reopening through connect(), the board becomes safe again."""
        path = self._board_with_runs(tmp_path, runs=2)
        self._drop_index(path)
        raw = sqlite3.connect(path)
        for _ in range(2):
            raw.execute(
                "INSERT INTO task_events (task_id, run_id, kind, payload, created_at)"
                " VALUES ('t_a', 1, ?, '{}', 1)",
                (kb.BROKER_EVENT_WORKER_COMPLETION,),
            )
        raw.commit()
        raw.close()

        kb.init_db(path)  # forces the migration/repair pass to re-run
        conn = kb.connect(path)
        try:
            health = kb.broker_health(conn)
            assert health.safe_to_schedule and health.healthy
            assert kb.assert_broker_safe_to_schedule(conn)
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# slice 3 — session reuse, action policy, seats, identity, recovery
# ---------------------------------------------------------------------------


def _completion_for(
    run_id: int,
    outcome: str = "crashed",
    task_id: str = "t_a",
    source: str | None = kb.SESSION_SOURCE_DISPATCHER,
) -> dict:
    return {
        "run_id": run_id,
        "task_id": task_id,
        "outcome": outcome,
        "run_status": "done",
        "worker_session_source": source,
    }


class TestSliceWorkerSessionProvenance:
    """(1) Durable provenance for an existing task/run. No provider spawn."""

    def test_the_provenance_column_exists(self, db):
        cols = {r["name"] for r in db.execute("PRAGMA table_info(task_runs)")}
        assert "worker_session_source" in cols

    def test_a_declared_mapping_is_durable_and_eligible(self, db):
        _seed_task(db, "t_a")
        run_id = _seed_run(db, "t_a", outcome="crashed")
        mapping = kb.record_worker_session_provenance(
            db, run_id=run_id, worker_session_id="wsess-1",
            source=kb.SESSION_SOURCE_DISPATCHER,
        )
        assert mapping.continue_eligible

        row = db.execute(
            "SELECT worker_session_id, worker_session_source FROM task_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        assert row["worker_session_id"] == "wsess-1"
        assert row["worker_session_source"] == kb.SESSION_SOURCE_DISPATCHER

    def test_an_inferred_mapping_is_recorded_but_not_eligible(self, db):
        _seed_task(db, "t_a")
        run_id = _seed_run(db, "t_a")
        mapping = kb.record_worker_session_provenance(
            db, run_id=run_id, worker_session_id="wsess-1",
            source=kb.SESSION_SOURCE_INFERRED,
        )
        assert not mapping.continue_eligible
        row = db.execute(
            "SELECT worker_session_source FROM task_runs WHERE id = ?", (run_id,)
        ).fetchone()
        assert row["worker_session_source"] == kb.SESSION_SOURCE_INFERRED

    def test_mapping_emits_a_typed_event(self, db):
        _seed_task(db, "t_a")
        run_id = _seed_run(db, "t_a")
        kb.record_worker_session_provenance(
            db, run_id=run_id, worker_session_id="wsess-1",
            source=kb.SESSION_SOURCE_OPERATOR,
        )
        row = db.execute(
            "SELECT payload FROM task_events WHERE kind = ?",
            (kb.BROKER_EVENT_SESSION_MAPPED,),
        ).fetchone()
        payload = json.loads(row["payload"])
        assert payload["source"] == kb.SESSION_SOURCE_OPERATOR
        assert payload["continue_eligible"] is True

    def test_an_unknown_source_is_rejected(self, db):
        _seed_task(db, "t_a")
        run_id = _seed_run(db, "t_a")
        with pytest.raises(ValueError, match="unknown worker session source"):
            kb.record_worker_session_provenance(
                db, run_id=run_id, worker_session_id="w", source="vibes",
            )

    def test_an_unknown_run_is_rejected(self, db):
        with pytest.raises(ValueError, match="no such run"):
            kb.record_worker_session_provenance(
                db, run_id=999999, worker_session_id="w",
                source=kb.SESSION_SOURCE_OPERATOR,
            )

    def test_the_event_payload_cannot_lie_about_eligibility(self):
        with pytest.raises(kb.BrokerEventValidationError, match="continue_eligible"):
            kb.validate_broker_event_payload(
                kb.BROKER_EVENT_SESSION_MAPPED,
                {"run_id": 1, "task_id": "t_a", "worker_session_id": "w",
                 "source": kb.SESSION_SOURCE_INFERRED, "continue_eligible": True},
            )

    # --- N1: enforcement must hold on the REAL fold -> claim -> route path ---

    def test_real_flow_an_inferred_source_blocks_continue(self, db):
        _seed_task(db, "t_a", provider="p")
        run_id = _seed_run(db, "t_a", outcome="crashed")
        kb.record_worker_session_provenance(
            db, run_id=run_id, worker_session_id="wsess-1",
            source=kb.SESSION_SOURCE_INFERRED,
        )
        decision = _fold_and_route(db, run_id)
        assert decision.route == kb.ROUTE_REVIEW
        assert decision.reason == "session_source_not_eligible:inferred"

    def test_real_flow_a_declared_source_permits_continue(self, db):
        _seed_task(db, "t_a", provider="p")
        run_id = _seed_run(db, "t_a", outcome="crashed")
        kb.record_worker_session_provenance(
            db, run_id=run_id, worker_session_id="wsess-1",
            source=kb.SESSION_SOURCE_DISPATCHER,
        )
        decision = _fold_and_route(db, run_id)
        assert decision.route == kb.ROUTE_CONTINUE
        assert decision.session_id == "wsess-1"

    def test_real_flow_a_session_without_provenance_fails_closed(self, db):
        """N1 regression: a session id alone must never reach CONTINUE."""
        _seed_task(db, "t_a", provider="p")
        run_id = _seed_run(db, "t_a", outcome="crashed", worker_session_id="wsess-1")
        decision = _fold_and_route(db, run_id)
        assert decision.route == kb.ROUTE_REVIEW
        assert decision.reason == "missing_session_provenance"

    def test_real_flow_an_unknown_source_fails_closed(self, db):
        """A value written directly into the column, bypassing the recorder."""
        _seed_task(db, "t_a", provider="p")
        run_id = _seed_run(
            db, "t_a", outcome="crashed", worker_session_id="wsess-1",
            worker_session_source="vibes",
        )
        decision = _fold_and_route(db, run_id)
        assert decision.route == kb.ROUTE_REVIEW
        assert decision.reason == "unknown_session_provenance:vibes"

    def test_the_completion_payload_carries_provenance(self, db):
        _seed_task(db, "t_a")
        run_id = _seed_run(db, "t_a", outcome="crashed")
        kb.record_worker_session_provenance(
            db, run_id=run_id, worker_session_id="wsess-1",
            source=kb.SESSION_SOURCE_OPERATOR,
        )
        kb.record_worker_completion_events(db)
        payload = _completion_payload(db, run_id)
        assert payload["worker_session_source"] == kb.SESSION_SOURCE_OPERATOR

    def test_decide_route_has_no_provenance_opt_in_kwarg(self, db):
        """The fail-open surface is gone, not merely defaulted."""
        import inspect

        params = inspect.signature(kb.decide_route).parameters
        assert "session_source" not in params


class TestSliceSeatResolution:
    """(3) Provider resolution fails closed without a declared eligible seat."""

    def _registry(self, **over) -> kb.SeatRegistry:
        base = dict(seat_id="seat-a", provider="anthropic",
                    worker_session_id="wsess-1", eligible=True)
        base.update(over)
        return kb.SeatRegistry([kb.Seat(**base)])

    def _provenanced_run(self, db, session="wsess-1") -> int:
        run_id = _seed_run(db, "t_a", outcome="crashed")
        kb.record_worker_session_provenance(
            db, run_id=run_id, worker_session_id=session,
            source=kb.SESSION_SOURCE_DISPATCHER,
        )
        return run_id

    def test_a_declared_eligible_seat_resolves_and_continues(self, db):
        _seed_task(db, "t_a")
        run_id = self._provenanced_run(db)
        decision = _fold_and_route(db, run_id, seats=self._registry())
        assert decision.route == kb.ROUTE_CONTINUE
        assert decision.seat == "seat-a"
        assert decision.provider == "anthropic"

    def test_an_ineligible_seat_fails_closed(self, db):
        _seed_task(db, "t_a")
        run_id = self._provenanced_run(db)
        decision = _fold_and_route(db, run_id, seats=self._registry(eligible=False))
        assert decision.route == kb.ROUTE_REVIEW
        assert decision.reason == "no_declared_eligible_seat"
        assert decision.seat is None

    def test_an_unknown_session_fails_closed(self, db):
        _seed_task(db, "t_a")
        run_id = self._provenanced_run(db, session="somebody-else")
        decision = _fold_and_route(db, run_id, seats=self._registry())
        assert decision.route == kb.ROUTE_REVIEW
        assert decision.reason == "no_declared_eligible_seat"

    def test_an_empty_registry_fails_closed(self, db):
        _seed_task(db, "t_a", provider="p")
        run_id = self._provenanced_run(db)
        decision = _fold_and_route(db, run_id, seats=kb.SeatRegistry([]))
        assert decision.route == kb.ROUTE_REVIEW
        assert decision.reason == "no_declared_eligible_seat"

    def test_a_seat_without_a_provider_is_not_eligible(self):
        registry = kb.SeatRegistry(
            [kb.Seat("seat-a", provider="", worker_session_id="w", eligible=True)]
        )
        assert registry.eligible_for_session("w") is None

    def test_a_seat_must_declare_a_session(self):
        with pytest.raises(ValueError, match="worker_session_id"):
            kb.SeatRegistry([kb.Seat("seat-a", "anthropic", "", eligible=True)])

    def test_no_registry_means_no_seat_reuse_is_claimed(self, db):
        """Backward-compatible: without a registry the prior rules apply."""
        _seed_task(db, "t_a", provider="p")
        run_id = self._provenanced_run(db)
        decision = _fold_and_route(db, run_id)
        assert decision.route == kb.ROUTE_CONTINUE
        assert decision.seat is None


class TestSliceActionPolicy:
    """(2) Only simulated transport observes freely; real action needs A3."""

    def _decision(self, db) -> kb.RouteDecision:
        """A real CONTINUE decision backed by a real provenanced run.

        It must survive `revalidate_decision_provenance` so these tests
        exercise the *policy* gate rather than tripping the provenance gate.
        """
        _seed_task(db, "t_a", provider="p")
        run_id = _seed_run(db, "t_a", outcome="crashed")
        kb.record_worker_session_provenance(
            db, run_id=run_id, worker_session_id="w",
            source=kb.SESSION_SOURCE_DISPATCHER,
        )
        decision = _fold_and_route(db, run_id)
        assert decision.route == kb.ROUTE_CONTINUE
        return decision

    def test_a_registered_simulated_transport_may_observe_without_any_gate(self, db):
        decision = self._decision(db)
        transport = kb.SimulatedTransport()
        policy = kb.ActionPolicy(simulated_transports=[transport])
        assert kb.dispatch_outcome(db, decision, transport=transport, policy=policy)
        assert transport.observed == [decision]

    def test_an_unregistered_transport_is_refused_even_if_it_claims_simulated(self, db):
        """N2: a liar object must not be able to declare itself harmless."""

        class LiarTransport:
            name = "definitely-safe"
            simulated = True  # self-declared; the policy must ignore this

            def __init__(self):
                self.observed = []

            def observe(self, decision):
                self.observed.append(decision)

        decision = self._decision(db)
        liar = LiarTransport()
        with pytest.raises(kb.ActionNotPermittedError, match="not a registered simulated"):
            kb.dispatch_outcome(db, decision, transport=liar)
        assert liar.observed == []

    def test_a_liar_is_still_refused_when_real_action_is_allowed_without_a_gate(self, db):
        class LiarTransport:
            name = "definitely-safe"
            simulated = True

            def observe(self, decision):  # pragma: no cover - must never run
                raise AssertionError("must not be observed")

        decision = self._decision(db)
        with pytest.raises(kb.ActionNotPermittedError, match="requires a positive A3 gate"):
            kb.dispatch_outcome(
                db, decision, transport=LiarTransport(),
                policy=kb.ActionPolicy(allow_real_action=True),
            )

    def test_registration_is_by_identity_not_by_name(self, db):
        """A look-alike with the same name is not the registered object."""
        decision = self._decision(db)
        registered = kb.SimulatedTransport(name="sim")
        impostor = kb.SimulatedTransport(name="sim")
        policy = kb.ActionPolicy(simulated_transports=[registered])

        assert policy.is_registered_simulated(registered)
        assert not policy.is_registered_simulated(impostor)
        with pytest.raises(kb.ActionNotPermittedError):
            kb.dispatch_outcome(db, decision, transport=impostor, policy=policy)

    def test_the_default_policy_registers_nothing(self, db):
        decision = self._decision(db)
        transport = kb.SimulatedTransport()
        with pytest.raises(kb.ActionNotPermittedError):
            kb.dispatch_outcome(db, decision, transport=transport)

    def test_a_real_transport_is_refused_by_the_default_policy(self, db):
        decision = self._decision(db)
        real = kb.SimulatedTransport(name="real-resume")
        with pytest.raises(kb.ActionNotPermittedError, match="does not allow real action"):
            kb.dispatch_outcome(db, decision, transport=real)
        assert real.observed == []

    def test_a_real_transport_without_an_a3_gate_fails_closed(self, db):
        decision = self._decision(db)
        real = kb.SimulatedTransport(name="real-resume")
        policy = kb.ActionPolicy(allow_real_action=True)
        with pytest.raises(kb.ActionNotPermittedError, match="requires a positive A3 gate"):
            kb.dispatch_outcome(db, decision, transport=real, policy=policy)
        assert real.observed == []

    def _comment(self, db, body: str) -> None:
        with kb.write_txn(db):
            db.execute(
                "INSERT INTO task_comments (task_id, author, body, created_at)"
                " VALUES ('t_a','operator',?,?)",
                (body, 1),
            )

    def test_a_granted_a3_gate_permits_a_real_transport(self, db):
        decision = self._decision(db)
        self._comment(db, "A3_GATE=GRANTED for one resume of t_a")
        real = kb.SimulatedTransport(name="real-resume")
        assert kb.dispatch_outcome(
            db, decision, transport=real, policy=kb.ActionPolicy(allow_real_action=True)
        )
        assert real.observed == [decision]

    def test_a_negated_gate_is_not_a_grant(self, db):
        decision = self._decision(db)
        self._comment(db, "No A3_GATE=GRANTED has been issued for this task")
        assert not kb.a3_gate_granted(db, "t_a")
        real = kb.SimulatedTransport(name="real-resume")
        with pytest.raises(kb.ActionNotPermittedError):
            kb.dispatch_outcome(
                db, decision, transport=real,
                policy=kb.ActionPolicy(allow_real_action=True),
            )

    def test_a_later_revocation_beats_an_earlier_grant(self, db):
        decision = self._decision(db)
        self._comment(db, "A3_GATE=GRANTED")
        assert kb.a3_gate_granted(db, "t_a")
        self._comment(db, "A3_GATE=REVOKED — rolling back")
        assert not kb.a3_gate_granted(db, "t_a")
        real = kb.SimulatedTransport(name="real-resume")
        with pytest.raises(kb.ActionNotPermittedError):
            kb.dispatch_outcome(
                db, decision, transport=real,
                policy=kb.ActionPolicy(allow_real_action=True),
            )

    def test_a_grant_after_a_revocation_restores_permission(self, db):
        decision = self._decision(db)
        self._comment(db, "A3_GATE=REVOKED")
        self._comment(db, "A3_GATE=GRANTED again after review")
        assert kb.a3_gate_granted(db, "t_a")

    def test_a_gate_on_another_task_does_not_leak(self, db):
        decision = self._decision(db)
        _seed_task(db, "t_other")
        with kb.write_txn(db):
            db.execute(
                "INSERT INTO task_comments (task_id, author, body, created_at)"
                " VALUES ('t_other','operator','A3_GATE=GRANTED',1)"
            )
        assert not kb.a3_gate_granted(db, decision.task_id)

    def test_no_gate_at_all_is_a_denial(self, db):
        self._decision(db)
        assert not kb.a3_gate_granted(db, "t_a")

    # --- L2: revocation must be durable, not undoable by deleting a comment ---

    def test_comment_only_revocation_is_reversible_which_is_why_the_latch_exists(
        self, db
    ):
        """Documents the weakness the latch closes, rather than assuming it."""
        self._decision(db)
        self._comment(db, "A3_GATE=GRANTED")
        self._comment(db, "A3_GATE=REVOKED")
        assert not kb.a3_gate_granted(db, "t_a")

        # Delete the revoking comment: the comment-only gate comes back to life.
        with kb.write_txn(db):
            db.execute(
                "DELETE FROM task_comments WHERE body LIKE '%A3_GATE=REVOKED%'"
            )
        assert kb.a3_gate_granted(db, "t_a"), (
            "comment-only revocation is reversible — this is the L2 defect"
        )

    def test_a_latched_revocation_survives_deleting_every_comment(self, db):
        self._decision(db)
        self._comment(db, "A3_GATE=GRANTED")
        assert kb.a3_gate_granted(db, "t_a")

        kb.latch_a3_revocation(db, task_id="t_a", reason="incident-123")
        assert kb.a3_revocation_latched(db, "t_a")
        assert not kb.a3_gate_granted(db, "t_a")

        # Erase all comments — including any revoking one. The latch holds.
        with kb.write_txn(db):
            db.execute("DELETE FROM task_comments WHERE task_id = 't_a'")
        self._comment(db, "A3_GATE=GRANTED")
        assert not kb.a3_gate_granted(db, "t_a"), (
            "a latched revocation must not be revivable by comment surgery"
        )

    def test_a_latched_revocation_refuses_a_real_transport(self, db):
        decision = self._decision(db)
        self._comment(db, "A3_GATE=GRANTED")
        kb.latch_a3_revocation(db, task_id="t_a", reason="incident-123")
        real = kb.SimulatedTransport(name="real-resume")
        with pytest.raises(kb.ActionNotPermittedError, match="A3 gate"):
            kb.dispatch_outcome(
                db, decision, transport=real,
                policy=kb.ActionPolicy(allow_real_action=True),
            )

    def test_clearing_the_latch_requires_an_explicit_call(self, db):
        self._decision(db)
        self._comment(db, "A3_GATE=GRANTED")
        kb.latch_a3_revocation(db, task_id="t_a", reason="incident-123")
        assert not kb.a3_gate_granted(db, "t_a")

        kb.clear_a3_revocation_latch(db, task_id="t_a", reason="incident closed")
        assert not kb.a3_revocation_latched(db, "t_a")
        assert kb.a3_gate_granted(db, "t_a"), "the surviving grant comment applies again"

    def test_clearing_the_latch_does_not_itself_grant(self, db):
        self._decision(db)
        kb.latch_a3_revocation(db, task_id="t_a", reason="incident-123")
        kb.clear_a3_revocation_latch(db, task_id="t_a", reason="closed")
        assert not kb.a3_gate_granted(db, "t_a"), "no grant comment exists"

    def test_the_latest_latch_event_wins(self, db):
        self._decision(db)
        self._comment(db, "A3_GATE=GRANTED")
        kb.latch_a3_revocation(db, task_id="t_a", reason="one")
        kb.clear_a3_revocation_latch(db, task_id="t_a", reason="two")
        kb.latch_a3_revocation(db, task_id="t_a", reason="three")
        assert kb.a3_revocation_latched(db, "t_a")
        assert not kb.a3_gate_granted(db, "t_a")

    def test_a_latch_on_another_task_does_not_leak(self, db):
        self._decision(db)
        self._comment(db, "A3_GATE=GRANTED")
        _seed_task(db, "t_other")
        kb.latch_a3_revocation(db, task_id="t_other", reason="unrelated")
        assert kb.a3_gate_granted(db, "t_a")

    def test_latch_functions_require_a_reason(self, db):
        for fn in (kb.latch_a3_revocation, kb.clear_a3_revocation_latch):
            with pytest.raises(ValueError, match="reason"):
                fn(db, task_id="t_a", reason="")

    def test_the_latch_is_never_invoked_by_the_slice_itself(self):
        """Guarded interface: nothing in the module calls these."""
        import inspect

        source = inspect.getsource(kb)
        for name in ("latch_a3_revocation", "clear_a3_revocation_latch"):
            # One definition, and no call site anywhere in the module.
            assert source.count(f"def {name}(") == 1
            assert f"{name}(conn" not in source.replace(f"def {name}(conn", "")


class TestSliceConsumerIdentityAndBounds:
    """(4) Named/authenticated identity, hard bounds, freshness, projection."""

    def test_an_unauthenticated_consumer_still_works(self, db):
        kb.ensure_broker_sub(db, consumer="loop")
        assert kb.claim_unseen_events_for_broker(db, consumer="loop") == (0, 0, [])

    def test_a_token_bound_consumer_requires_its_token(self, db):
        kb.ensure_broker_sub(db, consumer="loop", token="s3cret")
        with pytest.raises(kb.BrokerAuthError, match="requires a token"):
            kb.claim_unseen_events_for_broker(db, consumer="loop")

    def test_a_wrong_token_is_rejected(self, db):
        kb.ensure_broker_sub(db, consumer="loop", token="s3cret")
        with pytest.raises(kb.BrokerAuthError, match="invalid token"):
            kb.claim_unseen_events_for_broker(db, consumer="loop", token="guess")

    def test_the_right_token_is_accepted(self, db):
        kb.ensure_broker_sub(db, consumer="loop", token="s3cret")
        assert kb.claim_unseen_events_for_broker(
            db, consumer="loop", token="s3cret"
        ) == (0, 0, [])

    def test_a_name_cannot_be_rebound_to_a_different_token(self, db):
        kb.ensure_broker_sub(db, consumer="loop", token="s3cret")
        with pytest.raises(kb.BrokerAuthError):
            kb.ensure_broker_sub(db, consumer="loop", token="other")

    def test_the_token_is_stored_only_as_a_digest(self, db):
        kb.ensure_broker_sub(db, consumer="loop", token="s3cret")
        row = db.execute(
            "SELECT token_sha256 FROM kanban_broker_subs WHERE consumer='loop'"
        ).fetchone()
        assert row["token_sha256"] and "s3cret" not in row["token_sha256"]
        assert len(row["token_sha256"]) == 64

    def test_drain_enforces_the_token_too(self, db):
        kb.ensure_broker_sub(db, consumer="notifier", token="tok")
        with pytest.raises(kb.BrokerAuthError):
            kb.drain_route_notifications(db, consumer="notifier")

    def test_the_hard_limit_ceiling_cannot_be_exceeded(self, db):
        kb.ensure_broker_sub(db, consumer="loop")
        _seed_task(db, "t_a")
        for _ in range(3):
            _seed_run(db, "t_a")
        kb.record_worker_completion_events(db)
        # A caller asking for far more than the ceiling is clamped, not obeyed.
        _o, _n, events = kb.claim_unseen_events_for_broker(
            db, consumer="loop", limit=10_000_000
        )
        assert len(events) == 3
        assert kb._enforce_limit(10_000_000) == kb.BROKER_MAX_LIMIT

    def test_a_nonsense_limit_is_rejected(self):
        for bad in (0, -5):
            with pytest.raises(ValueError, match="limit must be"):
                kb._enforce_limit(bad)
        with pytest.raises(ValueError, match="must be an integer"):
            kb._enforce_limit("lots")

    def test_a_fractional_limit_is_rejected_not_truncated(self):
        """L1: int(2.7) silently became 2 — a bound the caller never asked for."""
        for bad in (2.7, 0.5, 199.999, -1.5):
            with pytest.raises(ValueError, match="whole number|limit must be"):
                kb._enforce_limit(bad)

    def test_a_whole_valued_float_is_accepted_as_that_integer(self):
        assert kb._enforce_limit(5.0) == 5
        assert kb._enforce_limit(float(kb.BROKER_MAX_LIMIT + 50)) == kb.BROKER_MAX_LIMIT

    def test_a_bool_is_not_an_integer_limit(self):
        for bad in (True, False):
            with pytest.raises(ValueError, match="bool"):
                kb._enforce_limit(bad)

    def test_integer_and_ceiling_behaviour_is_preserved(self):
        assert kb._enforce_limit(1) == 1
        assert kb._enforce_limit(7) == 7
        assert kb._enforce_limit(kb.BROKER_MAX_LIMIT) == kb.BROKER_MAX_LIMIT
        assert kb._enforce_limit(kb.BROKER_MAX_LIMIT + 1) == kb.BROKER_MAX_LIMIT

    def test_freshness_reports_lag_and_backlog_age(self, db):
        kb.ensure_broker_sub(db, consumer="loop")
        _seed_task(db, "t_a")
        _seed_run(db, "t_a")
        kb.record_worker_completion_events(db)

        fresh = kb.consumer_freshness(db, consumer="loop", now=10_000_000_000)
        assert fresh is not None
        assert fresh.lag >= 1
        assert fresh.oldest_unconsumed_age_seconds is not None
        assert fresh.stale(max_lag_seconds=60)

        kb.claim_unseen_events_for_broker(db, consumer="loop")
        caught_up = kb.consumer_freshness(db, consumer="loop")
        assert caught_up.lag == 0
        assert caught_up.oldest_unconsumed_age_seconds is None
        assert not caught_up.stale(max_lag_seconds=60)

    def test_freshness_for_an_unknown_consumer_is_none(self, db):
        assert kb.consumer_freshness(db, consumer="ghost") is None

    def test_the_notification_projection_is_typed_and_matches_the_text(self, db):
        _seed_task(db, "t_a", provider="p")
        decision = kb.decide_route(
            completion={**_completion_for(1), "worker_session_id": "wsess-1"},
            task_row=_task_row(db, "t_a"),
        )
        projection = kb.project_notification(decision)
        assert projection.route == kb.ROUTE_CONTINUE
        assert projection.session_id == "wsess-1"
        assert projection.spawn is False
        assert projection.text == kb.render_route_notification(decision)
        assert projection.to_json()["spawn"] is False


class TestSliceRecoveryAndHeartbeat:
    """(5) Deterministic recovery/heartbeat over the NATIVE claim primitives."""

    def test_a_claim_is_taken_and_heartbeat_extends_it(self, db):
        _seed_task(db, "t_a", status="ready")
        claimed = kb.claim_task(db, "t_a", claimer="host:1", ttl_seconds=60)
        assert claimed is not None
        assert kb.heartbeat_claim(db, "t_a", claimer="host:1", ttl_seconds=60)

    def test_a_foreign_heartbeat_does_not_extend_someone_elses_claim(self, db):
        _seed_task(db, "t_a", status="ready")
        kb.claim_task(db, "t_a", claimer="host:1", ttl_seconds=60)
        assert not kb.heartbeat_claim(db, "t_a", claimer="host:2", ttl_seconds=60)

    def test_a_second_claimer_cannot_take_a_claimed_task(self, db):
        _seed_task(db, "t_a", status="ready")
        assert kb.claim_task(db, "t_a", claimer="host:1") is not None
        assert kb.claim_task(db, "t_a", claimer="host:2") is None

    def test_an_operator_reclaim_produces_a_reclaimed_run_that_routes(self, db):
        _seed_task(db, "t_a", status="ready", provider="p")
        kb.claim_task(db, "t_a", claimer="host:1", ttl_seconds=60)
        assert kb.reclaim_task(db, "t_a", reason="operator abort")

        run = db.execute(
            "SELECT id, outcome FROM task_runs WHERE task_id='t_a'"
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert run is not None and run["outcome"] == "reclaimed"

        kb.record_worker_completion_events(db)
        payload = _completion_payload(db, int(run["id"]))
        assert payload["outcome"] == "reclaimed"
        decision = kb.decide_route(
            completion=payload, task_row=_task_row(db, "t_a")
        )
        # reclaimed is retryable, but with no worker session it fails closed.
        assert decision.route == kb.ROUTE_REVIEW
        assert decision.reason == "missing_worker_session"

    def test_a_reclaimed_run_with_a_declared_seat_continues(self, db):
        _seed_task(db, "t_a", status="ready")
        kb.claim_task(db, "t_a", claimer="host:1", ttl_seconds=60)
        kb.reclaim_task(db, "t_a", reason="operator abort")
        run = db.execute(
            "SELECT id FROM task_runs WHERE task_id='t_a' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        kb.record_worker_session_provenance(
            db, run_id=int(run["id"]), worker_session_id="wsess-1",
            source=kb.SESSION_SOURCE_OPERATOR,
        )
        kb.record_worker_completion_events(db)

        decision = kb.decide_route(
            completion=_completion_payload(db, int(run["id"])),
            task_row=_task_row(db, "t_a"),
            seats=kb.SeatRegistry([
                kb.Seat("seat-a", "anthropic", "wsess-1", eligible=True)
            ]),
        )
        assert decision.route == kb.ROUTE_CONTINUE
        assert decision.seat == "seat-a"
        assert decision.spawn is False

    def test_release_stale_claims_is_deterministic_with_an_expired_claim(self, db):
        _seed_task(db, "t_a", status="ready")
        kb.claim_task(db, "t_a", claimer="host:1", ttl_seconds=60)
        # Expire the claim deterministically; the PID belongs to no live worker.
        with kb.write_txn(db):
            db.execute(
                "UPDATE tasks SET claim_expires = 1, worker_pid = NULL WHERE id='t_a'"
            )
            db.execute(
                "UPDATE task_runs SET claim_expires = 1, worker_pid = NULL"
                " WHERE task_id='t_a' AND ended_at IS NULL"
            )
        released = kb.release_stale_claims(db)
        assert released >= 1
        status = db.execute("SELECT status FROM tasks WHERE id='t_a'").fetchone()
        assert status["status"] in {"ready", "todo", "blocked"}

    def test_release_stale_claims_is_a_no_op_when_nothing_is_stale(self, db):
        _seed_task(db, "t_a", status="ready")
        kb.claim_task(db, "t_a", claimer="host:1", ttl_seconds=3600)
        assert kb.release_stale_claims(db) == 0
        status = db.execute("SELECT status FROM tasks WHERE id='t_a'").fetchone()
        assert status["status"] == "running"


# ---------------------------------------------------------------------------
# M-PROV: provenance is immutable at fold time — revalidate before acting
# ---------------------------------------------------------------------------


class TestProvenanceRevalidationAtAction:
    """The folded snapshot must not carry a stale warrant into the act."""

    def _continue_decision(self, db, session="wsess-1"):
        _seed_task(db, "t_a", provider="p")
        run_id = _seed_run(db, "t_a", outcome="crashed")
        kb.record_worker_session_provenance(
            db, run_id=run_id, worker_session_id=session,
            source=kb.SESSION_SOURCE_DISPATCHER,
        )
        decision = _fold_and_route(db, run_id)
        assert decision.route == kb.ROUTE_CONTINUE
        return run_id, decision

    def _downgrade(self, db, run_id, source):
        """Bypass the recorder, as a direct SQL write or a future path would."""
        with kb.write_txn(db):
            db.execute(
                "UPDATE task_runs SET worker_session_source = ? WHERE id = ?",
                (source, run_id),
            )

    def test_a_folded_eligible_decision_is_refused_after_a_downgrade(self, db):
        """The exact reviewer scenario: folded dispatcher_spawn, later inferred."""
        run_id, decision = self._continue_decision(db)
        self._downgrade(db, run_id, kb.SESSION_SOURCE_INFERRED)

        transport = kb.SimulatedTransport()
        policy = kb.ActionPolicy(simulated_transports=[transport])
        with pytest.raises(kb.ProvenanceChangedError, match="not continue-eligible"):
            kb.dispatch_outcome(db, decision, transport=transport, policy=policy)
        assert transport.observed == [], "no provider action of any kind"

    def test_a_decision_is_refused_when_provenance_is_removed(self, db):
        run_id, decision = self._continue_decision(db)
        self._downgrade(db, run_id, None)
        transport = kb.SimulatedTransport()
        with pytest.raises(kb.ProvenanceChangedError, match="no provenance now"):
            kb.dispatch_outcome(
                db, decision, transport=transport,
                policy=kb.ActionPolicy(simulated_transports=[transport]),
            )
        assert transport.observed == []

    def test_a_decision_is_refused_when_provenance_becomes_unknown(self, db):
        run_id, decision = self._continue_decision(db)
        self._downgrade(db, run_id, "vibes")
        transport = kb.SimulatedTransport()
        with pytest.raises(kb.ProvenanceChangedError, match="not continue-eligible"):
            kb.dispatch_outcome(
                db, decision, transport=transport,
                policy=kb.ActionPolicy(simulated_transports=[transport]),
            )
        assert transport.observed == []

    def test_a_decision_is_refused_when_the_session_itself_changed(self, db):
        run_id, decision = self._continue_decision(db)
        with kb.write_txn(db):
            db.execute(
                "UPDATE task_runs SET worker_session_id = 'someone-else' WHERE id = ?",
                (run_id,),
            )
        transport = kb.SimulatedTransport()
        with pytest.raises(kb.ProvenanceChangedError, match="session changed"):
            kb.dispatch_outcome(
                db, decision, transport=transport,
                policy=kb.ActionPolicy(simulated_transports=[transport]),
            )
        assert transport.observed == []

    def test_an_unchanged_decision_still_dispatches(self, db):
        """The guard must not be vacuous."""
        _run_id, decision = self._continue_decision(db)
        transport = kb.SimulatedTransport()
        assert kb.dispatch_outcome(
            db, decision, transport=transport,
            policy=kb.ActionPolicy(simulated_transports=[transport]),
        )
        assert transport.observed == [decision]

    def test_non_continue_routes_are_not_revalidated(self, db):
        """Only CONTINUE would hand work back; REVIEW/BLOCK/CLOSE carry no warrant."""
        _seed_task(db, "t_a", status="done")
        run_id = _seed_run(db, "t_a", outcome="completed")
        decision = _fold_and_route(db, run_id)
        assert decision.route == kb.ROUTE_CLOSE

        with kb.write_txn(db):
            db.execute("DELETE FROM task_runs WHERE id = ?", (run_id,))
        transport = kb.SimulatedTransport()
        assert kb.dispatch_outcome(
            db, decision, transport=transport,
            policy=kb.ActionPolicy(simulated_transports=[transport]),
        )

    def test_a_later_upgrade_does_not_revive_an_already_folded_review(self, db):
        """A REVIEW stays REVIEW until a NEW completion event exists."""
        _seed_task(db, "t_a", provider="p")
        # A session id but NO provenance — the branch under test.
        run_id = _seed_run(db, "t_a", outcome="crashed", worker_session_id="wsess-1")
        first = _fold_and_route(db, run_id)
        assert first.route == kb.ROUTE_REVIEW
        assert first.reason == "missing_session_provenance"

        # Upgrade the run afterwards.
        kb.record_worker_session_provenance(
            db, run_id=run_id, worker_session_id="wsess-1",
            source=kb.SESSION_SOURCE_DISPATCHER,
        )

        # Re-folding produces nothing: exactly-once still holds, so the stored
        # completion payload (and therefore the decision) is unchanged.
        assert kb.record_worker_completion_events(db) == []
        payload = _completion_payload(db, run_id)
        assert payload["worker_session_source"] is None
        again = kb.decide_route(
            completion=payload, task_row=_task_row(db, "t_a")
        )
        assert again.route == kb.ROUTE_REVIEW

    def test_revalidation_does_not_disturb_exactly_once(self, db):
        _run_id, decision = self._continue_decision(db)
        transport = kb.SimulatedTransport()
        policy = kb.ActionPolicy(simulated_transports=[transport])
        kb.dispatch_outcome(db, decision, transport=transport, policy=policy)
        assert kb.record_worker_completion_events(db) == []
        n = db.execute(
            "SELECT COUNT(*) c FROM task_events WHERE kind = ?",
            (kb.BROKER_EVENT_WORKER_COMPLETION,),
        ).fetchone()["c"]
        assert n == 1

    def test_current_run_provenance_reads_live_values(self, db):
        run_id, _decision = self._continue_decision(db)
        assert kb.current_run_provenance(db, run_id) == (
            "wsess-1", kb.SESSION_SOURCE_DISPATCHER,
        )
        self._downgrade(db, run_id, kb.SESSION_SOURCE_INFERRED)
        assert kb.current_run_provenance(db, run_id) == (
            "wsess-1", kb.SESSION_SOURCE_INFERRED,
        )

    def test_a_missing_run_has_no_provenance(self, db):
        assert kb.current_run_provenance(db, 999999) == (None, None)

    # --- monotonic recorder (defence in depth on the ordinary write path) ---

    def test_the_recorder_refuses_a_declared_to_inferred_downgrade(self, db):
        _seed_task(db, "t_a")
        run_id = _seed_run(db, "t_a")
        kb.record_worker_session_provenance(
            db, run_id=run_id, worker_session_id="w",
            source=kb.SESSION_SOURCE_DISPATCHER,
        )
        with pytest.raises(kb.ProvenanceDowngradeError, match="refusing to downgrade"):
            kb.record_worker_session_provenance(
                db, run_id=run_id, worker_session_id="w",
                source=kb.SESSION_SOURCE_INFERRED,
            )
        _session, source = kb.current_run_provenance(db, run_id)
        assert source == kb.SESSION_SOURCE_DISPATCHER

    def test_declared_to_declared_is_allowed(self, db):
        _seed_task(db, "t_a")
        run_id = _seed_run(db, "t_a")
        kb.record_worker_session_provenance(
            db, run_id=run_id, worker_session_id="w",
            source=kb.SESSION_SOURCE_DISPATCHER,
        )
        kb.record_worker_session_provenance(
            db, run_id=run_id, worker_session_id="w",
            source=kb.SESSION_SOURCE_OPERATOR,
        )
        assert kb.current_run_provenance(db, run_id)[1] == kb.SESSION_SOURCE_OPERATOR

    def test_inferred_to_declared_upgrade_is_allowed(self, db):
        _seed_task(db, "t_a")
        run_id = _seed_run(db, "t_a")
        kb.record_worker_session_provenance(
            db, run_id=run_id, worker_session_id="w",
            source=kb.SESSION_SOURCE_INFERRED,
        )
        kb.record_worker_session_provenance(
            db, run_id=run_id, worker_session_id="w",
            source=kb.SESSION_SOURCE_DISPATCHER,
        )
        assert kb.current_run_provenance(db, run_id)[1] == kb.SESSION_SOURCE_DISPATCHER

    def test_an_explicit_downgrade_is_possible_but_must_be_asked_for(self, db):
        _seed_task(db, "t_a")
        run_id = _seed_run(db, "t_a")
        kb.record_worker_session_provenance(
            db, run_id=run_id, worker_session_id="w",
            source=kb.SESSION_SOURCE_DISPATCHER,
        )
        kb.record_worker_session_provenance(
            db, run_id=run_id, worker_session_id="w",
            source=kb.SESSION_SOURCE_INFERRED, allow_downgrade=True,
        )
        assert kb.current_run_provenance(db, run_id)[1] == kb.SESSION_SOURCE_INFERRED


class TestR11ActionBoundarySerialization:
    """R11 — no writer may interleave between revalidate, permit and observe."""

    def _continue_decision(self, conn, task_id="t_a"):
        _seed_task(conn, task_id, provider="p")
        run_id = _seed_run(conn, task_id, outcome="crashed")
        kb.record_worker_session_provenance(
            conn, run_id=run_id, worker_session_id="wsess-1",
            source=kb.SESSION_SOURCE_DISPATCHER,
        )
        decision = _fold_and_route(conn, run_id)
        assert decision.route == kb.ROUTE_CONTINUE
        return run_id, decision

    def test_a_downgrade_cannot_land_between_the_checks_and_the_act(self, tmp_path):
        """The R11 window: verify warrant -> (downgrade lands) -> act."""
        path = _fresh_db(tmp_path / "board" / "kanban.db")
        conn = kb.connect(path)
        run_id, decision = self._continue_decision(conn)

        observed_source: list[str | None] = []
        downgrade_done = threading.Event()
        in_observe = threading.Event()

        class ProbingTransport:
            name = "probe"

            def observe(self, _decision):
                # We are inside the serialized boundary. Ask a *separate*
                # connection what the world looks like right now.
                in_observe.set()
                # Give the competing writer a real chance to interleave.
                downgrade_done.wait(timeout=3.0)
                probe = sqlite3.connect(path, timeout=30)
                probe.row_factory = sqlite3.Row
                try:
                    observed_source.append(
                        kb.current_run_provenance(probe, run_id)[1]
                    )
                finally:
                    probe.close()

        transport = ProbingTransport()
        policy = kb.ActionPolicy(simulated_transports=[transport])
        errors: list[BaseException] = []

        def downgrader() -> None:
            in_observe.wait(timeout=10.0)
            other = kb.connect(path)
            try:
                with kb.write_txn(other):
                    other.execute(
                        "UPDATE task_runs SET worker_session_source = ? WHERE id = ?",
                        (kb.SESSION_SOURCE_INFERRED, run_id),
                    )
            except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
                errors.append(exc)
            finally:
                downgrade_done.set()
                other.close()

        thread = threading.Thread(target=downgrader)
        thread.start()
        try:
            assert kb.dispatch_outcome(
                conn, decision, transport=transport, policy=policy
            )
        finally:
            thread.join(timeout=30)
            conn.close()

        assert observed_source == [kb.SESSION_SOURCE_DISPATCHER], (
            "a downgrade must not become visible inside the action boundary; "
            f"saw {observed_source}"
        )
        assert not errors, f"the competing writer errored: {errors}"

        # And the downgrade does land afterwards — the lock delays, not drops.
        after = kb.connect(path)
        try:
            assert kb.current_run_provenance(after, run_id)[1] == (
                kb.SESSION_SOURCE_INFERRED
            )
        finally:
            after.close()

    def test_an_a3_revocation_cannot_land_mid_boundary(self, tmp_path):
        """Same window, exercised with the other concurrent writer that matters."""
        path = _fresh_db(tmp_path / "board" / "kanban.db")
        conn = kb.connect(path)
        run_id, decision = self._continue_decision(conn)
        with kb.write_txn(conn):
            conn.execute(
                "INSERT INTO task_comments (task_id, author, body, created_at)"
                " VALUES ('t_a','operator','A3_GATE=GRANTED',1)"
            )

        gate_seen: list[bool] = []
        latched = threading.Event()
        in_observe = threading.Event()

        class ProbingTransport:
            name = "real-ish"

            def observe(self, _decision):
                in_observe.set()
                latched.wait(timeout=3.0)
                probe = sqlite3.connect(path, timeout=30)
                probe.row_factory = sqlite3.Row
                try:
                    gate_seen.append(kb.a3_gate_granted(probe, "t_a"))
                finally:
                    probe.close()

        transport = ProbingTransport()
        # Real transport path: needs allow_real_action AND the gate.
        policy = kb.ActionPolicy(allow_real_action=True)

        def revoker() -> None:
            in_observe.wait(timeout=10.0)
            other = kb.connect(path)
            try:
                kb.latch_a3_revocation(other, task_id="t_a", reason="race")
            finally:
                latched.set()
                other.close()

        thread = threading.Thread(target=revoker)
        thread.start()
        try:
            assert kb.dispatch_outcome(
                conn, decision, transport=transport, policy=policy
            )
        finally:
            thread.join(timeout=30)
            conn.close()

        assert gate_seen == [True], (
            f"a revocation must not become visible mid-boundary; saw {gate_seen}"
        )

        after = kb.connect(path)
        try:
            assert kb.a3_revocation_latched(after, "t_a")
            assert not kb.a3_gate_granted(after, "t_a")
        finally:
            after.close()

    def test_a_downgrade_committed_before_dispatch_is_still_refused(self, tmp_path):
        """Serialization must not weaken the ordinary revalidation."""
        path = _fresh_db(tmp_path / "board" / "kanban.db")
        conn = kb.connect(path)
        try:
            run_id, decision = self._continue_decision(conn)
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE task_runs SET worker_session_source = ? WHERE id = ?",
                    (kb.SESSION_SOURCE_INFERRED, run_id),
                )
            transport = kb.SimulatedTransport()
            with pytest.raises(kb.ProvenanceChangedError):
                kb.dispatch_outcome(
                    conn, decision, transport=transport,
                    policy=kb.ActionPolicy(simulated_transports=[transport]),
                )
            assert transport.observed == []
        finally:
            conn.close()

    def test_a_refusal_inside_the_boundary_leaves_no_transaction_open(self, db):
        """A raise inside write_txn must roll back cleanly, not poison the conn."""
        run_id, decision = self._continue_decision(db)
        with kb.write_txn(db):
            db.execute(
                "UPDATE task_runs SET worker_session_source = NULL WHERE id = ?",
                (run_id,),
            )
        transport = kb.SimulatedTransport()
        with pytest.raises(kb.ProvenanceChangedError):
            kb.dispatch_outcome(
                db, decision, transport=transport,
                policy=kb.ActionPolicy(simulated_transports=[transport]),
            )
        # The connection is still usable for a fresh write transaction.
        with kb.write_txn(db):
            db.execute("SELECT 1")
        assert kb.current_run_provenance(db, run_id)[1] is None

    def test_a_transport_raising_inside_the_boundary_rolls_back(self, db):
        _run_id, decision = self._continue_decision(db)

        class ExplodingTransport:
            name = "boom"

            def observe(self, _decision):
                raise RuntimeError("transport failed")

        transport = ExplodingTransport()
        with pytest.raises(RuntimeError, match="transport failed"):
            kb.dispatch_outcome(
                db, decision, transport=transport,
                policy=kb.ActionPolicy(simulated_transports=[transport]),
            )
        with kb.write_txn(db):
            db.execute("SELECT 1")

    def test_non_continue_routes_are_not_serialized(self, db):
        """They carry no warrant; the fast path must remain unserialized."""
        _seed_task(db, "t_a", status="done")
        run_id = _seed_run(db, "t_a", outcome="completed")
        decision = _fold_and_route(db, run_id)
        assert decision.route == kb.ROUTE_CLOSE

        class NestingTransport:
            name = "nester"

            def __init__(self):
                self.ok = False

            def observe(self, _decision):
                # Would deadlock/raise if we were already inside write_txn.
                with kb.write_txn(db):
                    db.execute("SELECT 1")
                self.ok = True

        transport = NestingTransport()
        assert kb.dispatch_outcome(
            db, decision, transport=transport,
            policy=kb.ActionPolicy(simulated_transports=[transport]),
        )
        assert transport.ok

    def test_a_delegated_child_can_no_longer_dispatch_a_continue(self, db, monkeypatch):
        """Documented side effect of serializing on write_txn.

        ``write_txn`` fails closed for delegated-child contexts. Routing a
        CONTINUE through it therefore inherits that guard: a delegated child can
        no longer act on a session-reuse decision. That is a strengthening —
        a delegated child has no business driving another session — but it *is*
        a behaviour change, so it is pinned rather than left to be discovered.
        """
        _run_id, decision = self._continue_decision(db)
        transport = kb.SimulatedTransport()
        policy = kb.ActionPolicy(simulated_transports=[transport])
        monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", "1")

        with pytest.raises(PermissionError):
            kb.dispatch_outcome(db, decision, transport=transport, policy=policy)
        assert transport.observed == []

    def test_a_delegated_child_may_still_observe_non_continue_routes(self, db, monkeypatch):
        """The asymmetry, stated: only the acting route is blocked."""
        _seed_task(db, "t_a", status="done")
        run_id = _seed_run(db, "t_a", outcome="completed")
        decision = _fold_and_route(db, run_id)
        assert decision.route == kb.ROUTE_CLOSE

        transport = kb.SimulatedTransport()
        policy = kb.ActionPolicy(simulated_transports=[transport])
        monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", "1")
        assert kb.dispatch_outcome(db, decision, transport=transport, policy=policy)

    def test_routing_stays_pure_and_does_no_io(self, db):
        """decide_route must not have acquired a connection dependency."""
        import inspect

        params = inspect.signature(kb.decide_route).parameters
        assert "conn" not in params
        source = inspect.getsource(kb.decide_route)
        for forbidden in ("write_txn", "conn.execute", "revalidate_"):
            assert forbidden not in source


class TestLatchSurvivesEventGc:
    """L2 follow-up: gc_events must not silently erase a revocation latch."""

    def _terminal_task_with_old_events(self, db) -> None:
        _seed_task(db, "t_a", status="done")
        old = int(time.time()) - (365 * 24 * 3600)
        with kb.write_txn(db):
            db.execute(
                "INSERT INTO task_events (task_id, run_id, kind, payload, created_at)"
                " VALUES ('t_a', NULL, 'commented', NULL, ?)",
                (old,),
            )

    def test_gc_still_prunes_ordinary_history_for_terminal_tasks(self, db):
        self._terminal_task_with_old_events(db)
        removed = kb.gc_events(db, older_than_seconds=30 * 24 * 3600)
        assert removed == 1

    def test_a_latch_is_not_pruned_and_the_gate_stays_shut(self, db):
        """Genuinely reachable: gc_events is wired to a live CLI path."""
        self._terminal_task_with_old_events(db)
        with kb.write_txn(db):
            db.execute(
                "INSERT INTO task_comments (task_id, author, body, created_at)"
                " VALUES ('t_a','operator','A3_GATE=GRANTED',1)"
            )
        kb.latch_a3_revocation(db, task_id="t_a", reason="incident")
        # Age the latch well past the cutoff.
        old = int(time.time()) - (365 * 24 * 3600)
        with kb.write_txn(db):
            db.execute(
                "UPDATE task_events SET created_at = ? WHERE kind = ?",
                (old, kb.A3_EVENT_REVOKED),
            )
        assert not kb.a3_gate_granted(db, "t_a")

        kb.gc_events(db, older_than_seconds=30 * 24 * 3600)

        assert kb.a3_revocation_latched(db, "t_a"), "the latch must survive GC"
        assert not kb.a3_gate_granted(db, "t_a"), (
            "GC must not silently re-open a revoked A3 gate"
        )

    def test_a_cleared_latch_marker_also_survives_gc(self, db):
        """Otherwise GC could resurrect a latch that was deliberately cleared."""
        self._terminal_task_with_old_events(db)
        kb.latch_a3_revocation(db, task_id="t_a", reason="incident")
        kb.clear_a3_revocation_latch(db, task_id="t_a", reason="closed")
        old = int(time.time()) - (365 * 24 * 3600)
        with kb.write_txn(db):
            db.execute(
                "UPDATE task_events SET created_at = ? WHERE kind IN (?, ?)",
                (old, kb.A3_EVENT_REVOKED, kb.A3_EVENT_REVOCATION_CLEARED),
            )
        kb.gc_events(db, older_than_seconds=30 * 24 * 3600)
        assert not kb.a3_revocation_latched(db, "t_a")
        rows = db.execute(
            "SELECT COUNT(*) c FROM task_events WHERE kind IN (?, ?)",
            (kb.A3_EVENT_REVOKED, kb.A3_EVENT_REVOCATION_CLEARED),
        ).fetchone()["c"]
        assert rows == 2, "both latch markers are retained"


# ---------------------------------------------------------------------------
# no live DB writes
# ---------------------------------------------------------------------------


#: The audit-hook proof runs in a CHILD PROCESS on purpose.
#:
#: ``sys.addaudithook`` can never be removed once installed. An earlier version
#: of this file installed one in-process, which permanently blocked
#: ``subprocess.*`` for every test that ran afterwards in the same session —
#: an ordering-dependent booby trap for the whole suite. Isolating it in a
#: subprocess keeps the proof and removes the contamination entirely.
_AUDIT_PROBE = r'''
import json, os, sys, pathlib

repo, tmp = sys.argv[1], sys.argv[2]
sys.path.insert(0, repo)
from hermes_cli import kanban_db as kb

path = pathlib.Path(tmp) / "board" / "kanban.db"
kb.init_db(path)
conn = kb.connect(path)

allowed = os.path.realpath(tmp)
violations, spawns = [], []

def hook(event, args):
    if event.startswith("subprocess.") or event in {
        "os.system", "os.exec", "os.posix_spawn", "os.fork",
    }:
        spawns.append(event)
        raise RuntimeError("BLOCKED spawn: " + event)
    if event == "open":
        target, mode, flags = args[0], args[1], args[2]
        if isinstance(target, int):
            return
        p = str(target)
        if mode is not None:
            writing = any(f in str(mode) for f in ("w", "a", "x", "+"))
        else:
            writing = bool(
                isinstance(flags, int)
                and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND)
            )
        if writing and p.startswith("/") and not p.startswith(allowed):
            violations.append(p + " mode=" + str(mode))

sys.addaudithook(hook)

now = 1700000000
with kb.write_txn(conn):
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at, workspace_kind,"
        " consecutive_failures, provider_override, goal_mode)"
        " VALUES ('t_a','seeded','running',?, 'scratch', 0, 'p', 0)", (now,))
    conn.execute(
        "INSERT INTO task_runs (task_id, profile, status, started_at, ended_at,"
        " outcome, worker_session_id) VALUES ('t_a','worker','done',?,?,'crashed','w')",
        (now, now + 1))

kb.ensure_broker_sub(conn, consumer="loop")
kb.record_worker_completion_events(conn)
_o, _n, events = kb.claim_unseen_events_for_broker(conn, consumer="loop")
payload = json.loads(conn.execute(
    "SELECT payload FROM task_events WHERE kind = ?",
    (kb.BROKER_EVENT_WORKER_COMPLETION,)).fetchone()["payload"])
task_row = conn.execute("SELECT * FROM tasks WHERE id='t_a'").fetchone()
decision = kb.decide_route(completion=payload, task_row=task_row)
kb.record_route_decision_event(conn, decision)
kb.drain_route_notifications(conn, consumer="loop")

# Slice 4: build a session-resume plan under the same audit hook. Planning a
# resume must be as spawn-free as routing is; if plan_session_resume ever grew
# a subprocess, config read, or out-of-tmp write, this probe fails here.
resume_decision = kb.RouteDecision(
    route=kb.ROUTE_CONTINUE, reason="retryable_crashed:1/2", task_id="t_a",
    run_id=1, outcome="crashed", session_id="sess-x", provider="claude-code")
resume_binding = kb.SessionBinding(
    provider="claude-code", session_id="sess-x",
    source=kb.SESSION_SOURCE_DISPATCHER)
resume_capsule = kb.build_resume_capsule(
    decision=resume_decision, instruction="Resume and finish the run.")
plan = kb.plan_session_resume(
    decision=resume_decision, binding=resume_binding, capsule=resume_capsule)

conn.close()

print(json.dumps({
    "events": len(events),
    "route": decision.route,
    "spawn": decision.spawn,
    "spawns": spawns,
    "violations": violations,
    "plan_argv": list(plan.command.argv),
    "plan_executed": plan.executed,
    "plan_requires_a3": plan.requires_a3_gate,
}))
'''


class TestNoLiveWrites:
    def test_the_slice_writes_only_inside_the_disposable_db(self, tmp_path):
        """Audit-hook proof, run in a child process so it cannot leak (T1)."""
        repo_root = Path(kb.__file__).resolve().parents[1]
        proc = subprocess.run(
            [sys.executable, "-c", _AUDIT_PROBE, str(repo_root), str(tmp_path)],
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert proc.returncode == 0, proc.stderr
        result = json.loads(proc.stdout.strip().splitlines()[-1])

        assert result["events"] == 1
        assert result["spawn"] is False
        assert result["spawns"] == []
        assert result["violations"] == [], (
            f"wrote outside the disposable DB: {result['violations']}"
        )
        # Slice 4: the resume plan was built under the same hook. It rendered
        # the command without ever spawning it, and stayed inert.
        assert result["plan_argv"][:2] == ["claude", "--resume"]
        assert result["plan_executed"] is False
        assert result["plan_requires_a3"] is True

    def test_this_module_installs_no_process_wide_audit_hook(self):
        """Regression for T1 itself: subprocess must still work after import.

        If any test in this file installed an audit hook in-process, this call
        would raise and every later test in the session would too.
        """
        proc = subprocess.run(
            [sys.executable, "-c", "print('subprocess-ok')"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert proc.returncode == 0
        assert proc.stdout.strip() == "subprocess-ok"

    @pytest.mark.skipif(not LIVE_BOARD.exists(), reason="live board not present")
    def test_a_disposable_snapshot_of_a_real_board_is_never_written_back(self, tmp_path):
        """Fidelity against the real schema, with the source proven untouched."""
        before = hashlib.sha256(LIVE_BOARD.read_bytes()).hexdigest()

        dest = tmp_path / "snapshot.db"
        src = sqlite3.connect(f"file:{LIVE_BOARD}?mode=ro", uri=True)
        try:
            snap = sqlite3.connect(dest)
            try:
                src.backup(snap)
            finally:
                snap.close()
        finally:
            src.close()

        after = hashlib.sha256(LIVE_BOARD.read_bytes()).hexdigest()
        assert before == after, "taking the snapshot must not touch the source"

        conn = kb.connect(dest)
        try:
            kb.ensure_broker_sub(conn, consumer="review-slice")
            recorded = kb.record_worker_completion_events(conn, limit=25)
            _o, _n, events = kb.claim_unseen_events_for_broker(
                conn, consumer="review-slice", limit=25,
            )
            routes = []
            for event in events:
                if event.kind != kb.BROKER_EVENT_WORKER_COMPLETION:
                    continue
                task_row = _task_row(conn, event.task_id)
                routes.append(kb.decide_route(completion=event.payload, task_row=task_row))
        finally:
            conn.close()

        # The real board exercises the real schema; assert only invariants.
        assert len(recorded) <= 25
        assert all(r.spawn is False for r in routes)
        assert all(r.route in kb.VALID_ROUTES for r in routes)
        # Every write landed in the snapshot, not the source. The source is a
        # live board other agents write to continuously, so its hash is NOT a
        # sound assertion here; the read-only source connection is the proof,
        # and the snapshot must carry the new rows.
        snap = sqlite3.connect(dest)
        try:
            n = snap.execute(
                "SELECT COUNT(*) FROM task_events WHERE kind = ?",
                (kb.BROKER_EVENT_WORKER_COMPLETION,),
            ).fetchone()[0]
        finally:
            snap.close()
        assert n == len(recorded)


# ---------------------------------------------------------------------------
# Slice 4 — session-resume invocation contract (pre-activation, inert)
# ---------------------------------------------------------------------------


def _continue_decision(**overrides) -> "kb.RouteDecision":
    """A validated CONTINUE decision, the only kind a resume may be planned for."""
    fields = dict(
        route=kb.ROUTE_CONTINUE,
        reason="retryable_crashed:1/2",
        task_id="t_a",
        run_id=7,
        outcome="crashed",
        session_id="sess-1",
        provider=kb.PROVIDER_CLAUDE_CODE,
        seat="seat-a",
    )
    fields.update(overrides)
    return kb.RouteDecision(**fields)


def _binding(**overrides) -> kb.SessionBinding:
    fields = dict(
        provider=kb.PROVIDER_CLAUDE_CODE,
        session_id="sess-1",
        source=kb.SESSION_SOURCE_DISPATCHER,
        seat_id="seat-a",
    )
    fields.update(overrides)
    return kb.SessionBinding(**fields)


def _plan(decision=None, binding=None, instruction="Resume and finish the run.", **kw):
    decision = decision if decision is not None else _continue_decision()
    binding = binding if binding is not None else _binding()
    capsule = kw.pop("capsule", None)
    if capsule is None:
        capsule = kb.build_resume_capsule(decision=decision, instruction=instruction)
    return kb.plan_session_resume(
        decision=decision, binding=binding, capsule=capsule, **kw
    )


def _raw_capsule(**overrides) -> "kb.ResumeCapsule":
    """Construct a frozen ResumeCapsule DIRECTLY, bypassing the builder.

    `frozen=True` prevents mutation; it does not validate. This is the exact
    hole the plan boundary must close.
    """
    # Defaults mirror `_continue_decision()` so the baseline pairs cleanly and
    # each test isolates exactly one malformed field.
    fields = dict(
        capsule_version=kb.RESUME_CAPSULE_VERSION,
        task_id="t_a",
        run_id=7,
        outcome="crashed",
        reason="retryable_crashed:1/2",
        instruction="Resume and finish the run.",
        notes=(),
    )
    fields.update(overrides)
    return kb.ResumeCapsule(**fields)


class TestDirectCapsuleRevalidation:
    """A directly-constructed capsule must be fully revalidated at the plan."""

    def test_the_baseline_direct_capsule_is_accepted(self):
        """Guard against a vacuous suite: the well-formed case must pass."""
        plan = _plan(capsule=_raw_capsule())
        assert plan.command.executed is False

    # --- capsule_version ---

    @pytest.mark.parametrize(
        "bad", [0, 2, -1, "1", None, True, 1.0],
        ids=["zero", "future", "negative", "str", "none", "bool", "float"],
    )
    def test_a_bad_capsule_version_is_rejected(self, bad):
        with pytest.raises(kb.InvocationPlanError, match="capsule_version"):
            _plan(capsule=_raw_capsule(capsule_version=bad))

    # --- identifier fields ---

    @pytest.mark.parametrize("field", ["task_id", "outcome", "reason"])
    @pytest.mark.parametrize(
        "bad", ["", "   ", None, 5, b"t_a", "with\nnewline", "with\x00nul",
                "line\u2028sep"],
        ids=["empty", "blank", "none", "int", "bytes", "newline", "nul", "u2028"],
    )
    def test_malformed_identifier_fields_are_rejected(self, field, bad):
        with pytest.raises(kb.InvocationPlanError, match=f"capsule.{field}"):
            _plan(capsule=_raw_capsule(**{field: bad}))

    def test_outcome_and_reason_were_previously_unchecked(self):
        """Named explicitly: these two had no validation at all before."""
        for field in ("outcome", "reason"):
            with pytest.raises(kb.InvocationPlanError):
                _plan(capsule=_raw_capsule(**{field: 12345}))

    # --- instruction ---

    @pytest.mark.parametrize(
        "bad", ["", "   ", None, 7, "has\nnewline", "has\rreturn", "has\x00nul",
                "has\x07bell", "sep\u2029here"],
        ids=["empty", "blank", "none", "int", "newline", "return", "nul", "bell",
             "u2029"],
    )
    def test_a_malformed_instruction_is_rejected(self, bad):
        with pytest.raises(kb.InvocationPlanError, match="capsule.instruction"):
            _plan(capsule=_raw_capsule(instruction=bad))

    def test_an_overlength_instruction_is_rejected(self):
        big = "x" * (kb.RESUME_CAPSULE_MAX_INSTRUCTION_CHARS + 1)
        with pytest.raises(kb.InvocationPlanError, match="capsule.instruction"):
            _plan(capsule=_raw_capsule(instruction=big))

    def test_an_instruction_at_the_bound_is_accepted(self):
        exact = "x" * kb.RESUME_CAPSULE_MAX_INSTRUCTION_CHARS
        assert _plan(capsule=_raw_capsule(instruction=exact))

    # --- run_id ---

    @pytest.mark.parametrize(
        "bad", [0, -1, "1", None, True, 1.5],
        ids=["zero", "negative", "str", "none", "bool", "float"],
    )
    def test_a_bad_run_id_is_rejected(self, bad):
        with pytest.raises(kb.InvocationPlanError, match="run_id"):
            _plan(capsule=_raw_capsule(run_id=bad))

    # --- notes container ---

    @pytest.mark.parametrize(
        "bad", [["a"], "abc", None, {"a": 1}, 5],
        ids=["list", "str", "none", "dict", "int"],
    )
    def test_a_non_tuple_notes_container_is_rejected(self, bad):
        with pytest.raises(kb.InvocationPlanError, match="capsule.notes must be a tuple"):
            _plan(capsule=_raw_capsule(notes=bad))

    def test_too_many_notes_are_rejected(self):
        many = tuple(f"n{i}" for i in range(kb.RESUME_CAPSULE_MAX_NOTES + 1))
        with pytest.raises(kb.InvocationPlanError, match="too many notes"):
            _plan(capsule=_raw_capsule(notes=many))

    def test_notes_at_the_count_bound_are_accepted(self):
        ok = tuple(f"n{i}" for i in range(kb.RESUME_CAPSULE_MAX_NOTES))
        assert _plan(capsule=_raw_capsule(notes=ok))

    # --- individual notes ---

    @pytest.mark.parametrize(
        "bad", ["", "   ", None, 3, b"n", "has\nnewline", "has\x00nul",
                "sep\u2028here"],
        ids=["empty", "blank", "none", "int", "bytes", "newline", "nul", "u2028"],
    )
    def test_a_malformed_note_is_rejected(self, bad):
        with pytest.raises(kb.InvocationPlanError, match=r"capsule.notes\[0\]"):
            _plan(capsule=_raw_capsule(notes=(bad,)))

    def test_a_malformed_note_is_rejected_at_any_position(self):
        notes = ("fine", "also fine", "bad\nnote")
        with pytest.raises(kb.InvocationPlanError, match=r"capsule.notes\[2\]"):
            _plan(capsule=_raw_capsule(notes=notes))

    def test_an_overlength_note_is_rejected(self):
        big = "x" * (kb.RESUME_CAPSULE_MAX_NOTE_CHARS + 1)
        with pytest.raises(kb.InvocationPlanError, match=r"capsule.notes\[0\]"):
            _plan(capsule=_raw_capsule(notes=(big,)))

    def test_a_note_at_the_bound_is_accepted(self):
        exact = "x" * kb.RESUME_CAPSULE_MAX_NOTE_CHARS
        assert _plan(capsule=_raw_capsule(notes=(exact,)))

    # --- the builder and the boundary must agree ---

    def test_the_builder_now_rejects_control_chars_in_the_instruction(self):
        """Previously these passed the builder and only failed later."""
        for bad in ("has\nnewline", "has\rreturn", "has\x00nul", "sep\u2028here"):
            with pytest.raises(kb.InvocationPlanError, match="instruction"):
                kb.build_resume_capsule(
                    decision=_continue_decision(), instruction=bad
                )

    def test_the_builder_rejects_control_chars_in_a_note(self):
        with pytest.raises(kb.InvocationPlanError, match=r"notes\[0\]"):
            kb.build_resume_capsule(
                decision=_continue_decision(), instruction="x",
                notes=("bad\nnote",),
            )

    def test_the_builder_rejects_a_bare_string_as_notes(self):
        with pytest.raises(kb.InvocationPlanError, match="not a string"):
            kb.build_resume_capsule(
                decision=_continue_decision(), instruction="x", notes="abc",
            )

    def test_every_capsule_the_builder_produces_survives_revalidation(self):
        """The two entry points must agree, not merely both exist."""
        capsule = kb.build_resume_capsule(
            decision=_continue_decision(),
            instruction="  Resume and finish.  ",
            notes=("  keep  ", "", "   ", "also"),
        )
        assert capsule.notes == ("keep", "also")
        assert _plan(capsule=capsule)

    # --- U+0085 NEL: a C1 control above the C0 range ---

    def test_nel_is_rejected_in_every_text_field(self):
        """U+0085 is a line break to str.splitlines but sits above 0x1f."""
        assert "x\x85y".splitlines() == ["x", "y"], "premise: NEL breaks lines"
        assert kb._has_control_chars("x\x85y")

        for field in ("task_id", "outcome", "reason"):
            with pytest.raises(kb.InvocationPlanError, match=f"capsule.{field}"):
                _plan(capsule=_raw_capsule(**{field: f"bad\x85{field}"}))

        with pytest.raises(kb.InvocationPlanError, match="capsule.instruction"):
            _plan(capsule=_raw_capsule(instruction="bad\x85instruction"))

        with pytest.raises(kb.InvocationPlanError, match=r"capsule.notes\[0\]"):
            _plan(capsule=_raw_capsule(notes=("bad\x85note",)))

    def test_the_builder_rejects_nel_too(self):
        with pytest.raises(kb.InvocationPlanError, match="instruction"):
            kb.build_resume_capsule(
                decision=_continue_decision(), instruction="bad\x85instruction"
            )
        with pytest.raises(kb.InvocationPlanError, match=r"notes\[0\]"):
            kb.build_resume_capsule(
                decision=_continue_decision(), instruction="ok",
                notes=("bad\x85note",),
            )

    def test_every_forbidden_char_class_is_covered(self):
        """C0, DEL, NEL, and the Unicode separators — one assertion each."""
        for ch in ("\x00", "\n", "\r", "\x07", "\x1f", "\x7f", "\x85",
                   "\u2028", "\u2029"):
            assert kb._has_control_chars(f"a{ch}b"), f"{ch!r} must be forbidden"
        for ch in ("a", " ", "-", ":", "/", "\u00e9", "\u4e2d"):
            assert not kb._has_control_chars(f"a{ch}b"), f"{ch!r} must be allowed"

    def test_revalidation_does_not_execute_anything(self):
        plan = _plan(capsule=_raw_capsule(notes=("a", "b")))
        assert plan.command.executed is False
        assert isinstance(plan.command.argv, tuple)


class TestSliceResumeCommandContract:
    """The command specification is exact, ordered, and deterministic."""

    def test_renders_the_exact_documented_argv(self):
        plan = _plan()
        assert plan.command.argv == (
            "claude",
            "--resume",
            "sess-1",
            "--print",
            "--verbose",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--include-hook-events",
            "--permission-mode",
            "plan",
            "--disallowedTools",
            "Bash,Edit,Write,Read,Glob,Grep,WebFetch,WebSearch,Task",
            "--safe-mode",
            "--strict-mcp-config",
            "--max-turns",
            "1",
        )

    def test_the_session_id_is_the_operand_of_resume(self):
        """--resume must be immediately followed by the session, not a flag."""
        argv = _plan(binding=_binding(session_id="abc-123"),
                     decision=_continue_decision(session_id="abc-123")).command.argv
        i = argv.index("--resume")
        assert argv[i + 1] == "abc-123"

    def test_output_format_operand_is_stream_json(self):
        argv = _plan().command.argv
        assert argv[argv.index("--output-format") + 1] == "stream-json"

    def test_carries_a_parseable_json_schema(self):
        schema = json.loads(_plan().command.output_schema_json)
        assert schema["type"] == "object"
        assert "hook_event" in schema["properties"]["type"]["enum"]

    def test_schema_accessor_returns_an_isolated_copy(self):
        """A caller mutating the returned schema must not poison later plans."""
        first = kb.resume_stream_event_schema()
        first["properties"]["type"]["enum"].append("injected")
        assert "injected" not in kb.resume_stream_event_schema()["properties"]["type"]["enum"]
        assert "injected" not in _plan().command.output_schema_json

    def test_rendering_is_deterministic(self):
        a, b = _plan().command, _plan().command
        assert a.argv == b.argv
        assert a.output_schema_json == b.output_schema_json

    def test_argv_is_an_immutable_tuple(self):
        argv = _plan().command.argv
        assert isinstance(argv, tuple)
        with pytest.raises(AttributeError):
            argv.append("--dangerously-skip-permissions")  # type: ignore[attr-defined]

    def test_capsule_json_is_canonical_and_bounded(self):
        capsule = kb.build_resume_capsule(
            decision=_continue_decision(), instruction="do the thing", notes=["b", "a"]
        )
        payload = json.loads(capsule.to_json())
        assert payload["capsule_version"] == kb.RESUME_CAPSULE_VERSION
        assert payload["task_id"] == "t_a"
        assert payload["run_id"] == 7
        # sorted keys => stable rendering
        assert capsule.to_json() == capsule.to_json()
        assert list(payload) == sorted(payload)

    def test_default_timeout_is_within_declared_bounds(self):
        t = _plan().command.timeout_seconds
        assert kb.RESUME_MIN_TIMEOUT_SECONDS <= t <= kb.RESUME_MAX_TIMEOUT_SECONDS


class TestSliceResumePlanRejections:
    """Every unsafe input fails closed with InvocationPlanError."""

    @pytest.mark.parametrize("route", [kb.ROUTE_REVIEW, kb.ROUTE_BLOCK, kb.ROUTE_CLOSE])
    def test_non_continue_route_is_refused(self, route):
        with pytest.raises(kb.InvocationPlanError, match="continue"):
            _plan(decision=_continue_decision(route=route))

    def test_a_decision_claiming_to_spawn_is_refused(self):
        decision = _continue_decision()
        object.__setattr__(decision, "spawn", True)
        capsule = kb.build_resume_capsule(decision=decision, instruction="x")
        with pytest.raises(kb.InvocationPlanError, match="spawn"):
            kb.plan_session_resume(
                decision=decision, binding=_binding(), capsule=capsule
            )

    def test_unknown_provider_is_refused(self):
        decision = _continue_decision(provider="grok")
        with pytest.raises(kb.InvocationPlanError, match="resume-capable"):
            _plan(decision=decision, binding=_binding(provider="grok"))

    def test_inferred_provenance_is_refused(self):
        with pytest.raises(kb.InvocationPlanError, match="continue-eligible"):
            _plan(binding=_binding(source=kb.SESSION_SOURCE_INFERRED))

    def test_unknown_provenance_is_refused(self):
        with pytest.raises(kb.InvocationPlanError, match="unknown session source"):
            _plan(binding=_binding(source="scraped_from_a_log"))

    @pytest.mark.parametrize("session_id", ["", "   ", None, 5])
    def test_missing_or_malformed_session_id_is_refused(self, session_id):
        with pytest.raises(kb.InvocationPlanError):
            _plan(binding=_binding(session_id=session_id))

    def test_binding_session_must_match_the_decision(self):
        with pytest.raises(kb.InvocationPlanError, match="does not match decision session"):
            _plan(binding=_binding(session_id="a-different-session"))

    def test_binding_provider_must_match_the_decision(self):
        """A provider disagreement is ambiguity, never silently resolved."""
        decision = _continue_decision(provider="grok")
        with pytest.raises(kb.InvocationPlanError, match="does not match decision provider"):
            _plan(decision=decision)

    def test_binding_seat_must_match_the_decision(self):
        with pytest.raises(kb.InvocationPlanError, match="does not match decision seat"):
            _plan(binding=_binding(seat_id="some-other-seat"))

    @pytest.mark.parametrize(
        "timeout",
        [0, -1, kb.RESUME_MIN_TIMEOUT_SECONDS - 1, kb.RESUME_MAX_TIMEOUT_SECONDS + 1],
    )
    def test_unsafe_timeout_is_refused(self, timeout):
        with pytest.raises(kb.InvocationPlanError, match="timeout_seconds"):
            _plan(timeout_seconds=timeout)

    def test_bool_timeout_is_not_an_integer(self):
        with pytest.raises(kb.InvocationPlanError, match="bool"):
            _plan(timeout_seconds=True)

    def test_missing_task_id_is_refused(self):
        with pytest.raises(kb.InvocationPlanError, match="task_id"):
            kb.build_resume_capsule(
                decision=_continue_decision(task_id="  "), instruction="x"
            )

    @pytest.mark.parametrize("run_id", [0, -3])
    def test_non_positive_run_id_is_refused(self, run_id):
        with pytest.raises(kb.InvocationPlanError, match="run_id"):
            kb.build_resume_capsule(
                decision=_continue_decision(run_id=run_id), instruction="x"
            )

    def test_capsule_must_be_a_capsule(self):
        with pytest.raises(kb.InvocationPlanError, match="ResumeCapsule"):
            kb.plan_session_resume(
                decision=_continue_decision(),
                binding=_binding(),
                capsule={"task_id": "t_a", "run_id": 7},  # type: ignore[arg-type]
            )

    def test_capsule_from_another_version_is_refused(self):
        good = kb.build_resume_capsule(decision=_continue_decision(), instruction="x")
        drifted = dataclasses.replace(good, capsule_version=99)
        with pytest.raises(kb.InvocationPlanError, match="capsule_version"):
            _plan(capsule=drifted)

    def test_capsule_must_describe_the_same_run(self):
        other = kb.build_resume_capsule(
            decision=_continue_decision(run_id=99), instruction="x"
        )
        with pytest.raises(kb.InvocationPlanError, match="does not match decision run"):
            _plan(capsule=other)

    def test_capsule_must_describe_the_same_task(self):
        other = kb.build_resume_capsule(
            decision=_continue_decision(task_id="t_other"), instruction="x"
        )
        with pytest.raises(kb.InvocationPlanError, match="does not match decision"):
            _plan(capsule=other)

    def test_oversized_instruction_is_refused(self):
        big = "x" * (kb.RESUME_CAPSULE_MAX_INSTRUCTION_CHARS + 1)
        with pytest.raises(kb.InvocationPlanError, match="instruction exceeds"):
            kb.build_resume_capsule(decision=_continue_decision(), instruction=big)

    def test_empty_instruction_is_refused(self):
        with pytest.raises(kb.InvocationPlanError, match="non-empty"):
            kb.build_resume_capsule(decision=_continue_decision(), instruction="   ")

    def test_too_many_notes_are_refused(self):
        notes = [f"n{i}" for i in range(kb.RESUME_CAPSULE_MAX_NOTES + 1)]
        with pytest.raises(kb.InvocationPlanError, match="notes"):
            kb.build_resume_capsule(
                decision=_continue_decision(), instruction="x", notes=notes
            )

    def test_control_characters_in_identifiers_are_refused(self):
        """A newline in a session id could forge a second argument downstream."""
        with pytest.raises(kb.InvocationPlanError, match="control characters"):
            kb.render_resume_command(
                provider=kb.PROVIDER_CLAUDE_CODE, session_id="sess\n--dangerous"
            )

    def test_a_non_route_decision_object_is_refused(self):
        with pytest.raises(kb.InvocationPlanError, match="RouteDecision"):
            kb.plan_session_resume(
                decision={"route": "continue"},  # type: ignore[arg-type]
                binding=_binding(),
                capsule=kb.build_resume_capsule(
                    decision=_continue_decision(), instruction="x"
                ),
            )


class TestSliceResumePlanInertness:
    """The plan is a specification and stays one."""

    def test_the_plan_is_never_marked_executed(self):
        plan = _plan()
        assert plan.executed is False
        assert plan.command.executed is False

    def test_the_plan_always_demands_an_a3_gate(self):
        assert _plan().requires_a3_gate is True

    def test_the_plan_is_frozen(self):
        plan = _plan()
        with pytest.raises(dataclasses.FrozenInstanceError):
            plan.executed = True  # type: ignore[misc]

    def test_payload_round_trips_as_json(self):
        payload = _plan().to_payload()
        assert json.loads(json.dumps(payload))["executed"] is False
        assert payload["session_source"] == kb.SESSION_SOURCE_DISPATCHER

    def test_the_contract_section_contains_no_execution_path(self):
        """Source scan: the new API must not reach a process or the network."""
        import inspect

        for fn in (
            kb.plan_session_resume,
            kb.render_resume_command,
            kb.build_resume_capsule,
            kb.resume_stream_event_schema,
        ):
            source = inspect.getsource(fn)
            for forbidden in (
                "subprocess",
                "Popen",
                "os.system",
                "os.exec",
                "os.environ",
                "getenv",
                "socket",
                "urlopen",
                "requests.",
                "write_txn",
                "conn.execute",
                "open(",
            ):
                assert forbidden not in source, (
                    f"{fn.__name__} must not reference {forbidden!r}"
                )

    def test_planning_takes_no_connection(self):
        """No I/O dependency can sneak in through the signature."""
        import inspect

        for fn in (kb.plan_session_resume, kb.render_resume_command,
                   kb.build_resume_capsule):
            assert "conn" not in inspect.signature(fn).parameters

    def test_only_the_gated_adapter_path_consumes_a_plan(self):
        """Guarded interface: exactly one call site, and dispatch is untouched.

        This assertion was previously "no call site anywhere". The provider-
        adapter slice deliberately changed that: preparing a resume request *is*
        consuming a plan. The forged-plan repair adds one still-inert caller:
        the disabled adapter canonicalises a hand-constructed frozen plan before
        it can render argv. Rather than drop the guarantee, it is tightened —
        there must be exactly two callers: the binding/A3/fence entry point and
        that disabled adapter boundary. Any further call site fails this test.
        """
        import inspect

        source = inspect.getsource(kb)
        assert source.count("def plan_session_resume(") == 1
        call_sites = source.replace("def plan_session_resume(", "").count(
            "plan_session_resume("
        )
        assert call_sites == 2, f"expected exactly 2 call sites, found {call_sites}"
        assert "plan_session_resume(" in inspect.getsource(kb.prepare_resume_request)
        assert "plan_session_resume(" in inspect.getsource(
            kb.ClaudeCodeAdapter.build_command
        )
        # The dispatcher path must not have learned about any of this.
        dispatch_src = inspect.getsource(kb.dispatch_once)
        for name in ("plan_session_resume", "InvocationPlan", "render_resume_command",
                     "prepare_resume_request", "interpret_terminal_result"):
            assert name not in dispatch_src

    #: The core guarantee, unchanged since the first plan slice: a plan may be
    #: *constructed* and *described*, never *executed*. Consuming a plan to
    #: build an inert request is allowed; reaching an execution primitive is not.
    PLAN_CONSUMERS = (
        "plan_session_resume", "render_resume_command", "build_resume_capsule",
        "prepare_resume_request", "claim_execution_fence",
    )
    EXECUTION_PRIMITIVES = (
        "subprocess", "Popen", "socket", "urllib", "requests", "httpx",
        "os.system", "os.exec", "os.spawn", "pty.", "fork(", "asyncio",
        "crontab", "systemctl", "at -f", "os.environ", "getenv",
        "ANTHROPIC", "API_KEY", "credential",
    )

    def test_no_plan_consumer_reaches_an_execution_primitive(self):
        """The plan-handling path cannot spawn, connect, schedule, or read creds."""
        import inspect

        for name in self.PLAN_CONSUMERS:
            fn = getattr(kb, name)
            doc = fn.__doc__ or ""
            # Strip prose: a docstring naming "subprocess" must not decide this.
            code = inspect.getsource(fn).replace(doc, "")
            for token in self.EXECUTION_PRIMITIVES:
                assert token not in code, f"{name} reaches {token!r}"

    def test_a_constructed_plan_and_request_stay_unexecuted(self, db):
        """`executed` is hard-wired False on both the plan and the request."""
        _run_id, decision = _adapter_env(db)
        request = _prepare(db, decision)
        assert request.plan.executed is False
        assert request.plan.command.executed is False
        assert request.executed is False
        assert request.plan.requires_a3_gate is True
        # Frozen: nothing can flip it after the fact either.
        with pytest.raises(dataclasses.FrozenInstanceError):
            request.executed = True

    def test_a_plan_alone_cannot_reach_terminal_interpretation(self, db):
        """The only door to a canonical terminal write is a registered receipt."""
        _run_id, decision = _adapter_env(db)
        request = _prepare(db, decision)
        result = _ok_result(request.fence.run_id)
        # The request itself is not a receipt...
        for impostor in (request, result, None, request.plan):
            with pytest.raises(kb.UnsealedResultError):
                kb.interpret_terminal_result(
                    db, receipt=impostor, policy=kb.ExecutorPolicy(), now=NOW,
                )
        # ...and the task was never touched by any of those attempts.
        assert db.execute(
            "SELECT status FROM tasks WHERE id='t_a'"
        ).fetchone()["status"] == "running"

    def test_mutation_removing_the_continue_guard_is_caught(self):
        """The route guard is load-bearing, not decorative.

        Proves the rejection suite would actually notice if the CONTINUE check
        were removed: with the guard bypassed, a REVIEW decision would render a
        real resume command. The guard is what stops that.
        """
        review = _continue_decision(route=kb.ROUTE_REVIEW)
        # Use a directly constructed *valid* capsule.  That ensures this test
        # reaches the route guard itself; it would fail if that guard changed
        # to ``if False`` even though build_resume_capsule has its own guard.
        capsule = _raw_capsule()
        with pytest.raises(kb.InvocationPlanError):
            kb.plan_session_resume(
                decision=review, binding=_binding(), capsule=capsule,
            )
        # ...while the identical inputs under CONTINUE do produce a plan, so the
        # rejection above is attributable to the route, nothing else.
        assert _plan(decision=_continue_decision()).command.argv[0] == "claude"


# ---------------------------------------------------------------------------
# Executor boundary — source-only, default-off, fake transport only
# ---------------------------------------------------------------------------


class FakeExecutor:
    """Deterministic fake. Never spawns, connects, or resumes anything."""

    name = "fake"

    def __init__(self, result=None, raises=None, sleep_to=None):
        self.result = result
        self.raises = raises
        self.sleep_to = sleep_to
        self.calls = 0
        self.seen_timeouts: list[int] = []

    def execute(self, plan, *, timeout_seconds):
        self.calls += 1
        self.seen_timeouts.append(timeout_seconds)
        if self.raises is not None:
            raise self.raises
        return self.result


def _exec_env(db, *, session="wsess-1", now=1_700_000_500):
    """A real claimed CONTINUE decision plus a fresh dispatcher-owned binding."""
    # provider must match the binding's, or `_validate_binding` rejects the
    # pair before the executor is ever reached.
    _seed_task(db, "t_a", status="running", provider=kb.PROVIDER_CLAUDE_CODE)
    run_id = _seed_run(db, "t_a", outcome="crashed")
    kb.record_worker_session_provenance(
        db, run_id=run_id, worker_session_id=session,
        source=kb.SESSION_SOURCE_DISPATCHER,
    )
    with kb.write_txn(db):
        db.execute("UPDATE tasks SET current_run_id = ? WHERE id = 't_a'", (run_id,))
    decision = _fold_and_route(db, run_id)
    assert decision.route == kb.ROUTE_CONTINUE
    binding = kb.SessionBinding(
        provider=kb.PROVIDER_CLAUDE_CODE, session_id=session,
        source=kb.SESSION_SOURCE_DISPATCHER, seat_id="seat-a",
        issued_at=now - 60, expires_at=now + 600,
        owner=kb.DISPATCHER_BINDING_OWNER, retired=False,
    )
    capsule = kb.build_resume_capsule(decision=decision, instruction="Finish it.")
    plan = kb.plan_session_resume(decision=decision, binding=binding, capsule=capsule)
    return run_id, plan, binding, now


def _ok_result(run_id, status="completed", summary="done"):
    return {"status": status, "summary": summary, "run_id": run_id}


class TestExecutorOrdering:
    """(1) execution happens after a committed claim and outside write_txn."""

    def test_the_claim_is_committed_before_the_transport_is_called(self, db):
        run_id, plan, binding, now = _exec_env(db)
        seen: dict = {}

        class ProbingExecutor(FakeExecutor):
            def execute(self, plan, *, timeout_seconds):
                probe = sqlite3.connect(db_path_of(db), timeout=30)
                probe.row_factory = sqlite3.Row
                try:
                    seen["claim_visible"] = probe.execute(
                        "SELECT COUNT(*) c FROM task_events WHERE kind = ?",
                        (kb.EXEC_EVENT_CLAIMED,),
                    ).fetchone()["c"]
                finally:
                    probe.close()
                return _ok_result(run_id)

        ex = ProbingExecutor()
        kb.execute_planned_resume(
            db, plan=plan, binding=binding, executor=ex,
            policy=kb.ExecutorPolicy(fake_executors=[ex]), now=now,
        )
        assert seen["claim_visible"] == 1, (
            "the claim must be committed and visible to another connection "
            "before the transport runs"
        )

    def test_no_write_transaction_is_held_during_execution(self, db):
        """A separate connection must be able to WRITE while we execute."""
        run_id, plan, binding, now = _exec_env(db)
        wrote: dict = {}

        class ProbingExecutor(FakeExecutor):
            def execute(self, plan, *, timeout_seconds):
                other = sqlite3.connect(db_path_of(db), timeout=10)
                try:
                    other.execute(
                        "INSERT INTO task_events (task_id, run_id, kind, payload,"
                        " created_at) VALUES ('t_a', NULL, 'commented', NULL, 1)"
                    )
                    other.commit()
                    wrote["ok"] = True
                finally:
                    other.close()
                return _ok_result(run_id)

        ex = ProbingExecutor()
        kb.execute_planned_resume(
            db, plan=plan, binding=binding, executor=ex,
            policy=kb.ExecutorPolicy(fake_executors=[ex]), now=now,
        )
        assert wrote.get("ok"), "a writer must not be blocked during execution"


class TestExecutorIdempotency:
    """(2) redelivery cannot execute twice."""

    def test_a_second_execution_of_the_same_run_is_refused(self, db):
        run_id, plan, binding, now = _exec_env(db)
        ex = FakeExecutor(result=_ok_result(run_id))
        policy = kb.ExecutorPolicy(fake_executors=[ex])
        kb.execute_planned_resume(
            db, plan=plan, binding=binding, executor=ex, policy=policy, now=now
        )
        assert ex.calls == 1

        with pytest.raises(kb.DuplicateExecutionError):
            kb.execute_planned_resume(
                db, plan=plan, binding=binding, executor=ex, policy=policy, now=now
            )
        assert ex.calls == 1, "the transport must not run a second time"

    def test_the_claim_index_is_the_idempotency_key(self, db):
        assert kb.execution_claim_index_present(db)
        row = db.execute(
            "SELECT sql FROM sqlite_master WHERE name = ?",
            (kb.EXECUTION_CLAIM_INDEX,),
        ).fetchone()
        assert "unique" in row["sql"].lower()
        assert kb.EXEC_EVENT_CLAIMED in row["sql"]

    def test_claiming_refuses_when_the_index_is_absent(self, db):
        _run_id, plan, _binding, _now = _exec_env(db)
        with kb.write_txn(db):
            db.execute(f"DROP INDEX {kb.EXECUTION_CLAIM_INDEX}")
        with pytest.raises(kb.ExecutionNotPermitted, match="idempotency"):
            kb.claim_execution_fence(db, plan=plan)


class TestExecutorBindingFreshness:
    """(3) eligible, fresh, non-retired, dispatcher-owned — at execution time."""

    @pytest.mark.parametrize(
        "override,match",
        [
            ({"retired": True}, "retired"),
            ({"owner": "someone-else"}, "not 'hermes-dispatcher'"),
            ({"owner": ""}, "not 'hermes-dispatcher'"),
            ({"source": kb.SESSION_SOURCE_INFERRED}, "not continue-eligible"),
            ({"issued_at": 0}, "issued_at"),
            ({"expires_at": 0}, "expires_at"),
        ],
    )
    def test_an_unfit_binding_is_refused(self, db, override, match):
        run_id, plan, binding, now = _exec_env(db)
        bad = dataclasses.replace(binding, **override)
        ex = FakeExecutor(result=_ok_result(run_id))
        with pytest.raises(kb.BindingNotFreshError, match=match):
            kb.execute_planned_resume(
                db, plan=plan, binding=bad, executor=ex,
                policy=kb.ExecutorPolicy(fake_executors=[ex]), now=now,
            )
        assert ex.calls == 0, "no execution on an unfit binding"

    def test_a_binding_that_expires_between_plan_and_execution_is_refused(self, db):
        run_id, plan, binding, now = _exec_env(db)
        ex = FakeExecutor(result=_ok_result(run_id))
        later = binding.expires_at + 1
        with pytest.raises(kb.BindingNotFreshError, match="expired"):
            kb.execute_planned_resume(
                db, plan=plan, binding=binding, executor=ex,
                policy=kb.ExecutorPolicy(fake_executors=[ex]), now=later,
            )
        assert ex.calls == 0

    def test_a_future_dated_binding_is_refused(self, db):
        run_id, plan, binding, now = _exec_env(db)
        ex = FakeExecutor(result=_ok_result(run_id))
        with pytest.raises(kb.BindingNotFreshError, match="future"):
            kb.execute_planned_resume(
                db, plan=plan, binding=binding, executor=ex,
                policy=kb.ExecutorPolicy(fake_executors=[ex]),
                now=binding.issued_at - 1,
            )
        assert ex.calls == 0

    def test_freshness_is_not_derived_from_worker_heartbeat(self, db):
        """A heartbeating worker must not keep a retired mapping alive."""
        run_id, plan, binding, now = _exec_env(db)
        with kb.write_txn(db):
            db.execute(
                "UPDATE task_runs SET last_heartbeat_at = ? WHERE id = ?",
                (now, run_id),
            )
        retired = dataclasses.replace(binding, retired=True)
        ex = FakeExecutor(result=_ok_result(run_id))
        with pytest.raises(kb.BindingNotFreshError, match="retired"):
            kb.execute_planned_resume(
                db, plan=plan, binding=retired, executor=ex,
                policy=kb.ExecutorPolicy(fake_executors=[ex]), now=now,
            )

    def test_a_binding_for_another_session_is_refused(self, db):
        run_id, plan, binding, now = _exec_env(db)
        other = dataclasses.replace(binding, session_id="somebody-else")
        ex = FakeExecutor(result=_ok_result(run_id))
        with pytest.raises(kb.BindingNotFreshError, match="does not match"):
            kb.execute_planned_resume(
                db, plan=plan, binding=other, executor=ex,
                policy=kb.ExecutorPolicy(fake_executors=[ex]), now=now,
            )
        assert ex.calls == 0

    def test_default_binding_fields_are_not_a_valid_window(self):
        plain = kb.SessionBinding(
            provider=kb.PROVIDER_CLAUDE_CODE, session_id="s",
            source=kb.SESSION_SOURCE_DISPATCHER,
        )
        with pytest.raises(kb.BindingNotFreshError):
            kb.validate_binding_freshness(plain, now=1_700_000_000)


class TestExecutorKillGate:
    """(4) one authoritative kill/A3 check immediately before execution."""

    def test_an_unregistered_executor_is_refused(self, db):
        run_id, plan, binding, now = _exec_env(db)
        ex = FakeExecutor(result=_ok_result(run_id))
        with pytest.raises(kb.ExecutionNotPermitted, match="not a registered fake"):
            kb.execute_planned_resume(
                db, plan=plan, binding=binding, executor=ex, now=now
            )
        assert ex.calls == 0

    def test_a_liar_claiming_to_be_a_fake_is_refused(self, db):
        run_id, plan, binding, now = _exec_env(db)

        class Liar(FakeExecutor):
            name = "totally-a-fake"
            simulated = True

        ex = Liar(result=_ok_result(run_id))
        with pytest.raises(kb.ExecutionNotPermitted):
            kb.execute_planned_resume(
                db, plan=plan, binding=binding, executor=ex, now=now
            )
        assert ex.calls == 0

    def test_registration_is_by_identity_not_name(self, db):
        run_id, plan, binding, now = _exec_env(db)
        registered = FakeExecutor(result=_ok_result(run_id))
        impostor = FakeExecutor(result=_ok_result(run_id))
        impostor.name = registered.name
        policy = kb.ExecutorPolicy(fake_executors=[registered])
        with pytest.raises(kb.ExecutionNotPermitted):
            kb.execute_planned_resume(
                db, plan=plan, binding=binding, executor=impostor,
                policy=policy, now=now,
            )
        assert impostor.calls == 0

    def test_a_latched_a3_revocation_stops_execution(self, db):
        run_id, plan, binding, now = _exec_env(db)
        kb.latch_a3_revocation(db, task_id="t_a", reason="kill")
        ex = FakeExecutor(result=_ok_result(run_id))
        with pytest.raises(kb.ExecutionNotPermitted, match="revocation is latched"):
            kb.execute_planned_resume(
                db, plan=plan, binding=binding, executor=ex,
                policy=kb.ExecutorPolicy(fake_executors=[ex]), now=now,
            )
        assert ex.calls == 0, "the kill check must precede the call"

    def test_a_real_executor_needs_allow_flag_and_a3_gate(self, db):
        run_id, plan, binding, now = _exec_env(db)
        ex = FakeExecutor(result=_ok_result(run_id))
        with pytest.raises(kb.ExecutionNotPermitted, match="A3 gate"):
            kb.execute_planned_resume(
                db, plan=plan, binding=binding, executor=ex,
                policy=kb.ExecutorPolicy(allow_real_execution=True), now=now,
            )
        assert ex.calls == 0


class TestExecutorResultMapping:
    """(5) bounded timeout + strict result through canonical APIs."""

    def test_completed_closes_the_task_through_the_canonical_api(self, db):
        run_id, plan, binding, now = _exec_env(db)
        ex = FakeExecutor(result=_ok_result(run_id, "completed", "all done"))
        outcome = kb.execute_planned_resume(
            db, plan=plan, binding=binding, executor=ex,
            policy=kb.ExecutorPolicy(fake_executors=[ex]), now=now,
        )
        assert outcome.route == kb.ROUTE_CLOSE
        assert outcome.terminal_write is True
        assert outcome.executed_against_real_provider is False
        status = db.execute("SELECT status FROM tasks WHERE id='t_a'").fetchone()
        assert status["status"] == "done"

    def test_blocked_blocks_the_task_through_the_canonical_api(self, db):
        run_id, plan, binding, now = _exec_env(db)
        ex = FakeExecutor(result=_ok_result(run_id, "blocked", "needs a decision"))
        outcome = kb.execute_planned_resume(
            db, plan=plan, binding=binding, executor=ex,
            policy=kb.ExecutorPolicy(fake_executors=[ex]), now=now,
        )
        assert outcome.route == kb.ROUTE_BLOCK and outcome.terminal_write is True
        status = db.execute("SELECT status FROM tasks WHERE id='t_a'").fetchone()
        assert status["status"] == "blocked"

    @pytest.mark.parametrize(
        "status,route", [("needs_review", kb.ROUTE_REVIEW),
                         ("incomplete", kb.ROUTE_CONTINUE)],
    )
    def test_review_and_continue_write_no_task_status(self, db, status, route):
        """A verdict and a re-drive are not the executor's to make."""
        run_id, plan, binding, now = _exec_env(db)
        before = db.execute("SELECT status FROM tasks WHERE id='t_a'").fetchone()[0]
        ex = FakeExecutor(result=_ok_result(run_id, status, "partial"))
        outcome = kb.execute_planned_resume(
            db, plan=plan, binding=binding, executor=ex,
            policy=kb.ExecutorPolicy(fake_executors=[ex]), now=now,
        )
        assert outcome.route == route and outcome.terminal_write is False
        after = db.execute("SELECT status FROM tasks WHERE id='t_a'").fetchone()[0]
        assert after == before

    def test_the_notification_projection_is_produced(self, db):
        run_id, plan, binding, now = _exec_env(db)
        ex = FakeExecutor(result=_ok_result(run_id))
        outcome = kb.execute_planned_resume(
            db, plan=plan, binding=binding, executor=ex,
            policy=kb.ExecutorPolicy(fake_executors=[ex]), now=now,
        )
        assert outcome.notification.spawn is False
        assert outcome.notification.route == kb.ROUTE_CLOSE
        assert "execution_completed" in outcome.notification.reason

    def test_the_transport_receives_the_bounded_timeout(self, db):
        run_id, plan, binding, now = _exec_env(db)
        ex = FakeExecutor(result=_ok_result(run_id))
        kb.execute_planned_resume(
            db, plan=plan, binding=binding, executor=ex,
            policy=kb.ExecutorPolicy(fake_executors=[ex]), now=now,
        )
        assert ex.seen_timeouts == [plan.command.timeout_seconds]
        assert 0 < plan.command.timeout_seconds <= kb.RESUME_MAX_TIMEOUT_SECONDS


class TestExecutorFailClosed:
    """(6) every failure mode: no terminal write, no second execution."""

    def _run(self, db, ex, now, plan, binding):
        return kb.execute_planned_resume(
            db, plan=plan, binding=binding, executor=ex,
            policy=kb.ExecutorPolicy(fake_executors=[ex]), now=now,
        )

    @pytest.mark.parametrize(
        "bad",
        [
            None, [], "done", 5,
            {"status": "completed", "summary": "s"},
            {"status": "completed", "run_id": 1},
            {"summary": "s", "run_id": 1},
            {"status": "nonsense", "summary": "s", "run_id": 1},
            {"status": "completed", "summary": "", "run_id": 1},
            {"status": "completed", "summary": "s", "run_id": "1"},
            {"status": "completed", "summary": "s", "run_id": True},
            {"status": "completed", "summary": "bad\nline", "run_id": 1},
            {"status": "completed", "summary": "s", "run_id": 1, "extra": 1},
        ],
    )
    def test_a_malformed_result_is_refused_without_a_terminal_write(self, db, bad):
        run_id, plan, binding, now = _exec_env(db)
        # `True == 1` in Python, so compare the *type* too — otherwise the
        # bool case is silently rewritten into a valid run id and stops
        # testing anything.
        placeholder = isinstance(bad, dict) and type(bad.get("run_id")) is int
        if placeholder and bad["run_id"] == 1:
            bad = {**bad, "run_id": run_id}
        ex = FakeExecutor(result=bad)
        with pytest.raises(kb.ExecutionResultInvalid):
            self._run(db, ex, now, plan, binding)
        assert db.execute(
            "SELECT status FROM tasks WHERE id='t_a'"
        ).fetchone()["status"] == "running"

    def test_a_result_for_another_run_is_refused(self, db):
        run_id, plan, binding, now = _exec_env(db)
        ex = FakeExecutor(result=_ok_result(run_id + 999))
        with pytest.raises(kb.ExecutionResultInvalid, match="does not match"):
            self._run(db, ex, now, plan, binding)

    def test_an_unavailable_executor_is_refused(self, db):
        _run_id, plan, binding, now = _exec_env(db)
        ex = FakeExecutor(raises=OSError("no such session"))
        with pytest.raises(kb.ExecutorUnavailableError):
            self._run(db, ex, now, plan, binding)
        assert db.execute(
            "SELECT status FROM tasks WHERE id='t_a'"
        ).fetchone()["status"] == "running"

    def test_a_timeout_is_refused(self, db):
        _run_id, plan, binding, now = _exec_env(db)
        ex = FakeExecutor(raises=TimeoutError("too slow"))
        with pytest.raises(kb.ExecutionTimeoutError):
            self._run(db, ex, now, plan, binding)

    def test_an_overrunning_executor_is_refused_even_if_it_returns(self, db):
        """Wall-clock bound, not merely a cooperative transport."""
        run_id, plan, binding, now = _exec_env(db)
        ex = FakeExecutor(result=_ok_result(run_id))
        ticks = iter([0.0, float(plan.command.timeout_seconds) + 1.0])
        with pytest.raises(kb.ExecutionTimeoutError, match="exceeding"):
            kb.execute_planned_resume(
                db, plan=plan, binding=binding, executor=ex,
                policy=kb.ExecutorPolicy(fake_executors=[ex]), now=now,
                monotonic=lambda: next(ticks),
            )
        assert db.execute(
            "SELECT status FROM tasks WHERE id='t_a'"
        ).fetchone()["status"] == "running"

    def test_a_moved_fence_discards_the_result(self, db):
        run_id, plan, binding, now = _exec_env(db)

        class MovingExecutor(FakeExecutor):
            def execute(self, plan, *, timeout_seconds):
                other = sqlite3.connect(db_path_of(db), timeout=10)
                try:
                    other.execute(
                        "UPDATE tasks SET current_run_id = 999999 WHERE id='t_a'"
                    )
                    other.commit()
                finally:
                    other.close()
                return _ok_result(run_id)

        ex = MovingExecutor()
        with pytest.raises(kb.ExecutionFenceLost, match="fence moved"):
            self._run(db, ex, now, plan, binding)
        assert db.execute(
            "SELECT status FROM tasks WHERE id='t_a'"
        ).fetchone()["status"] == "running"
        assert db.execute(
            "SELECT COUNT(*) c FROM task_events WHERE kind = ?",
            (kb.EXEC_EVENT_DISCARDED,),
        ).fetchone()["c"] == 1

    def test_the_terminal_write_is_cas_guarded_independently_of_the_fence(
        self, db, monkeypatch
    ):
        """`expected_run_id` is a *second*, independent guard — prove it alone.

        The fence recheck cannot share a transaction with the terminal write:
        `write_txn` issues a bare BEGIN IMMEDIATE and `complete_task` opens its
        own, so nesting them raises. The recheck therefore commits first and a
        real seam exists between it and the terminal write; the CAS is the only
        thing closing that seam. Blinding `_fence_intact` removes the outer
        guard so the CAS has to refuse on its own, which is exactly what a
        mutant dropping `expected_run_id` would break.
        """
        run_id, plan, binding, now = _exec_env(db)

        class MovingExecutor(FakeExecutor):
            def execute(self, plan, *, timeout_seconds):
                other = sqlite3.connect(db_path_of(db), timeout=10)
                try:
                    other.execute(
                        "UPDATE tasks SET current_run_id = 999999 WHERE id='t_a'"
                    )
                    other.commit()
                finally:
                    other.close()
                return _ok_result(run_id)

        # Blind ONLY the fence recheck. Everything else stays real.
        monkeypatch.setattr(kb, "_fence_intact", lambda conn, fence: None)
        ex = MovingExecutor()
        outcome = self._run(db, ex, now, plan, binding)
        assert outcome.route == kb.ROUTE_CLOSE
        # The CAS refused the stale write, so no terminal write landed...
        assert outcome.terminal_write is False
        # ...and the task is untouched.
        assert db.execute(
            "SELECT status FROM tasks WHERE id='t_a'"
        ).fetchone()["status"] == "running"

    def test_every_refusal_records_a_refused_event(self, db):
        _run_id, plan, binding, now = _exec_env(db)
        ex = FakeExecutor(raises=OSError("boom"))
        with pytest.raises(kb.ExecutorUnavailableError):
            self._run(db, ex, now, plan, binding)
        assert db.execute(
            "SELECT COUNT(*) c FROM task_events WHERE kind = ?",
            (kb.EXEC_EVENT_REFUSED,),
        ).fetchone()["c"] == 1

    def test_a_non_continue_decision_cannot_be_executed(self, db):
        _seed_task(db, "t_b", status="done")
        run_id = _seed_run(db, "t_b", outcome="completed")
        decision = _fold_and_route(db, run_id)
        assert decision.route == kb.ROUTE_CLOSE
        fake_plan = types.SimpleNamespace(decision=decision)
        with pytest.raises(kb.ExecutionNotPermitted, match="only a 'continue'"):
            kb.claim_execution_fence(db, plan=fake_plan)

    def test_a_refused_execution_still_consumed_its_claim(self, db):
        """The claim is durable: a failure cannot be retried into an execution."""
        run_id, plan, binding, now = _exec_env(db)
        ex = FakeExecutor(raises=OSError("boom"))
        with pytest.raises(kb.ExecutorUnavailableError):
            self._run(db, ex, now, plan, binding)
        good = FakeExecutor(result=_ok_result(run_id))
        with pytest.raises(kb.DuplicateExecutionError):
            self._run(db, good, now, plan, binding)
        assert good.calls == 0


class TestExecutorInertness:
    """No provider, subprocess, network, scheduler, or credential access."""

    def test_the_module_executor_path_contains_no_execution_primitive(self):
        import inspect

        for fn in (kb.execute_planned_resume, kb.claim_execution_fence,
                   kb.validate_binding_freshness, kb._validate_execution_result):
            # Strip the docstring: it *describes* what is not done ("opens a
            # socket", "reads a credential"), and matching prose would make
            # this assert on wording rather than on code.
            src = inspect.getsource(fn)
            doc = fn.__doc__ or ""
            code = src.replace(doc, "")
            for forbidden in ("subprocess", "Popen", "os.exec", "socket.",
                              "urllib", "requests.", "os.environ", "getenv"):
                assert forbidden not in code, f"{fn.__name__} references {forbidden}"

    def test_no_real_provider_is_ever_marked_executed(self, db):
        run_id, plan, binding, now = _exec_env(db)
        ex = FakeExecutor(result=_ok_result(run_id))
        outcome = kb.execute_planned_resume(
            db, plan=plan, binding=binding, executor=ex,
            policy=kb.ExecutorPolicy(fake_executors=[ex]), now=now,
        )
        assert outcome.executed_against_real_provider is False
        assert plan.command.executed is False

    def test_the_default_policy_permits_nothing(self):
        policy = kb.ExecutorPolicy()
        assert policy.allow_real_execution is False
        assert policy.is_registered_fake(FakeExecutor()) is False


def db_path_of(conn) -> str:
    """Filesystem path behind an open connection (for second-connection probes)."""
    row = conn.execute("PRAGMA database_list").fetchone()
    return row[2]


# ---------------------------------------------------------------------------
# Provider-adapter slice — persisted mapping, request preparation, sealed
# terminal results. Source-only; fake deterministic adapter only.
# ---------------------------------------------------------------------------


class FakeAdapter:
    """Deterministic fake. Never spawns, connects, or resumes anything."""

    name = "fake-adapter"

    def __init__(self, result=None):
        self.result = result
        self.calls = 0

    def execute(self, plan, *, timeout_seconds):
        self.calls += 1
        return self.result


NOW = 1_700_000_500


def _bind(db, run_id, *, session="wsess-1", task_id="t_a",
          provider=None, source=None, owner=kb.DISPATCHER_BINDING_OWNER,
          issued_at=NOW - 60, expires_at=NOW + 600):
    kb.record_session_binding(
        db, run_id=run_id, task_id=task_id,
        provider=provider or kb.PROVIDER_CLAUDE_CODE,
        session_id=session, source=source or kb.SESSION_SOURCE_DISPATCHER,
        owner=owner, issued_at=issued_at, expires_at=expires_at, now=NOW,
    )


def _adapter_env(db, *, session="wsess-1", bind=True, **bind_kwargs):
    """A real claimed CONTINUE decision plus a PERSISTED dispatcher mapping."""
    _seed_task(db, "t_a", status="running", provider=kb.PROVIDER_CLAUDE_CODE)
    run_id = _seed_run(db, "t_a", outcome="crashed")
    kb.record_worker_session_provenance(
        db, run_id=run_id, worker_session_id=session,
        source=kb.SESSION_SOURCE_DISPATCHER,
    )
    with kb.write_txn(db):
        db.execute("UPDATE tasks SET current_run_id = ? WHERE id = 't_a'", (run_id,))
    decision = _fold_and_route(db, run_id)
    assert decision.route == kb.ROUTE_CONTINUE
    if bind:
        _bind(db, run_id, session=session, **bind_kwargs)
    return run_id, decision


def _prepare(db, decision, **kw):
    kw.setdefault("instruction", "Finish it.")
    kw.setdefault("now", NOW)
    return kb.prepare_resume_request(db, decision=decision, **kw)


def _sealed(db, request, result, adapter=None, policy=None):
    adapter = adapter or FakeAdapter(result)
    policy = policy or kb.ExecutorPolicy(fake_executors=[adapter])
    receipt = kb.seal_adapter_result(
        db, adapter=adapter, request=request, result=result,
        policy=policy, now=NOW,
    )
    return receipt, policy


def _live_execution_run(db, *, task_id="t_a", claimer="executor-1"):
    """Attach one real-executor lease to the still-running task, in test only."""
    with kb.write_txn(db):
        cur = db.execute(
            "INSERT INTO task_runs (task_id, status, started_at, claim_expires) "
            "VALUES (?, 'running', ?, ?)",
            (task_id, NOW, NOW + 600),
        )
        run_id = int(cur.lastrowid)
        db.execute(
            "UPDATE tasks SET claim_lock=?, claim_expires=?, current_run_id=? "
            "WHERE id=?",
            (claimer, NOW + 600, run_id, task_id),
        )
    return run_id


def _grant_a3(db, task_id="t_a"):
    with kb.write_txn(db):
        db.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) "
            "VALUES (?, 'operator', 'A3_GATE=GRANTED', ?)",
            (task_id, NOW),
        )


class RecordingClaudeRunner:
    def __init__(self, events, *, db=None, lose_claim=False, before_return=None):
        self.events = events
        self.db = db
        self.lose_claim = lose_claim
        self.before_return = before_return
        self.calls = []

    def run(self, **kwargs):
        self.calls.append(kwargs)
        kwargs["heartbeat"]()
        if self.lose_claim:
            # Deliberately adversarial test runner. Production runners receive
            # no DB connection and cannot alter native task state.
            with kb.write_txn(self.db):
                self.db.execute("UPDATE tasks SET claim_lock='other' WHERE id='t_a'")
        kwargs["heartbeat"]()
        if self.before_return is not None:
            self.before_return()
        return list(self.events)


def _claude_success_events(session="wsess-1"):
    return [
        {"type": "system", "subtype": "init", "session_id": session},
        {"type": "assistant", "session_id": session},
        {"type": "result", "subtype": "success", "session_id": session,
         "is_error": False, "result": "canary complete"},
    ]


class TestClaudeResumeExecutor:
    """The concrete edge is default-off and delegates all authority to Hermes."""

    def _request(self, db):
        _decision_run, decision = _adapter_env(db)
        live_run = _live_execution_run(db)
        request = _prepare(db, decision)
        assert request.fence.current_run_id == live_run
        _grant_a3(db)
        return request, live_run

    @pytest.mark.parametrize("interval", [0, -1, 101, float("inf"), float("nan")])
    def test_rejects_heartbeat_cadence_that_cannot_protect_the_explicit_ttl(self, interval):
        with pytest.raises(ValueError, match="heartbeat_interval_seconds"):
            ClaudeResumeExecutor(
                heartbeat_interval_seconds=interval, claim_ttl_seconds=300
            )

    def test_heartbeat_cadence_is_bounded_by_the_explicit_ttl(self):
        executor = ClaudeResumeExecutor(
            heartbeat_interval_seconds=100, claim_ttl_seconds=300
        )
        assert executor.heartbeat_interval_seconds == 100
        assert executor.claim_ttl_seconds == 300

    def test_default_disabled_never_invokes_the_process_runner(self, db, tmp_path):
        request, _live_run = self._request(db)
        runner = RecordingClaudeRunner(_claude_success_events())
        executor = ClaudeResumeExecutor(runner=runner)
        with pytest.raises(kb.ExecutorError, match="disabled"):
            executor.execute(
                db, request=request, claimer="executor-1", workspace=tmp_path,
                policy=kb.ExecutorPolicy(allow_real_execution=True), now=NOW,
            )
        assert runner.calls == []
        assert _task_row(db, "t_a")["status"] == "running"

    def test_armed_executor_refuses_a_workspace_outside_declared_task_root(self, db, tmp_path):
        request, _live_run = self._request(db)
        runner = RecordingClaudeRunner(_claude_success_events())
        wrong_workspace = tmp_path / "wrong"
        wrong_workspace.mkdir()
        executor = ClaudeResumeExecutor(
            armed=True, runner=runner, workspace_root=tmp_path
        )
        with pytest.raises(kb.ExecutionNotPermitted, match="declared task workspace"):
            executor.execute(
                db, request=request, claimer="executor-1", workspace=wrong_workspace,
                policy=kb.ExecutorPolicy(allow_real_execution=True), now=NOW,
            )
        assert runner.calls == []

    def test_armed_executor_uses_only_canonical_resume_jsonl_and_native_terminal_path(self, db, tmp_path):
        request, live_run = self._request(db)
        runner = RecordingClaudeRunner(_claude_success_events())
        workspace = tmp_path / "t_a"
        workspace.mkdir()
        executor = ClaudeResumeExecutor(
            armed=True, runner=runner, heartbeat_interval_seconds=1,
            workspace_root=tmp_path,
        )
        result = executor.execute(
            db, request=request, claimer="executor-1", workspace=workspace,
            policy=kb.ExecutorPolicy(allow_real_execution=True), now=NOW,
        )
        assert result.status == "completed"
        assert result.terminal_write is True
        assert _task_row(db, "t_a")["status"] == "done"
        assert len(runner.calls) == 1
        call = runner.calls[0]
        assert tuple(call["argv"]) == request.plan.command.argv
        assert "--resume" in call["argv"] and "--continue" not in call["argv"]
        assert call["input_jsonl"] == kb.ClaudeCodeAdapter().build_command(
            request.plan
        ).input_jsonl
        assert call["input_jsonl"].endswith("\n")
        assert db.execute("SELECT status FROM task_runs WHERE id=?", (live_run,)).fetchone()["status"] == "done"

    def test_lost_run_bound_lease_aborts_before_terminal_write(self, db, tmp_path):
        request, _live_run = self._request(db)
        runner = RecordingClaudeRunner(_claude_success_events(), db=db, lose_claim=True)
        workspace = tmp_path / "t_a"
        workspace.mkdir()
        executor = ClaudeResumeExecutor(armed=True, runner=runner, workspace_root=tmp_path)
        with pytest.raises(kb.ClaimLeaseLost):
            executor.execute(
                db, request=request, claimer="executor-1", workspace=workspace,
                policy=kb.ExecutorPolicy(allow_real_execution=True), now=NOW,
            )
        assert _task_row(db, "t_a")["status"] == "running"
        assert db.execute(
            "SELECT COUNT(*) AS c FROM task_events WHERE kind=?", (kb.EXEC_EVENT_COMPLETED,)
        ).fetchone()["c"] == 0

    def test_terminal_a3_revocation_after_launch_prevents_terminal_write(self, db, tmp_path):
        request, _live_run = self._request(db)
        runner = RecordingClaudeRunner(
            _claude_success_events(),
            before_return=lambda: kb.latch_a3_revocation(
                db, task_id="t_a", reason="canary stop"
            ),
        )
        workspace = tmp_path / "t_a"
        workspace.mkdir()
        executor = ClaudeResumeExecutor(armed=True, runner=runner, workspace_root=tmp_path)
        with pytest.raises(kb.ExecutionNotPermitted, match="positive A3 gate"):
            executor.execute(
                db, request=request, claimer="executor-1", workspace=workspace,
                policy=kb.ExecutorPolicy(allow_real_execution=True), now=NOW,
            )
        assert _task_row(db, "t_a")["status"] == "running"
        assert db.execute(
            "SELECT COUNT(*) AS c FROM task_events WHERE kind=?", (kb.EXEC_EVENT_COMPLETED,)
        ).fetchone()["c"] == 0


def test_subprocess_runner_signals_child_group_even_when_parent_already_exited(tmp_path, monkeypatch):
    class ExitedParent:
        pid = 4321
        returncode = 1
        stdin = io.StringIO()
        stdout = io.StringIO("not-json\n")

        def poll(self):
            return 1

        def wait(self, timeout):  # pragma: no cover - must not be needed
            raise AssertionError("exited parent should not be waited on")

    parent = ExitedParent()
    signals = []
    monkeypatch.setattr(ce.subprocess, "Popen", lambda *a, **kw: parent)
    monkeypatch.setattr(ce.select, "select", lambda *a, **kw: ([], [], []))
    monkeypatch.setattr(ce.os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    with pytest.raises(ce.ClaudeExecutorProtocolError, match="trailing"):
        ce.SubprocessClaudeRunner().run(
            argv=("claude", "--resume", "s"), input_jsonl="{}\n", cwd=tmp_path,
            timeout_seconds=30, heartbeat=lambda: None, heartbeat_interval_seconds=1,
        )
    assert signals == [(4321, ce.signal.SIGTERM)]


def test_subprocess_runner_keeps_stderr_out_of_structured_stdout(tmp_path, monkeypatch):
    """Provider diagnostics must not make a valid stream look malformed."""

    class CleanProcess:
        pid = 9876
        returncode = 0
        stdin = io.StringIO()
        stdout = io.StringIO('{"type":"system","subtype":"init"}\n')

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    parent = CleanProcess()
    seen = {}

    def fake_popen(*args, **kwargs):
        seen["stderr"] = kwargs["stderr"]
        kwargs["stderr"].write("ordinary provider diagnostic\n")
        return parent

    monkeypatch.setattr(ce.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(ce.select, "select", lambda *a, **kw: ([parent.stdout], [], []))

    events = ce.SubprocessClaudeRunner().run(
        argv=("claude", "--resume", "s"), input_jsonl="{}\n", cwd=tmp_path,
        timeout_seconds=30, heartbeat=lambda: None, heartbeat_interval_seconds=1,
    )

    assert events == [{"type": "system", "subtype": "init"}]
    assert seen["stderr"] is not ce.subprocess.STDOUT


def test_subprocess_runner_bounds_stderr_diagnostic_on_provider_failure(tmp_path, monkeypatch):
    class FailedProcess:
        pid = 9877
        returncode = 17
        stdin = io.StringIO()
        stdout = io.StringIO()

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode

    parent = FailedProcess()

    def fake_popen(*args, **kwargs):
        kwargs["stderr"].write("BEGIN-" + ("x" * 1_100) + "-END")
        return parent

    monkeypatch.setattr(ce.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(ce.select, "select", lambda *a, **kw: ([], [], []))
    monkeypatch.setattr(ce.os, "killpg", lambda *a, **kw: None)

    with pytest.raises(kb.ExecutorUnavailableError) as excinfo:
        ce.SubprocessClaudeRunner().run(
            argv=("claude", "--resume", "s"), input_jsonl="{}\n", cwd=tmp_path,
            timeout_seconds=30, heartbeat=lambda: None, heartbeat_interval_seconds=1,
        )

    message = str(excinfo.value)
    assert "status 17" in message
    assert "BEGIN-" not in message
    assert message.endswith("-END")
    assert len(message.rsplit(": ", 1)[-1]) <= 1_000


def test_subprocess_runner_registers_process_before_it_accepts_stream(tmp_path, monkeypatch):
    class CleanProcess:
        pid = 24680
        returncode = 0
        stdin = io.StringIO()
        stdout = io.StringIO('{"type":"system","subtype":"init"}\n')

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    parent = CleanProcess()
    seen = []
    monkeypatch.setattr(ce.subprocess, "Popen", lambda *a, **kw: parent)
    monkeypatch.setattr(ce.select, "select", lambda *a, **kw: ([parent.stdout], [], []))

    events = ce.SubprocessClaudeRunner().run(
        argv=("claude", "--resume", "s"), input_jsonl="{}\n", cwd=tmp_path,
        timeout_seconds=30, heartbeat=lambda: None, heartbeat_interval_seconds=1,
        on_process_started=seen.append,
    )

    assert seen == [24680]
    assert events == [{"type": "system", "subtype": "init"}]


def test_register_claim_process_requires_exact_live_run(db):
    _seed_task(db, "t_register", status="ready")
    claimed = kb.claim_task(db, "t_register", claimer="executor:one", ttl_seconds=60)
    assert claimed is not None and claimed.current_run_id is not None

    kb.register_claim_process(
        db, "t_register", claimer="executor:one",
        expected_run_id=claimed.current_run_id, pid=24681,
    )
    task = db.execute("SELECT worker_pid FROM tasks WHERE id='t_register'").fetchone()
    run = db.execute("SELECT worker_pid FROM task_runs WHERE id=?", (claimed.current_run_id,)).fetchone()
    assert task["worker_pid"] == run["worker_pid"] == 24681
    with pytest.raises(kb.ClaimLeaseLost):
        kb.register_claim_process(
            db, "t_register", claimer="foreign",
            expected_run_id=claimed.current_run_id, pid=24682,
        )


def test_reclaim_causes_registered_executor_heartbeat_to_abort_process_group(db, tmp_path, monkeypatch):
    """A reclaimed live run cannot accept a result or leave its group running."""

    class HungProcess:
        pid = 24683
        returncode = None
        stdin = io.StringIO()
        stdout = io.StringIO()

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            self.returncode = 0
            return self.returncode

    task_id = kb.create_task(
        db, title="reclaim abort", provider_override=kb.PROVIDER_CLAUDE_CODE,
        model_override="test", initial_status="blocked",
    )
    assert kb.unblock_task(db, task_id)
    claimed = kb.claim_task(db, task_id, claimer="executor:abort", ttl_seconds=60)
    assert claimed is not None and claimed.current_run_id is not None
    process = HungProcess()
    signals = []
    monkeypatch.setattr(ce.subprocess, "Popen", lambda *a, **kw: process)
    monkeypatch.setattr(ce.select, "select", lambda *a, **kw: ([], [], []))
    monkeypatch.setattr(ce.os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(
        kb, "_terminate_reclaimed_worker",
        lambda *a, **kw: {"termination_attempted": True, "terminated": True, "host_local": True},
    )
    workspace = tmp_path / task_id
    workspace.mkdir()

    def register(pid):
        kb.register_claim_process(
            db, task_id, claimer="executor:abort",
            expected_run_id=claimed.current_run_id, pid=pid,
        )

    first = True

    def heartbeat():
        nonlocal first
        if first:
            first = False
            assert kb.reclaim_task(db, task_id, reason="adverse abort proof")
        kb.require_claim_heartbeat(
            db, task_id, claimer="executor:abort",
            expected_run_id=claimed.current_run_id, ttl_seconds=60,
        )

    with pytest.raises(kb.ClaimLeaseLost):
        ce.SubprocessClaudeRunner().run(
            argv=("claude", "--resume", "s"), input_jsonl="{}\n", cwd=workspace,
            timeout_seconds=30, heartbeat=heartbeat, heartbeat_interval_seconds=1,
            on_process_started=register,
        )

    assert signals == [(process.pid, ce.signal.SIGTERM)]
    task = db.execute("SELECT status, worker_pid FROM tasks WHERE id=?", (task_id,)).fetchone()
    run = db.execute("SELECT status, outcome FROM task_runs WHERE id=?", (claimed.current_run_id,)).fetchone()
    assert task["status"] == "ready" and task["worker_pid"] is None
    assert run["status"] == run["outcome"] == "reclaimed"
    assert db.execute(
        "SELECT COUNT(*) AS n FROM task_events WHERE task_id=? AND kind=?",
        (task_id, kb.EXEC_EVENT_COMPLETED),
    ).fetchone()["n"] == 0


class TestAdapterPersistedMapping:
    """(1) The mapping is a persisted, dispatcher-owned, provenanced row."""

    def test_an_unknown_run_has_no_mapping_and_is_not_inferred(self, db):
        with pytest.raises(kb.BindingNotFoundError, match="refusing to infer"):
            kb.load_session_binding(db, run_id=4242)

    def test_inferred_provenance_can_never_be_persisted(self, db):
        with pytest.raises(kb.InvocationPlanError, match="not continue-eligible"):
            _bind(db, 5, source=kb.SESSION_SOURCE_INFERRED)
        assert db.execute(
            "SELECT COUNT(*) c FROM kanban_session_bindings"
        ).fetchone()["c"] == 0

    def test_re_recording_an_identical_mapping_is_idempotent(self, db):
        _bind(db, 5)
        _bind(db, 5)
        assert db.execute(
            "SELECT COUNT(*) c FROM kanban_session_bindings WHERE run_id = 5"
        ).fetchone()["c"] == 1

    def test_rebinding_a_live_run_to_another_session_is_refused(self, db):
        _bind(db, 5, session="wsess-1")
        with pytest.raises(kb.BindingConflictError, match="refusing to rebind"):
            _bind(db, 5, session="wsess-2")
        assert kb.load_session_binding(db, run_id=5).session_id == "wsess-1"

    def test_retirement_is_durable_and_visible_to_the_loader(self, db):
        _bind(db, 5)
        assert kb.load_session_binding(db, run_id=5).retired is False
        assert kb.retire_session_binding(db, run_id=5, now=NOW) is True
        assert kb.load_session_binding(db, run_id=5).retired is True
        # Idempotent: nothing live left to retire.
        assert kb.retire_session_binding(db, run_id=5, now=NOW) is False

    def test_a_mapping_belonging_to_another_task_is_refused(self, db):
        _bind(db, 5, task_id="t_other")
        with pytest.raises(kb.BindingConflictError, match="belongs to task"):
            kb.load_session_binding(db, run_id=5, task_id="t_a")

    def test_the_window_survives_the_load(self, db):
        _bind(db, 5, issued_at=1234, expires_at=5678)
        loaded = kb.load_session_binding(db, run_id=5)
        assert (loaded.issued_at, loaded.expires_at) == (1234, 5678)
        assert loaded.owner == kb.DISPATCHER_BINDING_OWNER


class TestAdapterRequestPreparation:
    """(2)(3) Durable claim, then pure explicit-session construction."""

    def test_the_request_uses_explicit_resume_never_continue(self, db):
        _run_id, decision = _adapter_env(db)
        request = _prepare(db, decision)
        argv = request.argv
        assert argv[0] == "claude"
        assert argv[1] == "--resume" and argv[2] == "wsess-1"
        assert "--continue" not in argv
        assert "-c" not in argv

    def test_the_prepared_request_is_inert(self, db):
        _run_id, decision = _adapter_env(db)
        request = _prepare(db, decision)
        assert request.executed is False
        assert request.plan.executed is False
        assert request.plan.requires_a3_gate is True
        assert request.to_payload()["executed"] is False

    def test_preparation_commits_a_durable_claim(self, db):
        run_id, decision = _adapter_env(db)
        request = _prepare(db, decision)
        # Visible from a *separate* connection, so it really committed.
        other = sqlite3.connect(db_path_of(db), timeout=10)
        try:
            row = other.execute(
                "SELECT COUNT(*) FROM task_events WHERE id = ? AND kind = ?",
                (request.fence.claim_event_id, kb.EXEC_EVENT_CLAIMED),
            ).fetchone()
        finally:
            other.close()
        assert row[0] == 1
        assert request.fence.run_id == run_id

    def test_a_second_preparation_for_the_same_run_is_refused(self, db):
        _run_id, decision = _adapter_env(db)
        _prepare(db, decision)
        with pytest.raises(kb.DuplicateExecutionError):
            _prepare(db, decision)

    def test_the_mapping_comes_from_the_table_not_the_caller(self, db):
        """An in-memory SessionBinding cannot authorise anything."""
        _run_id, decision = _adapter_env(db, bind=False)
        # A perfectly-formed in-memory mapping exists in the caller's hand...
        fabricated = kb.SessionBinding(
            provider=kb.PROVIDER_CLAUDE_CODE, session_id="wsess-1",
            source=kb.SESSION_SOURCE_DISPATCHER, seat_id=None,
            issued_at=NOW - 60, expires_at=NOW + 600,
            owner=kb.DISPATCHER_BINDING_OWNER, retired=False,
        )
        assert kb.validate_binding_freshness(fabricated, now=NOW) is fabricated
        # ...and it still cannot drive a request, because nothing is persisted.
        with pytest.raises(kb.BindingNotFoundError):
            _prepare(db, decision)
        # There is no parameter through which it could be supplied.
        import inspect
        params = inspect.signature(kb.prepare_resume_request).parameters
        assert "binding" not in params

    def test_a_failed_preparation_does_not_burn_the_claim(self, db):
        """A rejected request must stay retryable once the cause is fixed."""
        run_id, decision = _adapter_env(db, expires_at=NOW - 1)  # expired
        with pytest.raises(kb.BindingNotFreshError):
            _prepare(db, decision)
        assert db.execute(
            "SELECT COUNT(*) c FROM task_events WHERE kind = ?",
            (kb.EXEC_EVENT_CLAIMED,),
        ).fetchone()["c"] == 0
        # Re-issue a valid mapping; the run can still be claimed.
        kb.retire_session_binding(db, run_id=run_id, now=NOW)
        _bind(db, run_id)
        assert _prepare(db, decision).fence.run_id == run_id


class TestAdapterFailClosed:
    """(5) Unknown / stale / retired / mismatched / A3 / duplicate."""

    @pytest.mark.parametrize(
        "kwargs,exc,match",
        [
            ({"expires_at": NOW - 1}, kb.BindingNotFreshError, "expired"),
            ({"issued_at": NOW + 60, "expires_at": NOW + 600},
             kb.BindingNotFreshError, "in the future"),
            ({"owner": "someone-else"}, kb.BindingNotFreshError, "is not"),
        ],
    )
    def test_an_unfit_mapping_is_refused(self, db, kwargs, exc, match):
        _run_id, decision = _adapter_env(db, **kwargs)
        with pytest.raises(exc, match=match):
            _prepare(db, decision)

    def test_a_retired_mapping_is_refused(self, db):
        run_id, decision = _adapter_env(db)
        kb.retire_session_binding(db, run_id=run_id, now=NOW)
        with pytest.raises(kb.BindingNotFreshError, match="retired"):
            _prepare(db, decision)

    def test_a_mismatched_session_is_refused(self, db):
        """The persisted mapping must agree with the decision's own session."""
        run_id, decision = _adapter_env(db, bind=False)
        _bind(db, run_id, session="a-different-session")
        with pytest.raises(kb.InvocationPlanError, match="does not match decision"):
            _prepare(db, decision)

    def test_a_latched_a3_revocation_stops_preparation(self, db):
        _run_id, decision = _adapter_env(db)
        kb.latch_a3_revocation(db, task_id="t_a", reason="stop")
        with pytest.raises(kb.ExecutionNotPermitted, match="A3 revocation"):
            _prepare(db, decision)

    @pytest.mark.parametrize("route", [kb.ROUTE_REVIEW, kb.ROUTE_BLOCK, kb.ROUTE_CLOSE])
    def test_a_non_continue_decision_cannot_be_prepared(self, db, route):
        _run_id, decision = _adapter_env(db)
        with pytest.raises(kb.ExecutionNotPermitted, match="may be prepared"):
            _prepare(db, dataclasses.replace(decision, route=route))

    def test_every_refusal_records_a_refused_event(self, db):
        run_id, decision = _adapter_env(db)
        kb.retire_session_binding(db, run_id=run_id, now=NOW)
        with pytest.raises(kb.BindingNotFreshError):
            _prepare(db, decision)
        assert db.execute(
            "SELECT COUNT(*) c FROM task_events WHERE kind = ?",
            (kb.EXEC_EVENT_REFUSED,),
        ).fetchone()["c"] == 1

    def test_no_refusal_performs_a_terminal_write(self, db):
        run_id, decision = _adapter_env(db)
        kb.retire_session_binding(db, run_id=run_id, now=NOW)
        with pytest.raises(kb.BindingNotFreshError):
            _prepare(db, decision)
        assert db.execute(
            "SELECT status FROM tasks WHERE id='t_a'"
        ).fetchone()["status"] == "running"


class TestAdapterSealedResults:
    """The result path is closed to anything a registered adapter didn't seal."""

    def test_a_bare_dict_cannot_mutate_the_board(self, db):
        _run_id, decision = _adapter_env(db)
        request = _prepare(db, decision)
        policy = kb.ExecutorPolicy(fake_executors=[])
        with pytest.raises(kb.UnsealedResultError, match="requires an AdapterReceipt"):
            kb.interpret_terminal_result(
                db, receipt=_ok_result(request.fence.run_id), policy=policy, now=NOW,
            )
        assert db.execute(
            "SELECT status FROM tasks WHERE id='t_a'"
        ).fetchone()["status"] == "running"

    def test_an_unregistered_adapter_cannot_seal(self, db):
        _run_id, decision = _adapter_env(db)
        request = _prepare(db, decision)
        stranger = FakeAdapter()
        with pytest.raises(kb.ExecutionNotPermitted):
            kb.seal_adapter_result(
                db, adapter=stranger, request=request,
                result=_ok_result(request.fence.run_id),
                policy=kb.ExecutorPolicy(fake_executors=[]), now=NOW,
            )

    def test_a_liar_claiming_to_be_a_fake_cannot_seal(self, db):
        _run_id, decision = _adapter_env(db)
        request = _prepare(db, decision)

        class Liar(FakeAdapter):
            simulated = True
            is_fake = True
            name = "fake-adapter"

        with pytest.raises(kb.ExecutionNotPermitted):
            kb.seal_adapter_result(
                db, adapter=Liar(), request=request,
                result=_ok_result(request.fence.run_id),
                policy=kb.ExecutorPolicy(fake_executors=[FakeAdapter()]), now=NOW,
            )

    def test_a_receipt_is_not_a_bearer_token(self, db):
        """A receipt sealed under one policy is refused by a policy that
        does not itself vouch for the adapter — so it cannot be replayed."""
        _run_id, decision = _adapter_env(db)
        request = _prepare(db, decision)
        receipt, _ = _sealed(db, request, _ok_result(request.fence.run_id))
        foreign = kb.ExecutorPolicy(fake_executors=[])
        with pytest.raises(kb.ExecutionNotPermitted):
            kb.interpret_terminal_result(db, receipt=receipt, policy=foreign, now=NOW)
        assert db.execute(
            "SELECT status FROM tasks WHERE id='t_a'"
        ).fetchone()["status"] == "running"

    def test_a_receipt_wrapping_a_non_request_is_refused(self, db):
        forged = kb.AdapterReceipt(
            adapter=FakeAdapter(), request={"task_id": "t_a"},
            result={"status": "completed", "summary": "x", "run_id": 1},
            sealed_at=NOW,
        )
        with pytest.raises(kb.UnsealedResultError, match="must be a ResumeRequest"):
            kb.interpret_terminal_result(
                db, receipt=forged, policy=kb.ExecutorPolicy(), now=NOW,
            )

    def test_sealing_requires_a_real_request_object(self, db):
        with pytest.raises(kb.ExecutionResultInvalid, match="must be a ResumeRequest"):
            kb.seal_adapter_result(
                db, adapter=FakeAdapter(), request={"nope": True},
                result={}, policy=kb.ExecutorPolicy(), now=NOW,
            )

    def test_a_real_adapter_still_needs_allow_flag_and_a3_gate(self, db):
        """Extensibility preserved, invocation still disabled."""
        _run_id, decision = _adapter_env(db)
        request = _prepare(db, decision)
        real = FakeAdapter()  # unregistered => treated as real
        permissive = kb.ExecutorPolicy(allow_real_execution=True, fake_executors=[])
        with pytest.raises(kb.ExecutionNotPermitted, match="A3 gate"):
            kb.seal_adapter_result(
                db, adapter=real, request=request,
                result=_ok_result(request.fence.run_id),
                policy=permissive, now=NOW,
            )


class TestAdapterTerminalRouting:
    """(4) Typed outcomes route through canonical APIs into a projection."""

    def _interpret(self, db, request, result):
        receipt, policy = _sealed(db, request, result)
        return kb.interpret_terminal_result(
            db, receipt=receipt, policy=policy, now=NOW,
        )

    def test_completed_closes_through_the_canonical_api(self, db):
        _run_id, decision = _adapter_env(db)
        request = _prepare(db, decision)
        out = self._interpret(
            db, request, _ok_result(request.fence.run_id, "completed", "all done"),
        )
        assert out.route == kb.ROUTE_CLOSE and out.terminal_write is True
        assert db.execute(
            "SELECT status FROM tasks WHERE id='t_a'"
        ).fetchone()["status"] == "done"

    def test_blocked_blocks_through_the_canonical_api(self, db):
        _run_id, decision = _adapter_env(db)
        request = _prepare(db, decision)
        out = self._interpret(
            db, request, _ok_result(request.fence.run_id, "blocked", "needs a call"),
        )
        assert out.route == kb.ROUTE_BLOCK and out.terminal_write is True
        assert db.execute(
            "SELECT status FROM tasks WHERE id='t_a'"
        ).fetchone()["status"] == "blocked"

    @pytest.mark.parametrize(
        "status,route",
        [("needs_review", kb.ROUTE_REVIEW), ("incomplete", kb.ROUTE_CONTINUE)],
    )
    def test_review_and_continue_write_no_task_status(self, db, status, route):
        _run_id, decision = _adapter_env(db)
        request = _prepare(db, decision)
        out = self._interpret(
            db, request, _ok_result(request.fence.run_id, status, "partial"),
        )
        assert out.route == route and out.terminal_write is False
        assert db.execute(
            "SELECT status FROM tasks WHERE id='t_a'"
        ).fetchone()["status"] == "running"

    def test_a_notification_projection_is_produced(self, db):
        _run_id, decision = _adapter_env(db)
        request = _prepare(db, decision)
        out = self._interpret(db, request, _ok_result(request.fence.run_id))
        assert out.notification is not None
        assert out.notification.task_id == "t_a"

    def test_a_malformed_result_is_refused_without_a_terminal_write(self, db):
        _run_id, decision = _adapter_env(db)
        request = _prepare(db, decision)
        with pytest.raises(kb.ExecutionResultInvalid):
            self._interpret(
                db, request,
                {"status": "completed", "summary": "x", "run_id": 1, "extra": 1},
            )
        assert db.execute(
            "SELECT status FROM tasks WHERE id='t_a'"
        ).fetchone()["status"] == "running"

    def test_a_moved_fence_discards_the_result(self, db):
        _run_id, decision = _adapter_env(db)
        request = _prepare(db, decision)
        with kb.write_txn(db):
            db.execute("UPDATE tasks SET current_run_id = 999999 WHERE id='t_a'")
        with pytest.raises(kb.ExecutionFenceLost, match="fence moved"):
            self._interpret(db, request, _ok_result(request.fence.run_id))
        assert db.execute(
            "SELECT status FROM tasks WHERE id='t_a'"
        ).fetchone()["status"] == "running"
        assert db.execute(
            "SELECT COUNT(*) c FROM task_events WHERE kind = ?",
            (kb.EXEC_EVENT_DISCARDED,),
        ).fetchone()["c"] == 1

    def test_a_result_for_another_run_is_refused(self, db):
        _run_id, decision = _adapter_env(db)
        request = _prepare(db, decision)
        with pytest.raises(kb.ExecutionResultInvalid):
            self._interpret(db, request, _ok_result(request.fence.run_id + 500))


class TestAdapterNoSideEffects:
    """(6) No-side-effect scans over the adapter path."""

    ADAPTER_FUNCS = (
        "record_session_binding", "retire_session_binding", "load_session_binding",
        "prepare_resume_request", "seal_adapter_result", "interpret_terminal_result",
    )

    def test_no_execution_primitive_appears_in_the_adapter_path(self):
        import inspect
        forbidden = (
            "subprocess", "Popen", "socket", "urllib", "requests", "httpx",
            "os.system", "os.exec", "os.spawn", "pty.", "fork(",
            "crontab", "systemctl", "schedule", "asyncio.create_subprocess",
        )
        for name in self.ADAPTER_FUNCS:
            src = inspect.getsource(getattr(kb, name))
            # Prose must not be able to fail (or pass) this scan.
            doc = getattr(kb, name).__doc__ or ""
            code = src.replace(doc, "")
            for token in forbidden:
                assert token not in code, f"{name} references {token!r}"

    def test_the_adapter_path_never_writes_task_status_with_raw_sql(self):
        import inspect
        src = inspect.getsource(kb.interpret_terminal_result)
        doc = kb.interpret_terminal_result.__doc__ or ""
        code = src.replace(doc, "")
        assert "UPDATE tasks" not in code
        assert "complete_task(" in code and "block_task(" in code
        assert "expected_run_id=fence.current_run_id" in code

    def test_preparing_a_request_reads_no_credentials_or_environment(self, db):
        import inspect
        for name in self.ADAPTER_FUNCS:
            code = inspect.getsource(getattr(kb, name))
            for token in ("os.environ", "getenv", "ANTHROPIC", "API_KEY", "token="):
                assert token not in code, f"{name} touches {token!r}"

    def test_the_fake_adapter_is_never_called_by_the_slice(self, db):
        """Nothing in preparation or interpretation invokes the adapter."""
        _run_id, decision = _adapter_env(db)
        request = _prepare(db, decision)
        adapter = FakeAdapter(_ok_result(request.fence.run_id))
        policy = kb.ExecutorPolicy(fake_executors=[adapter])
        receipt = kb.seal_adapter_result(
            db, adapter=adapter, request=request,
            result=_ok_result(request.fence.run_id), policy=policy, now=NOW,
        )
        kb.interpret_terminal_result(db, receipt=receipt, policy=policy, now=NOW)
        assert adapter.calls == 0


class TestAdapterTerminalKillRecheck:
    """A3 latched between seal and interpretation must stop the board write.

    The registered-fake branch skips `policy.permit`, so before this recheck a
    fake-sealed receipt reached `complete_task` with no revocation check on the
    terminal path at all.
    """

    def _counts(self, db):
        return {
            kind: db.execute(
                "SELECT COUNT(*) c FROM task_events WHERE kind = ?", (kind,)
            ).fetchone()["c"]
            for kind in (kb.EXEC_EVENT_REFUSED, kb.EXEC_EVENT_COMPLETED,
                         kb.EXEC_EVENT_CLAIMED, kb.EXEC_EVENT_DISCARDED,
                         kb.EXEC_EVENT_VALIDATED)
        }

    def test_an_a3_latch_after_sealing_stops_the_terminal_write(self, db):
        _run_id, decision = _adapter_env(db)
        request = _prepare(db, decision)
        receipt, policy = _sealed(
            db, request, _ok_result(request.fence.run_id, "completed", "all done"),
        )
        # The adapter IS registered, so this exercises the shortcut path.
        assert policy.is_registered_fake(receipt.adapter) is True
        before = self._counts(db)

        # Latch AFTER prepare and AFTER seal, before interpretation.
        kb.latch_a3_revocation(db, task_id="t_a", reason="stop everything")

        with pytest.raises(kb.ExecutionNotPermitted, match="A3 revocation is latched"):
            kb.interpret_terminal_result(
                db, receipt=receipt, policy=policy, now=NOW,
            )

        after = self._counts(db)
        # (a) no terminal task write
        assert db.execute(
            "SELECT status FROM tasks WHERE id='t_a'"
        ).fetchone()["status"] == "running"
        assert db.execute(
            "SELECT result, completed_at FROM tasks WHERE id='t_a'"
        ).fetchone()["completed_at"] is None
        # (b) no completion AND no validation recorded => the pre-check
        #     refused before any part of the terminal path was entered
        assert after[kb.EXEC_EVENT_COMPLETED] == before[kb.EXEC_EVENT_COMPLETED] == 0
        assert after[kb.EXEC_EVENT_VALIDATED] == before[kb.EXEC_EVENT_VALIDATED] == 0
        # (c) an auditable refusal was appended
        assert after[kb.EXEC_EVENT_REFUSED] == before[kb.EXEC_EVENT_REFUSED] + 1
        # (d) the fence is untouched — not discarded, not re-claimed
        assert after[kb.EXEC_EVENT_CLAIMED] == before[kb.EXEC_EVENT_CLAIMED] == 1
        assert after[kb.EXEC_EVENT_DISCARDED] == before[kb.EXEC_EVENT_DISCARDED] == 0
        assert db.execute(
            "SELECT COUNT(*) c FROM task_events WHERE id = ? AND kind = ?",
            (request.fence.claim_event_id, kb.EXEC_EVENT_CLAIMED),
        ).fetchone()["c"] == 1

    def test_the_refusal_reason_is_auditable(self, db):
        _run_id, decision = _adapter_env(db)
        request = _prepare(db, decision)
        receipt, policy = _sealed(db, request, _ok_result(request.fence.run_id))
        kb.latch_a3_revocation(db, task_id="t_a", reason="stop everything")
        with pytest.raises(kb.ExecutionNotPermitted):
            kb.interpret_terminal_result(db, receipt=receipt, policy=policy, now=NOW)
        row = db.execute(
            "SELECT payload FROM task_events WHERE kind = ? ORDER BY id DESC LIMIT 1",
            (kb.EXEC_EVENT_REFUSED,),
        ).fetchone()
        payload = json.loads(row["payload"])
        assert "A3 revocation is latched" in payload["reason"]
        assert "t_a" in payload["reason"]

    def test_the_recheck_is_not_delegated_to_the_policy(self, db):
        """A policy that vouches for everything cannot wave a latch through."""
        _run_id, decision = _adapter_env(db)
        request = _prepare(db, decision)

        class VouchesForEverything(kb.ExecutorPolicy):
            def is_registered_fake(self, executor):
                return True

            def permit(self, conn, executor, task_id):
                return None

        adapter = FakeAdapter()
        policy = VouchesForEverything()
        receipt = kb.seal_adapter_result(
            db, adapter=adapter, request=request,
            result=_ok_result(request.fence.run_id), policy=policy, now=NOW,
        )
        kb.latch_a3_revocation(db, task_id="t_a", reason="stop everything")
        with pytest.raises(kb.ExecutionNotPermitted, match="A3 revocation is latched"):
            kb.interpret_terminal_result(db, receipt=receipt, policy=policy, now=NOW)
        assert db.execute(
            "SELECT status FROM tasks WHERE id='t_a'"
        ).fetchone()["status"] == "running"

    def test_an_unlatched_task_still_completes(self, db):
        """The recheck must not break the normal path (guards against a
        mutant that simply refuses everything)."""
        _run_id, decision = _adapter_env(db)
        request = _prepare(db, decision)
        receipt, policy = _sealed(
            db, request, _ok_result(request.fence.run_id, "completed", "all done"),
        )
        out = kb.interpret_terminal_result(
            db, receipt=receipt, policy=policy, now=NOW,
        )
        assert out.terminal_write is True and out.route == kb.ROUTE_CLOSE
        assert db.execute(
            "SELECT status FROM tasks WHERE id='t_a'"
        ).fetchone()["status"] == "done"
        # Terminal event recorded exactly once, AFTER the validation marker.
        kinds = [r["kind"] for r in db.execute(
            "SELECT kind FROM task_events ORDER BY id"
        )]
        assert kinds.count(kb.EXEC_EVENT_COMPLETED) == 1
        assert kinds.index(kb.EXEC_EVENT_VALIDATED) < kinds.index(
            kb.EXEC_EVENT_COMPLETED
        )

    def test_a_latch_landing_between_the_precheck_and_the_write_is_caught(self, db):
        """Adversarial interleaving, deterministic.

        The pre-check commits and the canonical writer opens its own
        transaction, so A3 can latch in that gap. Simulated exactly: the latch
        probe answers False for the pre-check and True from then on — i.e. the
        revocation lands after the pre-check passed but before the terminal
        CAS. Only the in-transaction `a3_guard` can catch this.
        """
        _run_id, decision = _adapter_env(db)
        request = _prepare(db, decision)
        receipt, policy = _sealed(
            db, request, _ok_result(request.fence.run_id, "completed", "all done"),
        )
        real = kb.a3_revocation_latched
        calls = {"n": 0}

        def racing_latch(conn, task_id):
            calls["n"] += 1
            if calls["n"] == 1:
                return False          # pre-check sees a clean task...
            return True               # ...then the revocation lands.

        with mock.patch.object(kb, "a3_revocation_latched", racing_latch):
            with pytest.raises(kb.ExecutionNotPermitted, match="A3 revocation is latched"):
                kb.interpret_terminal_result(
                    db, receipt=receipt, policy=policy, now=NOW,
                )
        assert calls["n"] >= 2, "the terminal write did not re-check the latch"
        assert kb.a3_revocation_latched is real
        # The board is untouched: the writer's transaction rolled back.
        row = db.execute(
            "SELECT status, result, completed_at FROM tasks WHERE id='t_a'"
        ).fetchone()
        assert row["status"] == "running"
        assert row["completed_at"] is None
        assert db.execute(
            "SELECT COUNT(*) c FROM task_events WHERE kind = ?",
            (kb.EXEC_EVENT_REFUSED,),
        ).fetchone()["c"] == 1
        # The log must NOT claim a completion the guard refused. Only the
        # truthful non-terminal validation marker may be present.
        assert db.execute(
            "SELECT COUNT(*) c FROM task_events WHERE kind = ?",
            (kb.EXEC_EVENT_COMPLETED,),
        ).fetchone()["c"] == 0
        assert db.execute(
            "SELECT COUNT(*) c FROM task_events WHERE kind = ?",
            (kb.EXEC_EVENT_VALIDATED,),
        ).fetchone()["c"] == 1
        # ...and no terminal event of any kind precedes the refusal.
        kinds = [r["kind"] for r in db.execute(
            "SELECT kind FROM task_events ORDER BY id"
        )]
        assert kinds[-1] == kb.EXEC_EVENT_REFUSED
        assert kb.EXEC_EVENT_COMPLETED not in kinds

    def test_the_guard_defaults_off_for_existing_callers(self, db):
        """Backward compatible: canonical writers are unchanged by default."""
        import inspect
        for fn in (kb.complete_task, kb.block_task):
            assert inspect.signature(fn).parameters["a3_guard"].default is False
        _seed_task(db, "t_legacy", status="running")
        kb.latch_a3_revocation(db, task_id="t_legacy", reason="latched")
        # Default call path still completes despite the latch — unchanged.
        assert kb.complete_task(db, "t_legacy", summary="legacy") is True
        assert db.execute(
            "SELECT status FROM tasks WHERE id='t_legacy'"
        ).fetchone()["status"] == "done"

    def test_the_guard_blocks_the_block_route_too(self, db):
        _run_id, decision = _adapter_env(db)
        request = _prepare(db, decision)
        receipt, policy = _sealed(
            db, request, _ok_result(request.fence.run_id, "blocked", "needs a call"),
        )
        calls = {"n": 0}

        def racing_latch(conn, task_id):
            calls["n"] += 1
            return calls["n"] != 1

        with mock.patch.object(kb, "a3_revocation_latched", racing_latch):
            with pytest.raises(kb.ExecutionNotPermitted, match="A3 revocation is latched"):
                kb.interpret_terminal_result(
                    db, receipt=receipt, policy=policy, now=NOW,
                )
        assert db.execute(
            "SELECT status FROM tasks WHERE id='t_a'"
        ).fetchone()["status"] == "running"
        assert db.execute(
            "SELECT COUNT(*) c FROM task_events WHERE kind = ?",
            (kb.EXEC_EVENT_COMPLETED,),
        ).fetchone()["c"] == 0


# ---------------------------------------------------------------------------
# Provider adapter boundary — disabled by default, Claude Code first
# ---------------------------------------------------------------------------


def _events(subtype="success", *, text="all done", is_error=False,
            session_id="wsess-1", extra=()):
    ev = [{"type": "system", "subtype": "init", "session_id": session_id},
          {"type": "assistant", "message": {"role": "assistant"}}]
    result = {"type": "result", "subtype": subtype, "is_error": is_error,
              "result": text, "session_id": session_id}
    ev.append(result)
    ev.extend(extra)
    return ev


class TestClaudeCodeAdapterCommand:
    """Only an explicit mapped session id produces the expected pure command."""

    def test_the_adapter_renders_the_exact_documented_argv(self, db):
        _run_id, decision = _adapter_env(db)
        request = _prepare(db, decision)
        spec = kb.ClaudeCodeAdapter().build_command(request.plan)
        assert spec.argv == (
            "claude", "--resume", "wsess-1", "--print", "--verbose",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--include-hook-events",
            "--permission-mode", "plan",
            "--disallowedTools", "Bash,Edit,Write,Read,Glob,Grep,WebFetch,WebSearch,Task",
            "--safe-mode", "--strict-mcp-config",
            "--max-turns", "1",
        )
        assert spec.executed is False

    def test_command_carries_one_canonical_jsonl_user_message(self, db):
        import json

        _run_id, decision = _adapter_env(db)
        request = _prepare(db, decision)
        spec = kb.ClaudeCodeAdapter().build_command(request.plan)
        assert spec.input_jsonl.endswith("\n")
        assert spec.input_jsonl.count("\n") == 1
        envelope = json.loads(spec.input_jsonl)
        assert envelope["type"] == "user"
        assert envelope["message"]["role"] == "user"
        blocks = envelope["message"]["content"]
        assert isinstance(blocks, list) and len(blocks) == 1
        assert set(blocks[0]) == {"type", "text"}
        assert blocks[0]["type"] == "text"
        assert isinstance(blocks[0]["text"], str)
        text = blocks[0]["text"]
        assert text.startswith(
            "Hermes resume capsule (schema v1; bounded task data):\n"
        )
        payload = json.loads(text.split("\n", 1)[1])
        assert payload == request.plan.capsule.to_payload()

    def test_the_session_id_comes_from_the_persisted_mapping(self, db):
        _run_id, decision = _adapter_env(db, session="wsess-explicit")
        request = _prepare(db, decision)
        spec = kb.ClaudeCodeAdapter().build_command(request.plan)
        assert spec.argv[spec.argv.index("--resume") + 1] == "wsess-explicit"
        assert spec.session_id == "wsess-explicit"

    def test_continue_and_discovery_flags_are_never_emitted(self, db):
        _run_id, decision = _adapter_env(db)
        request = _prepare(db, decision)
        argv = kb.ClaudeCodeAdapter().build_command(request.plan).argv
        for banned in ("--continue", "-c", "--resume-last", "--list-sessions",
                       "--dangerously-skip-permissions", "--fork-session"):
            assert banned not in argv

    def test_building_is_deterministic(self, db):
        _run_id, decision = _adapter_env(db)
        request = _prepare(db, decision)
        a = kb.ClaudeCodeAdapter().build_command(request.plan)
        b = kb.ClaudeCodeAdapter().build_command(request.plan)
        assert a.argv == b.argv and a.output_schema_json == b.output_schema_json

    def test_a_foreign_provider_plan_is_refused(self, db):
        _run_id, decision = _adapter_env(db)
        request = _prepare(db, decision)
        alien = dataclasses.replace(
            request.plan,
            binding=dataclasses.replace(request.plan.binding, provider="grok"),
        )
        with pytest.raises(kb.InvocationPlanError, match="not resume-capable"):
            kb.ClaudeCodeAdapter().build_command(alien)

    def test_a_hand_constructed_forged_plan_cannot_render_a_resume_command(self):
        """Frozen dataclasses are not capabilities; revalidate at the edge."""
        forged = kb.InvocationPlan(
            decision=kb.RouteDecision(
                route=kb.ROUTE_BLOCK, reason="x", task_id="t1", run_id=1,
                outcome="x", spawn=False,
            ),
            binding=kb.SessionBinding(
                provider=kb.PROVIDER_CLAUDE_CODE, session_id="forged-session",
                source="inferred", issued_at=0, expires_at=0, owner="",
                retired=True,
            ),
            capsule=kb.ResumeCapsule(
                capsule_version=1, task_id="other", run_id=999, outcome="x",
                reason="x", instruction="x",
            ),
            command=kb.ResumeCommandSpec(
                provider=kb.PROVIDER_CLAUDE_CODE, session_id="forged-session",
                argv=("forged",), output_schema_json="{}", timeout_seconds=30,
            ),
        )
        with pytest.raises(kb.InvocationPlanError):
            kb.ClaudeCodeAdapter().build_command(forged)

    def test_a_non_plan_is_refused(self):
        with pytest.raises(kb.InvocationPlanError, match="must be an InvocationPlan"):
            kb.ClaudeCodeAdapter().build_command({"session_id": "wsess-1"})


class TestClaudeCodeAdapterDisabled:
    """No execution can occur while disabled — or at all, in this tree."""

    def test_the_adapter_is_disabled_by_default(self):
        assert kb.ClaudeCodeAdapter().enabled is False
        import inspect
        assert inspect.signature(
            kb.ClaudeCodeAdapter.__init__
        ).parameters["enabled"].default is False

    def test_execute_refuses_while_disabled(self, db):
        _run_id, decision = _adapter_env(db)
        request = _prepare(db, decision)
        adapter = kb.ClaudeCodeAdapter()
        with pytest.raises(kb.AdapterExecutionDisabled, match="disabled"):
            adapter.execute(request.plan, timeout_seconds=30)

    def test_execute_refuses_even_when_enabled(self, db):
        """Enabling is not arming: there is no transport behind the adapter."""
        _run_id, decision = _adapter_env(db)
        request = _prepare(db, decision)
        adapter = kb.ClaudeCodeAdapter(enabled=True)
        with pytest.raises(kb.AdapterExecutionDisabled, match="no transport linked"):
            adapter.execute(request.plan, timeout_seconds=30)

    def test_refusal_is_deterministic_across_repeats(self, db):
        _run_id, decision = _adapter_env(db)
        request = _prepare(db, decision)
        adapter = kb.ClaudeCodeAdapter(enabled=True)
        for _ in range(5):
            with pytest.raises(kb.AdapterExecutionDisabled):
                adapter.execute(request.plan, timeout_seconds=30)
        assert adapter.execute_calls == 5

    def test_the_adapter_path_imports_no_execution_primitive(self):
        import inspect
        forbidden = (
            "subprocess", "Popen", "socket", "urllib", "requests", "httpx",
            "os.system", "os.exec", "os.spawn", "pty.", "fork(", "asyncio",
            "crontab", "systemctl", "os.environ", "getenv", "ANTHROPIC",
            "API_KEY", "credential", "shell=", "check_output", "run(",
        )
        targets = [kb.ClaudeCodeAdapter.build_command,
                   kb.ClaudeCodeAdapter.execute,
                   kb.ClaudeCodeAdapter.__init__,
                   kb.parse_claude_stream_output,
                   kb.render_resume_command,
                   kb.render_claude_stream_input]
        for fn in targets:
            doc = fn.__doc__ or ""
            code = inspect.getsource(fn).replace(doc, "")
            for token in forbidden:
                assert token not in code, f"{fn.__qualname__} references {token!r}"

    def test_the_adapter_cannot_be_sealed_as_a_result_producer(self, db):
        """An unregistered adapter cannot mint a receipt, so a refusal to
        execute can never be laundered into a terminal outcome."""
        _run_id, decision = _adapter_env(db)
        request = _prepare(db, decision)
        adapter = kb.ClaudeCodeAdapter()
        with pytest.raises(kb.ExecutionNotPermitted):
            kb.seal_adapter_result(
                db, adapter=adapter, request=request,
                result=_ok_result(request.fence.run_id),
                policy=kb.ExecutorPolicy(), now=NOW,
            )


class TestClaudeCodeOutputParsing:
    """Malformed output fails closed; inconclusive output routes to review."""

    @pytest.mark.parametrize("bad,label", [
        ("a string", "string"),
        (b"bytes", "bytes"),
        ({"type": "result"}, "bare mapping"),
        ([], "empty"),
        ([{"type": "system"}], "no terminal result"),
        ([{"type": "result", "subtype": "success"},
          {"type": "result", "subtype": "success"}], "two results"),
        ([{"type": "result"}], "missing subtype"),
        ([{"type": "result", "subtype": ""}], "blank subtype"),
        ([{"type": "result", "subtype": "success", "result": 123}], "non-str result"),
        ([{"type": "result", "subtype": "success", "is_error": "yes"}], "non-bool"),
        (["not a mapping"], "non-mapping event"),
        ([{"subtype": "success"}], "event without type"),
    ])
    def test_malformed_output_fails_closed(self, bad, label):
        with pytest.raises(kb.ProviderOutputInvalid):
            kb.parse_claude_stream_output(
                bad, expected_run_id=7, expected_session_id="wsess-1"
            )

    def test_a_success_result_maps_to_completed(self):
        out = kb.parse_claude_stream_output(
            _events(), expected_run_id=7, expected_session_id="wsess-1"
        )
        assert out == {"status": "completed", "summary": "all done", "run_id": 7}

    def test_a_bound_claude_rate_limit_lifecycle_event_is_informational(self):
        """Actual Claude stream-json may emit this before its result event."""
        events = _events()
        events.insert(2, {
            "type": "rate_limit_event",
            "session_id": "wsess-1",
            "rate_limit_info": {"status": "allowed"},
        })
        assert kb.parse_claude_stream_output(
            events, expected_run_id=7, expected_session_id="wsess-1"
        ) == {"status": "completed", "summary": "all done", "run_id": 7}
        assert "rate_limit_event" in kb.RESUME_STREAM_EVENT_SCHEMA["properties"]["type"]["enum"]

    def test_bound_hook_lifecycle_events_are_informational(self):
        """Actual --include-hook-events streams bracket init/result this way."""
        events = _events()
        events.insert(0, {
            "type": "system", "subtype": "hook_started",
            "hook_event": "SessionStart", "session_id": "wsess-1",
        })
        events.append({
            "type": "system", "subtype": "hook_response",
            "hook_event": "Stop", "session_id": "wsess-1",
        })
        assert kb.parse_claude_stream_output(
            events, expected_run_id=7, expected_session_id="wsess-1"
        ) == {"status": "completed", "summary": "all done", "run_id": 7}

    def test_hook_lifecycle_events_remain_strictly_session_bound(self):
        events = _events()
        events.insert(0, {
            "type": "system", "subtype": "hook_started",
            "hook_event": "SessionStart", "session_id": "foreign-session",
        })
        with pytest.raises(kb.ProviderOutputInvalid, match="hook system session_id"):
            kb.parse_claude_stream_output(
                events, expected_run_id=7, expected_session_id="wsess-1"
            )

    @pytest.mark.parametrize("event", [
        {"type": "rate_limit_event", "session_id": "foreign-session",
         "rate_limit_info": {"status": "allowed"}},
        {"type": "rate_limit_event", "session_id": "wsess-1"},
    ])
    def test_rate_limit_events_remain_strictly_bound_and_well_formed(self, event):
        events = _events()
        events.insert(2, event)
        with pytest.raises(kb.ProviderOutputInvalid):
            kb.parse_claude_stream_output(
                events, expected_run_id=7, expected_session_id="wsess-1"
            )

    def test_rate_limit_schema_matches_the_parser_shape_contract(self):
        from jsonschema import Draft202012Validator

        validator = Draft202012Validator(kb.resume_stream_event_schema())
        valid = {
            "type": "rate_limit_event",
            "session_id": "wsess-1",
            "rate_limit_info": {"status": "allowed"},
        }
        assert not list(validator.iter_errors(valid))
        for malformed in (
            {"type": "rate_limit_event", "session_id": "wsess-1"},
            {"type": "rate_limit_event", "rate_limit_info": {}},
            {"type": "rate_limit_event", "session_id": "", "rate_limit_info": {}},
            {"type": "rate_limit_event", "session_id": "wsess-1", "rate_limit_info": []},
        ):
            assert list(validator.iter_errors(malformed))

    @pytest.mark.parametrize("subtype", [
        "error_max_turns", "error_during_execution", "error",
        "cancelled", "timeout", "unavailable",
    ])
    def test_ambiguous_outcomes_route_to_review_without_a_terminal_write(
        self, subtype
    ):
        out = kb.parse_claude_stream_output(
            _events(subtype, text="partial"), expected_run_id=7,
            expected_session_id="wsess-1",
        )
        assert out["status"] == "needs_review"
        assert kb.EXECUTION_STATUS_TO_ROUTE[out["status"]] == kb.ROUTE_REVIEW
        # A *recognised* inconclusive outcome must be labelled as such, not
        # swept into the unknown-subtype fallback. Both fail safe, so without
        # this the two branches are indistinguishable and one can rot unnoticed.
        assert out["summary"].startswith(f"[{subtype}]")
        assert "unknown subtype" not in out["summary"]

    def test_an_unknown_subtype_is_never_assumed_successful(self):
        out = kb.parse_claude_stream_output(
            _events("some_future_subtype"), expected_run_id=7,
            expected_session_id="wsess-1",
        )
        assert out["status"] == "needs_review"

    def test_an_error_flag_overrides_a_success_subtype(self):
        out = kb.parse_claude_stream_output(
            _events("success", is_error=True, text="boom"), expected_run_id=7,
            expected_session_id="wsess-1",
        )
        assert out["status"] == "needs_review"

    def test_a_control_character_summary_is_rejected(self):
        with pytest.raises(kb.InvocationPlanError):
            kb.parse_claude_stream_output(
                _events(text="line break"), expected_run_id=7,
                expected_session_id="wsess-1",
            )

    def test_an_oversized_summary_is_rejected(self):
        with pytest.raises(kb.InvocationPlanError):
            kb.parse_claude_stream_output(
                _events(text="x" * (kb.EXECUTION_MAX_SUMMARY_CHARS + 1)),
                expected_run_id=7, expected_session_id="wsess-1",
            )

    @pytest.mark.parametrize("events", [
        _events(session_id="foreign-session"),
        [{"type": "result", "subtype": "success", "is_error": False,
          "result": "missing init", "session_id": "wsess-1"}],
        [{"type": "system", "subtype": "init", "session_id": "wsess-1"},
         {"type": "result", "subtype": "success", "is_error": False,
          "result": "foreign result", "session_id": "foreign-session"}],
        _events(extra=({"type": "tool_use", "name": "unexpected"},)),
    ])
    def test_session_identity_and_stream_shape_are_bound(self, events):
        with pytest.raises(kb.ProviderOutputInvalid):
            kb.parse_claude_stream_output(
                events, expected_run_id=7, expected_session_id="wsess-1"
            )


class TestClaudeCodeOutputEndToEnd:
    """Parsed output flows through the sealed path with fence + A3 intact."""

    def _seal_and_interpret(self, db, request, events):
        result = kb.parse_claude_stream_output(
            events, expected_run_id=request.fence.run_id,
            expected_session_id=request.plan.binding.session_id,
        )
        receipt, policy = _sealed(db, request, result)
        return kb.interpret_terminal_result(
            db, receipt=receipt, policy=policy, now=NOW,
        )

    def test_a_success_stream_closes_the_task(self, db):
        _run_id, decision = _adapter_env(db)
        request = _prepare(db, decision)
        out = self._seal_and_interpret(db, request, _events())
        assert out.route == kb.ROUTE_CLOSE and out.terminal_write is True
        assert db.execute(
            "SELECT status FROM tasks WHERE id='t_a'"
        ).fetchone()["status"] == "done"

    @pytest.mark.parametrize("subtype", ["timeout", "unavailable", "error_max_turns"])
    def test_an_inconclusive_stream_writes_no_task_status(self, db, subtype):
        _run_id, decision = _adapter_env(db)
        request = _prepare(db, decision)
        out = self._seal_and_interpret(db, request, _events(subtype))
        assert out.route == kb.ROUTE_REVIEW
        assert out.terminal_write is False
        assert db.execute(
            "SELECT status FROM tasks WHERE id='t_a'"
        ).fetchone()["status"] == "running"

    def test_a_moved_fence_still_discards_parsed_output(self, db):
        _run_id, decision = _adapter_env(db)
        request = _prepare(db, decision)
        with kb.write_txn(db):
            db.execute("UPDATE tasks SET current_run_id = 999999 WHERE id='t_a'")
        with pytest.raises(kb.ExecutionFenceLost):
            self._seal_and_interpret(db, request, _events())
        assert db.execute(
            "SELECT status FROM tasks WHERE id='t_a'"
        ).fetchone()["status"] == "running"

    def test_a3_revocation_still_stops_parsed_output(self, db):
        _run_id, decision = _adapter_env(db)
        request = _prepare(db, decision)
        kb.latch_a3_revocation(db, task_id="t_a", reason="stop")
        with pytest.raises(kb.ExecutionNotPermitted, match="A3 revocation"):
            self._seal_and_interpret(db, request, _events())
        assert db.execute(
            "SELECT status FROM tasks WHERE id='t_a'"
        ).fetchone()["status"] == "running"

    def test_a_retired_mapping_still_blocks_preparation(self, db):
        run_id, decision = _adapter_env(db)
        kb.retire_session_binding(db, run_id=run_id, now=NOW)
        with pytest.raises(kb.BindingNotFreshError, match="retired"):
            _prepare(db, decision)


class TestNativeBrokerPass:
    """The durable completion cursor and route records move as one batch."""

    def test_fold_route_and_cursor_advance_are_one_native_pass(self, db):
        _seed_task(db, "t_pass", provider="claude-code")
        run_id = _seed_run(
            db, "t_pass", outcome="crashed", worker_session_id="known-session",
            worker_session_source=kb.SESSION_SOURCE_DISPATCHER,
        )
        result = kb.run_native_broker_pass(db, consumer="shadow-pass", limit=10)
        assert result.folded_run_ids == (run_id,)
        assert len(result.decisions) == len(result.notifications) == 1
        assert result.decisions[0].route == kb.ROUTE_CONTINUE
        assert result.notifications[0].text.startswith("CONTINUE task=t_pass")
        assert result.new_cursor > result.old_cursor
        assert kb.run_native_broker_pass(db, consumer="shadow-pass", limit=10).decisions == ()

    def test_bad_completion_rolls_back_cursor_and_route_records(self, db):
        _seed_task(db, "t_bad", provider="claude-code")
        with kb.write_txn(db):
            db.execute(
                "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("t_bad", 99, kb.BROKER_EVENT_WORKER_COMPLETION, "{bad", NOW),
            )
        with pytest.raises(kb.BrokerEventValidationError, match="invalid JSON"):
            kb.run_native_broker_pass(db, consumer="shadow-bad", limit=10)
        assert kb.broker_cursor(db, consumer="shadow-bad") == 0
        assert db.execute(
            "SELECT COUNT(*) AS n FROM task_events WHERE kind = ?",
            (kb.BROKER_EVENT_ROUTE_DECIDED,),
        ).fetchone()["n"] == 0


class TestCanonicalShadowBroker:
    """The service wrapper is one bounded native pass, never a worker loop."""

    @staticmethod
    def _provision(db, broker, token="test-shadow-token"):
        # Provisioning is deliberately separate from the wrapper invocation.
        kb.ensure_broker_sub(db, consumer=broker.consumer, token=token)

    def test_default_disabled_does_not_touch_native_cursor_or_events(self, db):
        _seed_task(db, "t_shadow", provider="claude-code")
        run_id = _seed_run(db, "t_shadow", outcome="crashed")
        broker = CanonicalShadowBroker()
        with pytest.raises(ShadowBrokerDisabled):
            broker.run_once(db, token="test-shadow-token")
        assert kb.broker_cursor(db, consumer=broker.consumer) == 0
        assert db.execute(
            "SELECT COUNT(*) AS c FROM task_events WHERE run_id=? AND kind=?",
            (run_id, kb.BROKER_EVENT_ROUTE_DECIDED),
        ).fetchone()["c"] == 0

    def test_enabled_wrapper_uses_one_fixed_consumer_and_only_native_projections(self, db):
        _seed_task(db, "t_shadow", provider="claude-code")
        run_id = _seed_run(db, "t_shadow", outcome="crashed")
        broker = CanonicalShadowBroker(enabled=True, limit=2)
        self._provision(db, broker)
        receipt = broker.run_once(db, token="test-shadow-token")
        assert receipt.consumer == "hermes-shadow-broker-v1"
        assert receipt.folded_run_ids == (run_id,)
        assert receipt.routes == (kb.ROUTE_REVIEW,)
        assert receipt.notifications and receipt.notifications[0].startswith("REVIEW")
        assert _task_row(db, "t_shadow")["status"] == "running"
        second = broker.run_once(db, token="test-shadow-token")
        assert second.routes == ()

    def test_absent_canonical_consumer_refuses_without_bootstrap_or_events(self, db):
        _seed_task(db, "t_shadow", provider="claude-code")
        run_id = _seed_run(db, "t_shadow", outcome="crashed")
        broker = CanonicalShadowBroker(enabled=True)
        with pytest.raises(kb.BrokerAuthError, match="not pre-provisioned"):
            broker.run_once(db, token="arbitrary-first-caller-token")
        assert db.execute(
            "SELECT COUNT(*) AS c FROM kanban_broker_subs WHERE consumer=?", (broker.consumer,)
        ).fetchone()["c"] == 0
        assert db.execute(
            "SELECT COUNT(*) AS c FROM task_events WHERE run_id=? AND kind=?",
            (run_id, kb.BROKER_EVENT_ROUTE_DECIDED),
        ).fetchone()["c"] == 0

    def test_wrong_preprovisioned_token_refuses_without_cursor_or_event_change(self, db):
        _seed_task(db, "t_shadow", provider="claude-code")
        run_id = _seed_run(db, "t_shadow", outcome="crashed")
        broker = CanonicalShadowBroker(enabled=True)
        self._provision(db, broker, token="right-token")
        before = kb.broker_cursor(db, consumer=broker.consumer)
        with pytest.raises(kb.BrokerAuthError, match="invalid token"):
            broker.run_once(db, token="wrong-token")
        assert kb.broker_cursor(db, consumer=broker.consumer) == before
        assert db.execute(
            "SELECT COUNT(*) AS c FROM task_events WHERE run_id=? AND kind=?",
            (run_id, kb.BROKER_EVENT_ROUTE_DECIDED),
        ).fetchone()["c"] == 0


class TestExternalExecutorClaimGuard:
    def test_matching_claim_renews_before_external_result_acceptance(self, db):
        _seed_task(db, "t_lease", status="ready")
        claimed = kb.claim_task(db, "t_lease", claimer="host:lease", ttl_seconds=60)
        assert claimed is not None
        assert kb.require_claim_heartbeat(
            db, "t_lease", claimer="host:lease", expected_run_id=claimed.current_run_id, ttl_seconds=60
        ) is None

    def test_foreign_or_lost_claim_fails_closed(self, db):
        _seed_task(db, "t_lease", status="ready")
        claimed = kb.claim_task(db, "t_lease", claimer="host:owner", ttl_seconds=60)
        assert claimed is not None
        with pytest.raises(kb.ClaimLeaseLost, match="refusing external result"):
            kb.require_claim_heartbeat(
                db, "t_lease", claimer="host:foreign", expected_run_id=claimed.current_run_id, ttl_seconds=60
            )

    def test_reclaim_and_reclaim_with_same_claimer_rejects_prior_run(self, db):
        _seed_task(db, "t_lease", status="ready")
        first = kb.claim_task(db, "t_lease", claimer="host:reused", ttl_seconds=60)
        assert first is not None and first.current_run_id is not None
        assert kb.reclaim_task(db, "t_lease", reason="canary reclaim")
        second = kb.claim_task(db, "t_lease", claimer="host:reused", ttl_seconds=60)
        assert second is not None and second.current_run_id != first.current_run_id
        with pytest.raises(kb.ClaimLeaseLost, match="refusing external result"):
            kb.require_claim_heartbeat(
                db, "t_lease", claimer="host:reused",
                expected_run_id=first.current_run_id, ttl_seconds=60,
            )
        assert kb.require_claim_heartbeat(
            db, "t_lease", claimer="host:reused",
            expected_run_id=second.current_run_id, ttl_seconds=60,
        ) is None

    def test_missing_or_ended_run_rolls_back_task_lease_renewal(self, db):
        _seed_task(db, "t_lease", status="ready")
        claimed = kb.claim_task(db, "t_lease", claimer="host:owner", ttl_seconds=60)
        assert claimed is not None and claimed.current_run_id is not None
        before = db.execute(
            "SELECT claim_expires FROM tasks WHERE id = 't_lease'"
        ).fetchone()["claim_expires"]
        run_before = db.execute(
            "SELECT claim_expires FROM task_runs WHERE id = ?", (claimed.current_run_id,)
        ).fetchone()["claim_expires"]
        with kb.write_txn(db):
            db.execute(
                "UPDATE task_runs SET status = 'done' WHERE id = ?",
                (claimed.current_run_id,),
            )
        with pytest.raises(kb.ClaimLeaseLost):
            kb.require_claim_heartbeat(
                db, "t_lease", claimer="host:owner",
                expected_run_id=claimed.current_run_id, ttl_seconds=600,
            )
        assert db.execute(
            "SELECT claim_expires FROM tasks WHERE id = 't_lease'"
        ).fetchone()["claim_expires"] == before
        assert db.execute(
            "SELECT claim_expires FROM task_runs WHERE id = ?", (claimed.current_run_id,)
        ).fetchone()["claim_expires"] == run_before
