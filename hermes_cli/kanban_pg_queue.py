"""Postgres SKIP LOCKED claim-queue arbitration for the kanban dispatcher.

Pilot (jarvis-os board only). Postgres decides *which* ready candidate the
dispatcher attempts; SQLite ``kanban.db`` remains the board-of-record and the
authoritative task state machine (``claim_task`` / ``heartbeat_claim`` in
``hermes_cli/kanban_db.py`` are unchanged). PG rows are derived and disposable
-- they hold only queue-arbitration fields (status/claimer/lease/attempts), no
task state.

Fail-open by design: every entry point here can raise the typed
:class:`QueueUnavailable` (or a ``psycopg2.Error``) which the dispatcher tick
catches and falls back to the unchanged SQLite claim path.

Schema (dedicated ``hermes-claim-queue-pg`` container, never a tenant DB):

    CREATE TABLE IF NOT EXISTS kanban_claim_queue (
        task_id       TEXT PRIMARY KEY,
        board         TEXT NOT NULL DEFAULT 'jarvis-os',
        priority      INTEGER NOT NULL DEFAULT 0,
        enqueued_at   BIGINT  NOT NULL,          -- unix seconds
        status        TEXT NOT NULL DEFAULT 'queued',  -- queued | claimed
        claimed_by    TEXT,
        claimed_at    BIGINT,
        claim_expires BIGINT,                    -- arbitration lease (short)
        attempts      INTEGER NOT NULL DEFAULT 0,
        cooldown_until BIGINT NOT NULL DEFAULT 0,
        UNIQUE (task_id, board)
    );
    CREATE INDEX IF NOT EXISTS idx_queue_pick
      ON kanban_claim_queue (board, status, priority DESC, enqueued_at ASC)
      WHERE status = 'queued';
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, List, Optional, Sequence, Tuple

_log = logging.getLogger(__name__)

# Default arbitration lease (seconds) between PG-claim and SQLite-commit.
DEFAULT_LEASE_SECONDS = 120
MIN_LEASE_SECONDS = 30
# Round-robin fairness cap on the genuine-failure backoff: min(30*2^attempts,300)s.
MAX_COOLDOWN_SECONDS = 300
BACKOFF_BASE_SECONDS = 30

# The canonical schema (spec section 3). Idempotent.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS kanban_claim_queue (
    task_id       TEXT PRIMARY KEY,
    board         TEXT NOT NULL DEFAULT 'jarvis-os',
    priority      INTEGER NOT NULL DEFAULT 0,
    enqueued_at   BIGINT  NOT NULL,
    status        TEXT NOT NULL DEFAULT 'queued',
    claimed_by    TEXT,
    claimed_at    BIGINT,
    claim_expires BIGINT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    cooldown_until BIGINT NOT NULL DEFAULT 0,
    UNIQUE (task_id, board)
);
CREATE INDEX IF NOT EXISTS idx_queue_pick
  ON kanban_claim_queue (board, status, priority DESC, enqueued_at ASC)
  WHERE status = 'queued';
"""


class QueueUnavailable(Exception):
    """Raised when the claim queue cannot be reached or used.

    The dispatcher treats this as a fail-open signal: log a warning and fall
    through to the unchanged SQLite claim path for that tick.
    """


def _now_epoch() -> int:
    return int(time.time())


def backoff_seconds(attempts: int) -> int:
    """Genuine-failure backoff: ``min(30 * 2^attempts, 300)`` seconds."""
    return min(BACKOFF_BASE_SECONDS * (2 ** max(attempts, 0)), MAX_COOLDOWN_SECONDS)


def _config() -> dict:
    try:
        from hermes_cli.config import load_config
        cfg = load_config() or {}
    except Exception:
        cfg = {}
    return cfg.get("kanban", {}) or {}


def queue_enabled(board: Optional[str]) -> bool:
    """Return whether the PG claim queue is enabled for ``board``.

    Enabled only when BOTH: the env ``KANBAN_CLAIM_QUEUE_URL`` is set (a valid
    DSN) AND ``board`` is in config ``kanban.claim_queue_boards``. Missing /
    invalid DSN or empty allowlist -> False (queue disabled fleet-wide). Also
    False if psycopg2 is unavailable.
    """
    if not board:
        return False
    dsn = (os.environ.get("KANBAN_CLAIM_QUEUE_URL") or "").strip()
    if not dsn:
        return False
    try:
        import psycopg2  # noqa: F401
    except Exception:
        return False
    allowlist = _config().get("claim_queue_boards") or []
    return board in allowlist


