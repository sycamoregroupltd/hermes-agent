"""Integration tests for the Postgres SKIP LOCKED claim-queue pilot.

These run against a DISPOSABLE postgres:16-alpine container on a private
loopback port (test-only; torn down after; NEVER a tenant DB). They exercise
the real SQLite dispatch path with the PG queue enabled, plus the real-PG
arbitration primitives (SKIP LOCKED exactly-once, idempotent sync, prune,
release fencing, reconcile).

Requires the container to be running (see tests/integration/README or the
task spec) with KANBAN_CLAIM_QUEUE_URL exported, e.g.:
    KANBAN_CLAIM_QUEUE_URL=postgresql://hermes:hermes@127.0.0.1:55433/claimqueue
"""
from __future__ import annotations

import os
import sys
import tempfile

import pytest

pytestmark = pytest.mark.integration

# The integration suite is skipped unless a disposable PG is reachable.
_TEST_DSN = os.environ.get(
    "KANBAN_CLAIM_QUEUE_URL",
    "postgresql://hermes:hermes@127.0.0.1:55433/claimqueue",
)


def _pg_available():
    try:
        import psycopg2
        conn = psycopg2.connect(_TEST_DSN, connect_timeout=2)
        conn.close()
        return True
    except Exception:
        return False


needs_pg = pytest.mark.skipif(not _pg_available(), reason="disposable PG not reachable")


