"""Unit tests for hermes_cli.kanban_pg_queue.

These test the pure logic of the queue module with a lightweight fake
psycopg2 connection/cursor (no real DB) — SQL text, CAS rowcount handling,
fencing, backoff math, gate_reject vs genuine-failure semantics, and the
fail-open connection wrapper. Real-Postgres behavior (SKIP LOCKED exactly-once
arbitration, prune, idempotent upsert) is covered by the integration test in
tests/integration/test_kanban_pg_queue_pg.py against a disposable container.
"""
from __future__ import annotations

import os

import pytest

from hermes_cli import kanban_pg_queue as q


class FakeCursor:
    """Records execute() calls; returns canned rowcount / fetch results."""

    def __init__(self, rowcount=0, fetchone_result=None, fetchall_result=None):
        self.rowcount = rowcount
        self._fetchone = fetchone_result
        self._fetchall = fetchall_result
        self.executed = []
        self.executed_args = []

    def execute(self, sql, args=None):
        self.executed.append(sql)
        self.executed_args.append(args)
        return self

    def fetchone(self):
        return self._fetchone

    def fetchall(self):
        return self._fetchall

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConnection:
    def __init__(self, **cursors):
        # cursors: dict mapping a counter -> FakeCursor, or a callable
        self._cursors = list(cursors.get("all", []))
        self._next_cursor = cursors.get("next")
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self):
        if self._next_cursor is not None:
            return self._next_cursor
        if self._cursors:
            return self._cursors.pop(0)
        return FakeCursor()

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


def _row_id(id_, assignee="alpha"):
    class R:
        def __getitem__(self, k):
            return {"id": id_, "assignee": assignee}[k]
    return R()


# ---------------------------------------------------------------------------
# queue_enabled
# ---------------------------------------------------------------------------

def test_queue_enabled_requires_env_and_allowlist(monkeypatch):
    monkeypatch.delenv("KANBAN_CLAIM_QUEUE_URL", raising=False)
    monkeypatch.setattr(q, "_config", lambda: {"claim_queue_boards": ["jarvis-os"]})
    assert q.queue_enabled("jarvis-os") is False  # no env DSN


def test_queue_enabled_empty_allowlist_disables(monkeypatch):
    monkeypatch.setenv("KANBAN_CLAIM_QUEUE_URL", "postgresql://u:p@h/db")
    monkeypatch.setattr(q, "_config", lambda: {"claim_queue_boards": []})
    assert q.queue_enabled("jarvis-os") is False
    assert q.queue_enabled("default") is False


def test_queue_enabled_board_not_in_allowlist(monkeypatch):
    monkeypatch.setenv("KANBAN_CLAIM_QUEUE_URL", "postgresql://u:p@h/db")
    monkeypatch.setattr(q, "_config", lambda: {"claim_queue_boards": ["jarvis-os"]})
    assert q.queue_enabled("default") is False


def test_queue_enabled_true_when_env_and_allowlist(monkeypatch):
    monkeypatch.setenv("KANBAN_CLAIM_QUEUE_URL", "postgresql://u:p@h/db")
    monkeypatch.setattr(q, "_config", lambda: {"claim_queue_boards": ["jarvis-os"]})
    assert q.queue_enabled("jarvis-os") is True


def test_queue_enabled_false_when_psycopg2_unavailable(monkeypatch):
    monkeypatch.setenv("KANBAN_CLAIM_QUEUE_URL", "postgresql://u:p@h/db")
    monkeypatch.setattr(q, "_config", lambda: {"claim_queue_boards": ["jarvis-os"]})
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name == "psycopg2":
            raise ImportError("no psycopg2")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert q.queue_enabled("jarvis-os") is False


def test_queue_enabled_no_board_false(monkeypatch):
    monkeypatch.setenv("KANBAN_CLAIM_QUEUE_URL", "postgresql://u:p@h/db")
    monkeypatch.setattr(q, "_config", lambda: {"claim_queue_boards": ["jarvis-os"]})
    assert q.queue_enabled(None) is False


