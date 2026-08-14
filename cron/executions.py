"""Profile-local durable audit ledger for cron execution attempts.

The ledger records what is known about each attempt; it is not a retry queue.
Interrupted attempts become ``unknown`` only after their exact owner process is
proved gone. Terminal states are immutable.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

from hermes_constants import get_hermes_home
from hermes_time import now as _hermes_now

logger = logging.getLogger(__name__)

EXECUTIONS_FILE = get_hermes_home().resolve() / "cron" / "executions.db"
MAX_TERMINAL_EXECUTIONS = 1000
_TERMINAL_STATES = ("completed", "failed", "unknown")
_lock = threading.RLock()
_PROCESS_ID = uuid.uuid4().hex


def _connect() -> sqlite3.Connection:
    EXECUTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(EXECUTIONS_FILE, timeout=5)


def _initialize_schema(conn: sqlite3.Connection) -> None:
    from hermes_state import apply_wal_with_fallback

    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    apply_wal_with_fallback(conn, db_label="cron/executions.db")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS executions (
             id TEXT PRIMARY KEY,
             job_id TEXT NOT NULL,
             source TEXT NOT NULL,
             process_id TEXT NOT NULL,
             pid INTEGER NOT NULL,
             process_started_at INTEGER,
             status TEXT NOT NULL CHECK(status IN
               ('claimed','running','completed','failed','unknown')),
             claimed_at TEXT NOT NULL,
             started_at TEXT,
             finished_at TEXT,
             error TEXT
           )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_executions_job_claimed "
        "ON executions(job_id, claimed_at DESC, id DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_executions_status_claimed "
        "ON executions(status, claimed_at DESC, id DESC)"
    )


@contextmanager
def _transaction() -> Iterator[sqlite3.Connection]:
    """Open a connection, commit/rollback on exit, always close.

    ``sqlite3.Connection.__enter__``/``__exit__`` only commit or roll back
    the transaction; it does not close the connection. Relying on that alone
    leaks a connection (and its WAL/SHM file descriptors) on every call,
    since closing then depends on the garbage collector. Schema init runs
    inside the ``try`` too, so a PRAGMA/DDL failure after a successful
    ``connect()`` still closes the connection instead of leaking it.
    """
    with _lock:
        conn = _connect()
        try:
            _initialize_schema(conn)
            with conn:
                yield conn
        finally:
            conn.close()


def _record(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    return dict(row) if row is not None else None


def _emit_execution_state(
    record: Optional[Dict[str, Any]], *, delivery_outcome: Optional[str] = None
) -> None:
    """Project durable state to monitoring without affecting ledger behavior."""
    try:
        from agent.monitoring.cron_health import emit_execution_state

        emit_execution_state(record, delivery_outcome=delivery_outcome)
    except Exception:
        pass


def _process_start_time(pid: int) -> Optional[int]:
    try:
        from gateway.status import get_process_start_time
        return get_process_start_time(pid)
    except Exception:
        return None


def _owner_is_live(pid: int, started_at: Optional[int]) -> bool:
    try:
        from gateway.status import _pid_exists
        if not _pid_exists(pid):
            return False
    except Exception:
        return True  # fail safe: inability to prove death must not rewrite state
    if started_at is None:
        return pid == os.getpid()
    current = _process_start_time(pid)
    return current is not None and current == started_at


def _prune_unlocked(conn: sqlite3.Connection) -> None:
    limit = max(0, int(MAX_TERMINAL_EXECUTIONS))
    conn.execute(
        """DELETE FROM executions WHERE id IN (
             SELECT id FROM executions
             WHERE status IN ('completed','failed','unknown')
             ORDER BY claimed_at DESC, id DESC LIMIT -1 OFFSET ?
           )""",
        (limit,),
    )


def create_execution(job_id: str, *, source: str) -> Dict[str, Any]:
    """Persist a claimed attempt before executor/provider dispatch."""
    now = _hermes_now().isoformat()
    execution_id = uuid.uuid4().hex
    pid = os.getpid()
    with _transaction() as conn:
        conn.execute(
            """INSERT INTO executions
               (id, job_id, source, process_id, pid, process_started_at,
                status, claimed_at)
               VALUES (?, ?, ?, ?, ?, ?, 'claimed', ?)""",
            (execution_id, str(job_id), str(source), _PROCESS_ID, pid,
             _process_start_time(pid), now),
        )
        row = conn.execute(
            "SELECT * FROM executions WHERE id=?", (execution_id,)
        ).fetchone()
    record = _record(row)
    _emit_execution_state(record)
    return record  # type: ignore[return-value]


def mark_execution_running(execution_id: str) -> Optional[Dict[str, Any]]:
    """Transition one claimed attempt to running exactly once."""
    now = _hermes_now().isoformat()
    with _transaction() as conn:
        cur = conn.execute(
            """UPDATE executions SET status='running', started_at=?
               WHERE id=? AND status='claimed'""",
            (now, execution_id),
        )
        if cur.rowcount != 1:
            return None
        record = _record(conn.execute(
            "SELECT * FROM executions WHERE id=?", (execution_id,)
        ).fetchone())
    _emit_execution_state(record)
    return record


def finish_execution(
    execution_id: str, *, success: bool, error: Optional[str] = None,
    delivery_outcome: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Write a terminal result once; terminal attempts cannot be rewritten."""
    now = _hermes_now().isoformat()
    status = "completed" if success else "failed"
    detail = None if success else (str(error) if error else "unknown failure")
    with _transaction() as conn:
        cur = conn.execute(
            """UPDATE executions SET status=?, finished_at=?, error=?
               WHERE id=? AND status IN ('claimed','running')""",
            (status, now, detail, execution_id),
        )
        if cur.rowcount != 1:
            return None
        _prune_unlocked(conn)
        record = _record(conn.execute(
            "SELECT * FROM executions WHERE id=?", (execution_id,)
        ).fetchone())
    _emit_execution_state(record, delivery_outcome=delivery_outcome)
    return record