def queue_lease_seconds() -> int:
    """Arbitration lease from config (``kanban.claim_queue_lease_seconds``)."""
    try:
        lease = int(_config().get("claim_queue_lease_seconds", DEFAULT_LEASE_SECONDS))
    except (TypeError, ValueError):
        lease = DEFAULT_LEASE_SECONDS
    return max(lease, MIN_LEASE_SECONDS)


def _ensure_schema(pg) -> None:
    """Create the table + pick index if missing. Idempotent."""
    with pg.cursor() as cur:
        cur.execute(_SCHEMA)
    pg.commit()


def queue_connect() -> Any:
    """Open a psycopg2 connection to the claim queue.

    Short timeouts so a dead/stopped PG container fails fast (fail-open).
    Connection-level errors are wrapped into :class:`QueueUnavailable`.
    Autocommit is off (we use explicit transactions per statement).
    """
    try:
        import psycopg2
    except Exception as exc:  # pragma: no cover - environment
        raise QueueUnavailable(f"psycopg2 unavailable: {exc}") from exc
    dsn = (os.environ.get("KANBAN_CLAIM_QUEUE_URL") or "").strip()
    if not dsn:
        raise QueueUnavailable("KANBAN_CLAIM_QUEUE_URL is not set")
    try:
        pg = psycopg2.connect(
            dsn,
            connect_timeout=3,
            options="-c statement_timeout=5000",
        )
    except Exception as exc:
        raise QueueUnavailable(f"connect failed: {exc}") from exc
    try:
        _ensure_schema(pg)
    except Exception:
        try:
            pg.close()
        except Exception:
            pass
        raise
    return pg


def _ready_priority_and_ts() -> Tuple[int, int]:
    """Priority/enqueued_at for a freshly synced ready row.

    The dispatch loop iterates the SQLite-ordered ``ready_rows`` snapshot for
    fairness, so PG ``priority``/``enqueued_at`` are informational here; the
    pilot keeps them deterministic (priority 0, enqueued now).
    """
    return 0, _now_epoch()


def sync_ready(pg, board: str, ready_rows: Sequence[Any]) -> None:
    """Mirror the SQLite ready set into PG (idempotent prune + upsert).

    ``ready_rows`` is the same ``SELECT id, assignee FROM tasks WHERE
    status='ready' AND claim_lock IS NULL ...`` snapshot the SQLite claim loop
    uses (spec section 6.1). Prune drops ``queued`` rows whose task is no
    longer ready; upsert re-queues never touches rows already ``claimed``
    (arbitration in flight). Same task enqueued twice -> no duplicate row.
    """
    ready_ids = [str(row["id"]) for row in ready_rows]
    with pg.cursor() as cur:
        # prune: drop queued rows whose task is no longer ready in SQLite
        cur.execute(
            "DELETE FROM kanban_claim_queue "
            " WHERE board = %s AND status = 'queued' "
            "   AND task_id NOT IN (SELECT unnest(%s::text[]))",
            (board, ready_ids),
        )
        for row in ready_rows:
            task_id = str(row["id"])
            priority, enqueued_at = _ready_priority_and_ts()
            cur.execute(
                "INSERT INTO kanban_claim_queue "
                "  (task_id, board, priority, enqueued_at) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (task_id, board) DO UPDATE SET priority = EXCLUDED.priority "
                " WHERE kanban_claim_queue.status = 'queued'",
                (task_id, board, priority, enqueued_at),
            )
    pg.commit()


def claim_row(pg, board: str, task_id: str, claimer: str, lease_seconds: int) -> Optional[int]:
    """Per-row SKIP LOCKED-equivalent CAS claim (dispatch-loop primitive).

    Single atomic UPDATE provides the arbitration: the row is locked for the
    duration of its own statement and the ``WHERE status='queued'`` CAS makes
    concurrent claims mutually exclusive. Returns the row's current ``attempts``
    on a successful ``queued->claimed`` transition, or ``None`` when the row is
    not claimable this tick (claimed by another dispatcher, or in cooldown from
    a prior genuine-failure backoff). Never raises for a mere not-claimable;
    only PG errors raise. Does NOT commit -- caller controls the transaction.
    """
    now = _now_epoch()
    with pg.cursor() as cur:
        cur.execute(
            "UPDATE kanban_claim_queue "
            "   SET status='claimed', claimed_by=%s, "
            "       claimed_at=%s, claim_expires=%s "
            " WHERE task_id=%s AND board=%s AND status='queued' "
            "   AND cooldown_until <= %s "
            " RETURNING attempts",
            (claimer, now, now + int(lease_seconds), task_id, board, now),
        )
        row = cur.fetchone()
    pg.commit()
    if row is None:
        return None
    return int(row[0])