@pytest.fixture()
def isolated_kanban_home(monkeypatch):
    """Fresh HERMES_HOME + queue env pointed at the disposable PG."""
    test_home = tempfile.mkdtemp(prefix="kanban_pg_queue_it_")
    for prof in ("alpha", "beta", "gamma", "default"):
        os.makedirs(os.path.join(test_home, "profiles", prof), exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", test_home)
    monkeypatch.setenv("KANBAN_CLAIM_QUEUE_URL", _TEST_DSN)
    # CRITICAL: the kanban dispatcher (this worker) sets HERMES_KANBAN_DB /
    # HERMES_KANBAN_BOARD, which OVERRIDE board-path resolution and would point
    # every test board at the REAL jarvis-os board DB. Unset them so the
    # isolated HERMES_HOME's own kanban.db is used (test isolation hard gate).
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_WORKSPACES_ROOT", raising=False)
    for mod in list(sys.modules.keys()):
        if mod.startswith("hermes_cli") or mod.startswith("hermes_state") or mod == "hermes_constants":
            del sys.modules[mod]
    from hermes_cli import kanban_db
    yield kanban_db, test_home


def _fake_spawn(*args, **kwargs):
    return 12345


# ---------------------------------------------------------------------------
# Real-PG primitive behavior (AC5 exactly-once, sync idempotency, prune,
# release fencing, reconcile) exercised directly against the container.
# ---------------------------------------------------------------------------

@needs_pg
def test_sync_ready_idempotent_and_prune(isolated_kanban_home):
    kb, _home = isolated_kanban_home
    from hermes_cli import kanban_pg_queue as q

    pg = q.queue_connect()
    try:
        # fresh table
        with pg.cursor() as cur:
            cur.execute("DELETE FROM kanban_claim_queue")
        pg.commit()

        class R:
            def __init__(self, id_):
                self._id = id_

            def __getitem__(self, k):
                return {"id": self._id}[k]

        # sync twice -> no duplicate rows (idempotent upsert)
        q.sync_ready(pg, "jarvis-os", [R("t1"), R("t2")])
        q.sync_ready(pg, "jarvis-os", [R("t1"), R("t2")])
        with pg.cursor() as cur:
            cur.execute("SELECT count(*) FROM kanban_claim_queue WHERE board='jarvis-os'")
            assert cur.fetchone()[0] == 2

        # prune: sync with only t1 -> t2 (no longer ready) disappears
        q.sync_ready(pg, "jarvis-os", [R("t1")])
        with pg.cursor() as cur:
            cur.execute("SELECT task_id FROM kanban_claim_queue WHERE board='jarvis-os'")
            assert [r[0] for r in cur.fetchall()] == ["t1"]
    finally:
        pg.close()


@needs_pg
def test_claim_release_reclaim_cycle_and_fencing(isolated_kanban_home):
    kb, _home = isolated_kanban_home
    from hermes_cli import kanban_pg_queue as q

    pg = q.queue_connect()
    try:
        with pg.cursor() as cur:
            cur.execute("DELETE FROM kanban_claim_queue")
        pg.commit()
        q.sync_ready(pg, "jarvis-os", [type("R", (), {"__getitem__": lambda self, k: {"id": "tX"}[k]})()])

        # claim_row -> claimed
        attempts = q.claim_row(pg, "jarvis-os", "tX", "host:1", 120)
        assert attempts == 0
        with pg.cursor() as cur:
            cur.execute("SELECT status, claimed_by FROM kanban_claim_queue WHERE task_id='tX'")
            assert cur.fetchone() == ("claimed", "host:1")

        # fencing: wrong claimer cannot release
        assert q.release_claim(pg, "tX", "host:2", cooldown=0, gate_reject=True) is False
        with pg.cursor() as cur:
            cur.execute("SELECT status FROM kanban_claim_queue WHERE task_id='tX'")
            assert cur.fetchone()[0] == "claimed"

        # gate_reject release returns to queued, attempts unchanged
        assert q.release_claim(pg, "tX", "host:1", cooldown=0, gate_reject=True) is True
        with pg.cursor() as cur:
            cur.execute("SELECT status, attempts FROM kanban_claim_queue WHERE task_id='tX'")
            assert cur.fetchone() == ("queued", 0)

        # genuine-failure release increments attempts + sets cooldown
        q.claim_row(pg, "jarvis-os", "tX", "host:1", 120)
        assert q.release_claim(pg, "tX", "host:1", cooldown=q.backoff_seconds(0), gate_reject=False) is True
        with pg.cursor() as cur:
            cur.execute("SELECT status, attempts, cooldown_until FROM kanban_claim_queue WHERE task_id='tX'")
            status, attempts, cooldown_until = cur.fetchone()
            assert status == "queued"
            assert attempts == 1
            assert cooldown_until > 0

        # in-cooldown: claim_row returns None (not claimable this tick)
        assert q.claim_row(pg, "jarvis-os", "tX", "host:1", 120) is None

        # delete_row success terminal: clear the cooldown so we can claim again
        with pg.cursor() as cur:
            cur.execute(
                "UPDATE kanban_claim_queue SET cooldown_until=0 "
                "WHERE task_id='tX'"
            )
        pg.commit()
        q.claim_row(pg, "jarvis-os", "tX", "host:1", 120)
        assert q.delete_row(pg, "tX", "host:1") is True
        with pg.cursor() as cur:
            cur.execute("SELECT count(*) FROM kanban_claim_queue WHERE task_id='tX'")
            assert cur.fetchone()[0] == 0
    finally:
        pg.close()


@needs_pg
def test_reconcile_stale_returns_expired_to_queued(isolated_kanban_home):
    kb, _home = isolated_kanban_home
    from hermes_cli import kanban_pg_queue as q

    pg = q.queue_connect()
    try:
        with pg.cursor() as cur:
            cur.execute("DELETE FROM kanban_claim_queue")
            cur.execute(
                "INSERT INTO kanban_claim_queue "
                "(task_id, board, priority, enqueued_at, status, claimed_by, claim_expires) "
                "VALUES ('t_stale', 'jarvis-os', 0, %s, 'claimed', 'host:dead', %s)",
                (int(__import__("time").time()), int(__import__("time").time()) - 5),
            )
        pg.commit()
        n = q.reconcile_stale(pg, "jarvis-os")
        assert n == 1
        with pg.cursor() as cur:
            cur.execute("SELECT status, claimed_by FROM kanban_claim_queue WHERE task_id='t_stale'")
            assert cur.fetchone() == ("queued", None)
    finally:
        pg.close()


@needs_pg
def test_claim_next_exactly_once_two_concurrent_transactions(isolated_kanban_home):
    """AC5: two concurrent claim_next transactions over a 100-row queue claim
    100 distinct rows, never a duplicate."""
    kb, _home = isolated_kanban_home
    from hermes_cli import kanban_pg_queue as q
    import threading

    pg = q.queue_connect()
    try:
        with pg.cursor() as cur:
            cur.execute("DELETE FROM kanban_claim_queue")
        pg.commit()
        # enqueue 100 ready rows
        class R:
            def __init__(self, id_):
                self._id = id_

            def __getitem__(self, k):
                return {"id": self._id}[k]
        q.sync_ready(pg, "jarvis-os", [R(f"t{i:03d}") for i in range(100)])

        results = []
        errors = []

        def worker(claimer):
            try:
                conn = q.queue_connect()
                try:
                    batch = q.claim_next(conn, "jarvis-os", 100, claimer, 120)
                    results.extend(batch)
                finally:
                    conn.close()
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        # Two concurrent workers each try to claim all 100; SKIP LOCKED +
        # the status='queued' CAS guarantees no overlap.
        t1 = threading.Thread(target=worker, args=("host:A",))
        t2 = threading.Thread(target=worker, args=("host:B",))
        t1.start(); t2.start(); t1.join(); t2.join()

        assert not errors, errors
        task_ids = [tid for tid, _attempts in results]
        assert len(task_ids) == 100, f"expected 100 distinct claims, got {len(task_ids)}"
        assert len(set(task_ids)) == 100, "duplicate claim detected"
        # Every claimed row is claimed by exactly one claimer (fencing).
        with pg.cursor() as cur:
            cur.execute(
                "SELECT task_id, claimed_by FROM kanban_claim_queue "
                "WHERE status='claimed'"
            )
            claimed = cur.fetchall()
        assert len(claimed) == 100
        assert all(cb in ("host:A", "host:B") for _tid, cb in claimed)
    finally:
        pg.close()


# ---------------------------------------------------------------------------
# AC7 gate-ordering regression through the REAL dispatch path (queue enabled)
# ---------------------------------------------------------------------------

@needs_pg
def test_pg_dispatch_gate_ordering_no_stranded_running(isolated_kanban_home, monkeypatch):
    """AC7: with the flag on and ready_budget pinned to 1, a per-profile-capped
    row at the head is gate-rejected BEFORE claim_task (never running in SQLite),
    its PG row returns to queued with attempts unchanged, and the eligible task
    behind it is still claimed+spawned in the SAME tick (the loop scans past the
    gate-reject without consuming the spawn budget)."""
    kb, _home = isolated_kanban_home
    from hermes_cli import kanban_pg_queue as q

    # Force queue enabled for the "default" board regardless of allowlist config
    # (config.load_config reads HERMES_HOME; we pin the allowlist via the module).
    monkeypatch.setattr(
        q, "_config",
        lambda: {"claim_queue_boards": ["default"], "claim_queue_lease_seconds": 120},
    )

    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        # A separate running alpha task occupies the per-profile cap.
        run_alpha = kb.create_task(conn, title="run-alpha", assignee="alpha")
        # Head ready row: alpha is at its per-profile cap -> skipped_per_profile_capped.
        capped_id = kb.create_task(conn, title="capped", assignee="alpha")
        # Guarded ready row: beta (not at cap) has a recent PR comment -> respawn guard.
        guarded_id = kb.create_task(conn, title="guarded", assignee="beta")
        # Eligible ready row behind the two rejectable rows, assigned to gamma.
        eligible_id = kb.create_task(conn, title="eligible", assignee="gamma")

    # Make alpha at its per-profile cap: one legitimately-running alpha task,
    # claimed via the real CAS so it has proper claim_lock/claim_expires and is
    # NOT reconciled as an orphan by reconcile_orphaned_running.
    with kb.connect_closing() as conn:
        claimed = kb.claim_task(conn, run_alpha, ttl_seconds=3600)
        assert claimed is not None
        # Give beta a recent PR-url comment so the respawn guard rejects guarded.
        import time as _t
        with kb.write_txn(conn):
            conn.execute(
                "INSERT INTO task_comments (task_id, author, body, created_at) "
                "VALUES (?, ?, ?, ?)",
                (guarded_id, "tester", "see https://github.com/o/r/pull/1234", int(_t.time())),
            )

    # Pin ready_budget=1 via max_spawn=2 (1 alpha already running, so
    # spawn_budget=1 -> ready_budget=1) with no review rows. The loop must scan
    # past capped + guarded (neither consumes the spawn budget) and still
    # claim+spawn eligible.
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=False,
            max_spawn=2, max_in_progress_per_profile=1, board="default",
        )

    # Gate rejects land in the right buckets and NEVER become running in SQLite.
    capped_bucket = [c[0] for c in res.skipped_per_profile_capped]
    assert capped_id in capped_bucket, res.skipped_per_profile_capped
    guarded_bucket = [g[0] for g in res.respawn_guarded]
    assert guarded_id in guarded_bucket, res.respawn_guarded

    with kb.connect_closing() as conn:
        rows = {r["id"]: r["status"] for r in conn.execute(
            "SELECT id, status FROM tasks WHERE id IN (?,?,?)",
            (capped_id, guarded_id, eligible_id),
        )}
    # Gate-rejected tasks stay ready (NOT running) in SQLite.
    assert rows[capped_id] == "ready", rows
    assert rows[guarded_id] == "ready", rows

    # The eligible task behind the rejects is claimed+spawned the SAME tick
    # (round-4 budget pin: loop scans past gate-rejects without consuming budget).
    assert len(res.spawned) == 1
    assert res.spawned[0][0] == eligible_id, res.spawned
    assert rows[eligible_id] == "running"

    # PG: gate-rejected rows returned to queued with attempts unchanged (0);
    # the successfully spawned eligible row was DELETED on success.
    pg = q.queue_connect()
    try:
        with pg.cursor() as cur:
            cur.execute(
                "SELECT task_id, status, attempts FROM kanban_claim_queue "
                "WHERE board='default'"
            )
            pg_rows = {(r[0], r[1], r[2]) for r in cur.fetchall()}
    finally:
        pg.close()
    assert (capped_id, "queued", 0) in pg_rows, pg_rows
    assert (guarded_id, "queued", 0) in pg_rows, pg_rows
    assert all(r[0] != eligible_id for r in pg_rows), pg_rows  # deleted on success