# ---------------------------------------------------------------------------
# queue_connect (fail-open)
# ---------------------------------------------------------------------------

def test_queue_connect_raises_queued_unavailable_no_env(monkeypatch):
    monkeypatch.delenv("KANBAN_CLAIM_QUEUE_URL", raising=False)
    with pytest.raises(q.QueueUnavailable):
        q.queue_connect()


def test_queue_connect_raises_queued_unavailable_on_connect_error(monkeypatch):
    monkeypatch.setenv("KANBAN_CLAIM_QUEUE_URL", "postgresql://u:p@127.0.0.1:1/nope")

    class Boom:
        def connect(self, *a, **kw):
            raise OSError("connection refused")

    import sys
    monkeypatch.setitem(sys.modules, "psycopg2", Boom())
    with pytest.raises(q.QueueUnavailable):
        q.queue_connect()


def test_queue_lease_seconds_min_and_default(monkeypatch):
    monkeypatch.setattr(q, "_config", lambda: {})
    assert q.queue_lease_seconds() == q.DEFAULT_LEASE_SECONDS
    monkeypatch.setattr(q, "_config", lambda: {"claim_queue_lease_seconds": 5})
    assert q.queue_lease_seconds() == q.MIN_LEASE_SECONDS  # clamped at 30


def test_backoff_seconds_formula():
    assert q.backoff_seconds(0) == 30
    assert q.backoff_seconds(1) == 60
    assert q.backoff_seconds(2) == 120
    assert q.backoff_seconds(3) == 240
    assert q.backoff_seconds(4) == 300  # capped at 300
    assert q.backoff_seconds(10) == 300


# ---------------------------------------------------------------------------
# claim_row (per-row CAS)
# ---------------------------------------------------------------------------

def test_claim_row_success_returns_attempts():
    cur = FakeCursor(rowcount=1, fetchone_result=(3,))
    pg = FakeConnection(next=cur)
    assert q.claim_row(pg, "jarvis-os", "t1", "host:1", 120) == 3
    # CAS WHERE includes status='queued' + cooldown, and sets claimed/lease
    sql = cur.executed[0]
    assert "status='claimed'" in sql
    assert "status='queued'" in sql
    assert "cooldown_until" in sql
    assert "claimed_by" in sql
    assert "claim_expires" in sql
    assert cur.executed_args[0][0] == "host:1"
    assert pg.commits == 1


def test_claim_row_not_claimable_returns_none():
    cur = FakeCursor(rowcount=0, fetchone_result=None)
    pg = FakeConnection(next=cur)
    assert q.claim_row(pg, "jarvis-os", "t1", "host:1", 120) is None
    assert pg.commits == 1


# ---------------------------------------------------------------------------
# claim_next (batch SKIP LOCKED, exactly-once arbitration)
# ---------------------------------------------------------------------------

def test_claim_next_returns_only_cas_successes():
    # Two selected rows; second CAS rowcount==0 (concurrent claim won) -> skipped
    cur = FakeCursor(
        rowcount=0,
        fetchall_result=[("t1", 0), ("t2", 2)],
    )

    class MultiCursor(FakeCursor):
        def __init__(self):
            self._call = 0
            self.executed = []
            self.executed_args = []

        def execute(self, sql, args=None):
            self.executed.append(sql)
            self.executed_args.append(args)
            self._call += 1
            if self._call == 1:  # the SELECT FOR UPDATE SKIP LOCKED
                self.rowcount = 2
            else:  # per-row CAS UPDATE; args = (claimer, now, now+lease, task_id)
                self.rowcount = 1 if args[3] == "t1" else 0
            return self

        def fetchall(self):
            return [("t1", 0), ("t2", 2)]

    pg = FakeConnection(next=MultiCursor())
    result = q.claim_next(pg, "jarvis-os", 2, "host:1", 120)
    assert result == [("t1", 0)]
    assert pg.commits == 1


# ---------------------------------------------------------------------------
# release_claim (fencing via claimed_by)
# ---------------------------------------------------------------------------