def claim_next(pg, board: str, n: int, claimer: str, lease_seconds: int) -> List[Tuple[str, int]]:
    """Batch SKIP LOCKED pick + CAS (exactly-once arbitration primitive).

    Used by the AC5 integration test (two concurrent transactions over a 100-row
    queue claim 100 distinct rows) and reserved for future multi-dispatcher
    batching. The dispatch loop does NOT use this (a single non-refilled batch
    under-spawns when rejectable rows occupy the head) -- it uses
    :func:`claim_row` per candidate instead.

    Returns the list of ``(task_id, attempts)`` pairs whose CAS rowcount == 1.
    ``attempts`` is the row's pre-increment value (feeds the backoff formula on a
    genuine claim failure).
    """
    now = _now_epoch()
    picked = []
    with pg.cursor() as cur:
        cur.execute(
            "SELECT task_id, attempts FROM kanban_claim_queue "
            " WHERE board = %s AND status = 'queued' "
            "   AND cooldown_until <= %s "
            " ORDER BY priority DESC, enqueued_at ASC LIMIT %s "
            " FOR UPDATE SKIP LOCKED",
            (board, now, int(n)),
        )
        selected = cur.fetchall()
        for task_id, attempts in selected:
            cur.execute(
                "UPDATE kanban_claim_queue "
                "   SET status='claimed', claimed_by=%s, "
                "       claimed_at=%s, claim_expires=%s "
                " WHERE task_id=%s AND status='queued'",
                (claimer, now, now + int(lease_seconds), task_id),
            )
            if cur.rowcount == 1:
                picked.append((str(task_id), int(attempts)))
    pg.commit()
    return picked


def release_claim(pg, task_id: str, claimer: str, cooldown: int = 0, gate_reject: bool = False) -> bool:
    """Release a claimed row back to ``queued`` (fenced on ``claimed_by``).

    Only the claimer may release -- a stale/duplicate dispatcher cannot clobber
    someone else's arbitration.

    ``gate_reject=True`` (cooldown=0, no ``attempts`` increment): a pre-claim
    gate rejected the candidate; the row returns to ``queued`` untouched so it
    stays available, and the attempt-count/backoff is NOT consumed because no
    claim was genuinely attempted.

    ``gate_reject=False`` (backoff): the authoritative SQLite ``claim_task``
    returned None; release with backoff so the task yields the queue head
    (round-robin fairness). ``cooldown`` is the computed backoff in seconds.
    """
    now = _now_epoch()
    with pg.cursor() as cur:
        if gate_reject:
            cur.execute(
                "UPDATE kanban_claim_queue "
                "   SET status='queued', claimed_by=NULL, claimed_at=NULL, "
                "       claim_expires=NULL "
                " WHERE task_id=%s AND claimed_by=%s",
                (task_id, claimer),
            )
        else:
            cur.execute(
                "UPDATE kanban_claim_queue "
                "   SET status='queued', claimed_by=NULL, claimed_at=NULL, "
                "       claim_expires=NULL, attempts = attempts + 1, "
                "       cooldown_until = %s "
                " WHERE task_id=%s AND claimed_by=%s",
                (now + int(cooldown), task_id, claimer),
            )
        released = cur.rowcount == 1
    pg.commit()
    return released


def delete_row(pg, task_id: str, claimer: str) -> bool:
    """Remove the PG row after the SQLite claim + spawn succeeded.

    ``claimed -> deleted`` is the success terminal of the row lifecycle. SQLite
    is authoritative either way (a leftover row would merely be pruned /
    reconciled later), but deleting on success keeps the queue clean.
    """
    with pg.cursor() as cur:
        cur.execute(
            "DELETE FROM kanban_claim_queue WHERE task_id=%s AND claimed_by=%s",
            (task_id, claimer),
        )
        deleted = cur.rowcount == 1
    pg.commit()
    return deleted


def reconcile_stale(pg, board: str) -> int:
    """Return lease-expired ``claimed`` rows to ``queued`` (crash recovery).

    If a dispatcher crashes between PG-claim and SQLite-commit, the row expires
    and is reconciled to ``queued`` next tick; SQLite still shows ``ready``
    (the crash happened before the authoritative commit), so no task is ever
    lost or double-run. Returns the number of rows reconciled.
    """
    now = _now_epoch()
    with pg.cursor() as cur:
        cur.execute(
            "UPDATE kanban_claim_queue SET status='queued', claimed_by=NULL, "
            "       claimed_at=NULL, claim_expires=NULL "
            " WHERE board=%s AND status='claimed' AND claim_expires < %s",
            (board, now),
        )
        count = cur.rowcount
    pg.commit()
    return int(count)


def queue_health(pg) -> bool:
    """Cheap liveness probe (``SELECT 1``). Raises on failure (fail-open)."""
    with pg.cursor() as cur:
        cur.execute("SELECT 1")
        cur.fetchone()
    pg.rollback()
    return True