@needs_pg
def test_pg_dispatch_default_assignee_capped_against_default_profile(isolated_kanban_home, monkeypatch):
    """AC7 fixture variant (round-3): an unassigned ready task whose fallback
    default profile is at its per-profile cap must be bucketed
    skipped_per_profile_capped against the DEFAULT profile -- never a ValueError
    from profile_exists(\"\"), never running in SQLite, never mis-bucketed
    skipped_unassigned."""
    kb, _home = isolated_kanban_home
    from hermes_cli import kanban_pg_queue as q
    monkeypatch.setattr(
        q, "_config",
        lambda: {"claim_queue_boards": ["default"], "claim_queue_lease_seconds": 120},
    )

    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = kb.create_task(conn, title="unassigned", assignee=None)

    # default profile at its per-profile cap (1 running default task).
    with kb.connect_closing() as conn:
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='running', assignee='default' "
                "WHERE id = ?", (task_id,),
            )
        # Create a fresh unassigned ready task to dispatch against.
        task_id = kb.create_task(conn, title="unassigned2", assignee=None)

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=False,
            default_assignee="default", max_in_progress_per_profile=1,
            board="default",
        )

    # The default-assigned task evaluates against the DEFAULT profile, which is
    # at its cap -> skipped_per_profile_capped (never ValueError / unassigned).
    assert task_id not in res.skipped_unassigned, res.skipped_unassigned
    capped = [c[0] for c in res.skipped_per_profile_capped]
    assert task_id in capped, res.skipped_per_profile_capped

    with kb.connect_closing() as conn:
        row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    assert row["status"] == "ready", row  # never running


# ---------------------------------------------------------------------------
# AC3 rollback: with the queue DISABLED (env unset), dispatch is byte-for-byte
# the pre-pilot SQLite path.
# ---------------------------------------------------------------------------

def test_rollback_disabled_queue_uses_sqlite_path(isolated_kanban_home, monkeypatch):
    """AC3: unsetting KANBAN_CLAIM_QUEUE_URL returns the dispatcher to the
    pre-pilot SQLite claim path with no residual PG interaction."""
    kb, _home = isolated_kanban_home
    from hermes_cli import kanban_pg_queue as q
    monkeypatch.delenv("KANBAN_CLAIM_QUEUE_URL", raising=False)
    # Even if the allowlist says enabled, no env DSN -> queue disabled.
    monkeypatch.setattr(q, "_config", lambda: {"claim_queue_boards": ["default"]})
    assert q.queue_enabled("default") is False

    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        tid = kb.create_task(conn, title="t1", assignee="alpha")
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False, board="default")
    assert len(res.spawned) == 1
    assert res.spawned[0][0] == tid
    with kb.connect_closing() as conn:
        row = conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()
    assert row["status"] == "running"