def test_release_claim_gate_reject_no_backoff_no_attempts():
    cur = FakeCursor(rowcount=1)
    pg = FakeConnection(next=cur)
    ok = q.release_claim(pg, "t1", "host:1", cooldown=0, gate_reject=True)
    assert ok is True
    sql = cur.executed[0]
    assert "claimed_by=%s" in sql
    assert "attempts" not in sql  # no increment on gate reject
    assert "cooldown_until" not in sql  # cooldown=0, no backoff
    assert cur.executed_args[0] == ("t1", "host:1")


def test_release_claim_genuine_failure_backoff_and_attempts():
    cur = FakeCursor(rowcount=1)
    pg = FakeConnection(next=cur)
    ok = q.release_claim(pg, "t1", "host:1", cooldown=60, gate_reject=False)
    assert ok is True
    sql = cur.executed[0]
    assert "attempts = attempts + 1" in sql
    assert "cooldown_until" in sql
    # args: (now+60, t1, host:1)
    assert cur.executed_args[0][1:] == ("t1", "host:1")


def test_release_claim_fenced_wrong_claimer_returns_false():
    cur = FakeCursor(rowcount=0)
    pg = FakeConnection(next=cur)
    assert q.release_claim(pg, "t1", "wrong-claimer", cooldown=0, gate_reject=True) is False


# ---------------------------------------------------------------------------
# delete_row (success terminal)
# ---------------------------------------------------------------------------

def test_delete_row_success():
    cur = FakeCursor(rowcount=1)
    pg = FakeConnection(next=cur)
    assert q.delete_row(pg, "t1", "host:1") is True
    assert "DELETE FROM kanban_claim_queue" in cur.executed[0]
    assert cur.executed_args[0] == ("t1", "host:1")


def test_delete_row_not_claimer_returns_false():
    cur = FakeCursor(rowcount=0)
    pg = FakeConnection(next=cur)
    assert q.delete_row(pg, "t1", "nope") is False


# ---------------------------------------------------------------------------
# sync_ready (idempotent prune + upsert)
# ---------------------------------------------------------------------------

def test_sync_ready_prunes_and_upserts():
    cur = FakeCursor(rowcount=1)
    pg = FakeConnection(next=cur)
    rows = [_row_id("t1", "alpha"), _row_id("t2", "beta")]
    q.sync_ready(pg, "jarvis-os", rows)
    assert len(cur.executed) == 3  # 1 DELETE + 2 INSERT ... ON CONFLICT
    assert "DELETE FROM kanban_claim_queue" in cur.executed[0]
    assert "ON CONFLICT (task_id, board)" in cur.executed[1]
    # unnest(%s::text[]) for the ready-id list in the prune
    assert "unnest(%s::text[])" in cur.executed[0]
    assert pg.commits == 1


def test_sync_ready_empty_ready_rows_prunes_all():
    cur = FakeCursor(rowcount=1)
    pg = FakeConnection(next=cur)
    q.sync_ready(pg, "jarvis-os", [])
    assert len(cur.executed) == 1
    assert cur.executed_args[0][1] == []  # empty ready-id list


# ---------------------------------------------------------------------------
# reconcile_stale (crash recovery)
# ---------------------------------------------------------------------------

def test_reconcile_stale_updates_expired_claims():
    cur = FakeCursor(rowcount=4)
    pg = FakeConnection(next=cur)
    n = q.reconcile_stale(pg, "jarvis-os")
    assert n == 4
    sql = cur.executed[0]
    assert "status='claimed'" in sql
    assert "claim_expires <" in sql
    assert "status='queued'" in sql


# ---------------------------------------------------------------------------
# queue_health
# ---------------------------------------------------------------------------

def test_queue_health_select_1():
    cur = FakeCursor(rowcount=1, fetchone_result=(1,))
    pg = FakeConnection(next=cur)
    assert q.queue_health(pg) is True
    assert "SELECT 1" in cur.executed[0]