def recover_interrupted_executions() -> int:
    """Mark provably abandoned attempts unknown without scheduling retries."""
    now = _hermes_now().isoformat()
    changed = 0
    recovered: List[Dict[str, Any]] = []
    with _transaction() as conn:
        rows = conn.execute(
            """SELECT id, process_id, pid, process_started_at FROM executions
               WHERE status IN ('claimed','running')"""
        ).fetchall()
        for row in rows:
            if row["process_id"] == _PROCESS_ID:
                continue
            if _owner_is_live(int(row["pid"]), row["process_started_at"]):
                continue
            cur = conn.execute(
                """UPDATE executions SET status='unknown', finished_at=?, error=?
                   WHERE id=? AND status IN ('claimed','running')""",
                (now,
                 "Scheduler restarted after this execution's owner exited before a durable "
                 "terminal state; whether side effects ran is unknown.",
                 row["id"]),
            )
            changed += cur.rowcount
            if cur.rowcount:
                record = _record(conn.execute(
                    "SELECT * FROM executions WHERE id=?", (row["id"],)
                ).fetchone())
                if record is not None:
                    recovered.append(record)
        if changed:
            _prune_unlocked(conn)
    for record in recovered:
        _emit_execution_state(record)
    return changed


# Per-tick sweep of stale execution rows whose owning pid is provably dead.
# ``recover_interrupted_executions`` only runs once per gateway/process restart,
# so a ``source=direct`` (or any) row created AFTER the current process's last
# restart — and whose owner process died without finishing it — was never
# terminalized: it sat ``running`` forever (the fleet-analyst 85bc89e5241f
# class, see t_84b68726). This sweep closes that gap by re-running the same
# dead-pid terminalization on every tick, gated by a ``started_at`` age so we
# only touch rows that have been running well past a healthy window (and never
# a row still owned by a live pid). Idempotent: terminal states are immutable
# and the UPDATE WHERE guard makes a no-op on already-terminal rows.
_STALE_DIRECT_MIN_AGE_HOURS = 2
_STALE_TERMINALIZED_LOG = "cron.executions.stale_terminalized"

# Module-level counter for probe visibility. Exposed for unified-health / the
# jarvis watchdog to read via ``cron.executions.stale_terminalized_count()`` so
# a stale row that gets reclaimed mid-cycle is observable out-of-process.
_stale_terminalized_counter = 0


def _record_stale_terminalization(record: Dict[str, Any]) -> None:
    """Mirror a terminalized stale row to a JSONL probe (best-effort).

    Surface only a count + opaque job_key — not raw pids/job ids — matching
    cron_health's content-free contract. The row id is included only to
    disambiguate within a single tick's batch.
    """
    try:
        key = "sha256:" + hashlib.sha256(
            str(record.get("job_id") or "unknown").encode("utf-8", errors="replace")
        ).hexdigest()[:24]
        entry = {
            "job_key": key,
            "source": record.get("source"),
            "status": record.get("status"),
            "row": str(record.get("id")),
            "at": _hermes_now().isoformat(),
        }
        path = EXECUTIONS_FILE.parent / "stale_terminalized.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception as e:  # telemetry must never break a tick
        logger.debug("stale terminalization probe write failed: %s", e)


def stale_terminalized_count() -> int:
    """Probe-visible monotonic count of stale rows terminalized this process."""
    return _stale_terminalized_counter


def stale_terminalization_stats() -> dict:
    """Probe-visible snapshot of the per-tick stale-row terminalizer.

    Mirrors ``cron.scheduler.get_inflight_guard_stats`` for the DB-row class:
    a monotonic ``terminalized`` counter plus the most recent terminalization
    records read from the ``stale_terminalized.jsonl`` probe file (best-effort,
    content-free — opaque job_key, never raw pids or job ids).
    """
    recent: List[Dict[str, Any]] = []
    try:
        path = EXECUTIONS_FILE.parent / "stale_terminalized.jsonl"
        if path.exists():
            lines = path.read_text(encoding="utf-8").splitlines()
            for line in lines[-20:]:
                line = line.strip()
                if line:
                    recent.append(json.loads(line))
    except Exception as e:  # probe must never break a tick
        logger.debug("stale terminalization probe read failed: %s", e)
    return {
        "terminalized": _stale_terminalized_counter,
        "recent_terminalizations": recent,
    }


def terminalize_stale_executions(
    *, min_age_hours: float = _STALE_DIRECT_MIN_AGE_HOURS,
) -> int:
    """Terminalize running/claimed rows whose owner pid is provably dead.

    Runs on the ticker's per-tick path (NOT only at gateway restart) so a
    ``source=direct`` execution row created after the current process last
    restarted cannot wedge forever. Matches the fleet-wide zombie scan
    predicate: ``status='running' AND started_at > <min_age> AND finished_at IS
    NULL``. Uses ``_owner_is_live`` (pid liveness + PID-reuse start-time guard)
    so a row still owned by a live pid is never touched.

    Returns the number of rows terminalized to ``unknown``.
    """
    from datetime import timedelta

    now = _hermes_now()
    age_cutoff = (now - timedelta(hours=min_age_hours)).isoformat()
    terminalization_error = (
        "Execution owner process exited before a durable terminal state was "
        "written and was not reclaimed at the owner's gateway restart; "
        "side effects are unknown (stale source=direct/claimed sweep)."
    )
    changed = 0
    terminalized: List[Dict[str, Any]] = []
    with _transaction() as conn:
        rows = conn.execute(
            """SELECT id, process_id, pid, process_started_at,
                      job_id, source, status, started_at, finished_at
               FROM executions
               WHERE status IN ('claimed','running')
                 AND finished_at IS NULL AND started_at IS NOT NULL
                 AND datetime(started_at) < datetime(?)""",
            (age_cutoff,),
        ).fetchall()
        for row in rows:
            if row["process_id"] == _PROCESS_ID:
                continue
            if _owner_is_live(int(row["pid"]), row["process_started_at"]):
                continue
            cur = conn.execute(
                """UPDATE executions SET status='unknown', finished_at=?, error=?
                   WHERE id=? AND status IN ('claimed','running')
                     AND finished_at IS NULL""",
                (now.isoformat(), terminalization_error, row["id"]),
            )
            changed += cur.rowcount
            if cur.rowcount:
                record = _record(conn.execute(
                    "SELECT * FROM executions WHERE id=?", (row["id"],)
                ).fetchone())
                if record is not None:
                    terminalized.append(record)
        if changed:
            _prune_unlocked(conn)
    global _stale_terminalized_counter
    _stale_terminalized_counter += changed
    for record in terminalized:
        _record_stale_terminalization(record)
        logger.warning(
            "%s: terminalized stale execution row id=%s job_id=%s source=%s "
            "pid=%s (owner process dead; not reclaimed at restart)",
            _STALE_TERMINALIZED_LOG,
            record.get("id"),
            record.get("job_id"),
            record.get("source"),
            record.get("pid"),
        )
    return changed


def list_executions(
    *, job_id: Optional[str] = None, limit: int = 50,
    before_claimed_at: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return indexed, newest-first execution history with cursor pagination."""
    clauses: List[str] = []
    params: List[Any] = []
    if job_id is not None:
        clauses.append("job_id=?")
        params.append(str(job_id))
    if before_claimed_at is not None:
        clauses.append("claimed_at < ?")
        params.append(str(before_claimed_at))
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(max(1, min(int(limit), 500)))
    with _transaction() as conn:
        rows = conn.execute(
            "SELECT * FROM executions" + where
            + " ORDER BY claimed_at DESC, id DESC LIMIT ?",
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def latest_execution(job_id: str) -> Optional[Dict[str, Any]]:
    rows = list_executions(job_id=job_id, limit=1)
    return rows[0] if rows else None


def latest_executions(job_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """Load latest execution for many jobs in one indexed query."""
    clean = [str(job_id) for job_id in dict.fromkeys(job_ids) if job_id]
    if not clean:
        return {}
    placeholders = ",".join("?" for _ in clean)
    with _transaction() as conn:
        rows = conn.execute(
            f"""SELECT e.* FROM executions e
                WHERE e.job_id IN ({placeholders})
                  AND e.id=(SELECT e2.id FROM executions e2
                            WHERE e2.job_id=e.job_id
                            ORDER BY e2.claimed_at DESC, e2.id DESC LIMIT 1)""",
            clean,
        ).fetchall()
    return {row["job_id"]: dict(row) for row in rows}
