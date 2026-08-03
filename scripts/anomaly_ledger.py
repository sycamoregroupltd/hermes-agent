#!/usr/bin/env python3
"""
anomaly_ledger.py -- Anomaly State Ledger & auto-resolution engine.

Part of Enhanced ACRADR (t_03e2fea5 / jarvis-os). Task t_cd7e8188
(State Ledger & Auto-Resolution, integration-builder).

This module is a *library* -- it holds no CLI/main and performs no I/O of its
own beyond calling small injectable helpers. The core detector (t_82023960) and
the cron wrapper (t_00a856e5) import it and drive it. That makes the three
flows (first-detection, dedupe, self-heal) unit-testable without touching the
live kanban board or Discord.

State Ledger location (single source of truth):
    /home/frank/.hermes/profiles/jarvis/cron/state/detected_anomalies.json

Design invariant (from SOUL.md / task body):
  - First detection      -> create high-priority ticket on the correct board in
                            status 'ready' (via kanban_create) + record ticket id.
  - Subsequent runs      -> anomaly persists -> append fresh output as a comment
                            on the EXISTING ticket (dedupe, no new card).
  - Resolution (green)  -> comment "RESOLVED: Report is green as of <ts>.",
                            auto-transition the task to 'completed' if still in
                            'ready'/'todo', clear ledger entry, send recovery
                            alert to the originating channel.

All side-effects (kanban create/comment/complete, discord send) are injected
via callbacks so tests run fully mocked. In production the wrappers pass the
real `hermes kanban` / `hermes send` shells (see make_harness()).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

# ----------------------------------------------------------------------------
# Paths / constants
# ----------------------------------------------------------------------------
DEFAULT_LEDGER = Path(
    os.environ.get(
        "ANOMALY_LEDGER_PATH",
        "/home/frank/.hermes/profiles/jarvis/cron/state/detected_anomalies.json",
    )
)
HERMES_BIN = os.environ.get("HERMES_BIN", "/home/frank/.local/bin/hermes")
RESOLVED_COMMENT_PREFIX = "RESOLVED: Report is green as of "
ACTIVE_STATUSES = ("ready", "todo")  # statuses eligible for auto-completion


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_ts() -> str:
    return utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")


# ----------------------------------------------------------------------------
# Ledger load / save  (atomic; never loses the file on a bad write)
# ----------------------------------------------------------------------------
def load_ledger(path: Path = DEFAULT_LEDGER) -> dict:
    """Return the ledger dict. Missing/corrupt file -> empty ledger."""
    if not path.exists():
        return {"version": 1, "entries": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "entries" not in data:
            data = {"version": 1, "entries": data.get("entries", {})}
        data.setdefault("entries", {})
        data.setdefault("version", 1)
        return data
    except Exception:
        # Do not crash the cron loop on a corrupt ledger; start clean but keep
        # the corrupt file renamed for forensics.
        try:
            path.rename(path.with_suffix(path.suffix + ".corrupt"))
        except Exception:
            pass
        return {"version": 1, "entries": {}}


def save_ledger(ledger: dict, path: Path = DEFAULT_LEDGER) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    import tempfile
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(ledger, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


# ----------------------------------------------------------------------------
# Side-effect harness  (injectable; real impl shells `hermes`)
# ----------------------------------------------------------------------------
class KanbanHarness:
    """Thin wrapper around `hermes kanban` + `hermes send`.

    Methods used by the ledger engine:
        create_ticket(title, body, assignee, priority, board) -> task_id
        comment(task_id, body, board) -> None
        complete(task_id, summary, board) -> None
        send_alert(target, message) -> None
    """

    def __init__(self, board: str = "jarvis-os", hermes_bin: str = HERMES_BIN):
        self.board = board
        self.hermes_bin = hermes_bin

    # -- low level -----------------------------------------------------------
    def _kanban(self, *args: str) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env["HERMES_HOME"] = "/home/frank/.hermes"
        cmd = [self.hermes_bin, "kanban"]
        if self.board:
            cmd += ["--board", self.board]
        cmd += list(args)
        return subprocess.run(cmd, capture_output=True, text=True, timeout=180, env=env)

    # -- high level ----------------------------------------------------------
    def create_ticket(self, title: str, body: str, assignee: str,
                      priority: int = 5, board: Optional[str] = None) -> str:
        board = board or self.board
        env = os.environ.copy()
        env["HERMES_HOME"] = "/home/frank/.hermes"
        cmd = [
            self.hermes_bin, "kanban", "--board", board, "create",
            "--assignee", assignee,
            "--priority", str(priority),
            "--json",
            "--body", body,
            title,
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=180, env=env)
        if res.returncode != 0:
            raise RuntimeError(
                f"kanban create failed (rc={res.returncode}): "
                f"{(res.stdout + res.stderr).strip()[:400]}"
            )
        # Output is JSON: {"id": "t_xxxx"} (or a list). Robust-parse it.
        task_id = _extract_task_id(res.stdout)
        if not task_id:
            raise RuntimeError(
                f"kanban create returned no id: {(res.stdout + res.stderr).strip()[:400]}"
            )
        return task_id

    def comment(self, task_id: str, body: str, board: Optional[str] = None) -> None:
        res = self._kanban("comment", task_id, body)
        if res.returncode != 0:
            raise RuntimeError(
                f"kanban comment failed (rc={res.returncode}) on {task_id}: "
                f"{(res.stdout + res.stderr).strip()[:400]}"
            )

    def complete(self, task_id: str, summary: str, board: Optional[str] = None) -> None:
        res = self._kanban("complete", "--summary", summary, task_id)
        if res.returncode != 0:
            raise RuntimeError(
                f"kanban complete failed (rc={res.returncode}) on {task_id}: "
                f"{(res.stdout + res.stderr).strip()[:400]}"
            )

    def send_alert(self, target: str, message: str) -> bool:
        env = os.environ.copy()
        env["HERMES_HOME"] = "/home/frank/.hermes"
        res = subprocess.run(
            [self.hermes_bin, "send", "-q", "-t", target, "-s", "ACRADR recovery", message],
            capture_output=True, text=True, timeout=120, env=env,
        )
        return res.returncode == 0

    def status(self, task_id: str, board: Optional[str] = None) -> Optional[str]:
        """Read a task's current status directly from the board sqlite DB.

        Used by resolve_anomaly's ready/todo guard so we never auto-complete a
        card a human already picked up. Mirrors blocked_task_notifier.py's
        sqlite approach (no agent loop, no LLM).
        """
        board = board or self.board
        import sqlite3
        db = Path(os.environ.get("BOARDS_DIR", "/home/frank/.hermes/kanban/boards")) / board / "kanban.db"
        if not db.exists():
            return None
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
            con.row_factory = sqlite3.Row
            row = con.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
            con.close()
            return row["status"] if row else None
        except Exception:
            return None


def _extract_task_id(stdout: str) -> Optional[str]:
    """Parse a task id out of `hermes kanban create --json` output."""
    stdout = (stdout or "").strip()
    if not stdout:
        return None
    try:
        data = json.loads(stdout)
        if isinstance(data, dict):
            return data.get("id") or data.get("task_id")
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return data[0].get("id") or data[0].get("task_id")
    except Exception:
        pass
    m = re.search(r"\b(t_[0-9a-f]{8,})\b", stdout)
    return m.group(1) if m else None


def make_harness(board: str = "jarvis-os", hermes_bin: str = HERMES_BIN) -> KanbanHarness:
    return KanbanHarness(board=board, hermes_bin=hermes_bin)


# ----------------------------------------------------------------------------
# Engine
# ----------------------------------------------------------------------------
def _cron_job_id(source: str) -> str:
    """Derive a STABLE cron-job identity from a report source path.

    The fleet cron layer writes one dated report file per run inside a
    per-job output directory:

        <scan_root>/<cron_job_id>/<YYYY-MM-DD_HH-MM-SS>.md

    Keying the dedupe ledger on the per-run *filename* spawned a fresh
    kanban card on every run (card churn), violating fleet "link, don't
    clone" governance (t_0596724e). The stable identity is the cron job
    id -- the immediate parent directory of the report file.

    Reports that live directly in the scan root (e.g. the append-only
    health_canary.jsonl) or synthetic test paths that carry no per-job
    directory fall back to the file basename so they keep a stable,
    unique key.
    """
    p = Path(source)
    parent = p.parent
    # Only promote the parent dir to a job id when it is a real directory
    # segment (not the filesystem root, cwd, or empty).
    if parent.name and parent.name not in (".", os.sep):
        return parent.name
    return p.name


def _entry_key(report_class: str, source: str) -> str:
    """Stable dedupe key: one open ticket per (report_class, cron_job_id).

    Keying on the per-run timestamped report file caused ACRADR to spawn
    a new card on every calibration run (t_0596724e). We key on the
    stable cron-job identity instead so all runs of one cron job dedupe
    onto a single owned incident (occurrences accumulate as comments).
    """
    return f"{report_class}::{_cron_job_id(source)}"


def record_anomaly(
    ledger: dict,
    *,
    report_class: str,
    source: str,
    fingerprint: str,
    title: str,
    body: str,
    assignee: str,
    channel: str,
    ticket_id: Optional[str] = None,
    priority: int = 5,
    board: str = "jarvis-os",
    harness: Optional[KanbanHarness] = None,
    now: Optional[str] = None,
    _comment_fn: Optional[Callable[[str, str, str], None]] = None,
    _create_fn: Optional[Callable[..., str]] = None,
) -> dict:
    """Record / dedupe an active anomaly.

    Returns a result dict describing what happened:
        {"action": "created"|"deduped"|"already_open", "task_id": str, ...}

    Side effects (real or mocked):
      - first time for this (class, source): create ticket, store in ledger.
      - persists: append comment with fresh body, update last_seen + occurrence.
    """
    now = now or iso_ts()
    key = _entry_key(report_class, source)
    entries: dict = ledger.setdefault("entries", {})

    create_impl = _create_fn or (harness.create_ticket if harness else None)
    comment_impl = _comment_fn or (harness.comment if harness else None)
    if create_impl is None or comment_impl is None:
        raise RuntimeError("record_anomaly requires a harness or explicit *_fn callbacks")

    if key in entries:
        entry = entries[key]
        # Anomaly still active -> dedupe: comment fresh output on existing ticket.
        entry["last_seen"] = now
        entry["occurrences"] = int(entry.get("occurrences", 1)) + 1
        entry["last_fingerprint"] = fingerprint
        comment_impl(
            entry["task_id"],
            f"[ACRADR occurrence #{entry['occurrences']} @ {now}]\n{body}",
            board,
        )
        return {"action": "deduped", "task_id": entry["task_id"], "key": key,
                "occurrences": entry["occurrences"]}

    # First detection.
    tid = create_impl(
        title=title, body=body, assignee=assignee, priority=priority, board=board
    )
    entries[key] = {
        "task_id": tid,
        "report_class": report_class,
        "source": source,
        "channel": channel,
        "assignee": assignee,
        "priority": priority,
        "board": board,
        "first_seen": now,
        "last_seen": now,
        "occurrences": 1,
        "first_fingerprint": fingerprint,
        "last_fingerprint": fingerprint,
    }
    return {"action": "created", "task_id": tid, "key": key, "occurrences": 1}


def resolve_anomaly(
    ledger: dict,
    *,
    report_class: str,
    source: str,
    channel: Optional[str] = None,
    board: str = "jarvis-os",
    now: Optional[str] = None,
    _comment_fn: Optional[Callable[[str, str, str], None]] = None,
    _alert_fn: Optional[Callable[[str, str], bool]] = None,
    _status_fn: Optional[Callable[[str, str], str]] = None,
    harness: Any = None,
) -> dict:
    """Self-heal: report went green again for this (class, source).

    Steps (from task body Feature 3):
      1. comment  "RESOLVED: Report is green as of <ts>."
      2. transition task -> completed if still in ready/todo.
      3. clear ledger entry.
      4. send recovery alert to the originating channel.

    Returns {"action": "resolved"|"no_entry", "task_id": ..., "cleared": bool}.
    If there is no open entry for this key, returns {"action": "no_entry"}.
    """
    now = now or iso_ts()
    key = _entry_key(report_class, source)
    entries: dict = ledger.setdefault("entries", {})
    if key not in entries:
        return {"action": "no_entry", "key": key}

    entry = entries[key]
    task_id = entry["task_id"]
    board = entry.get("board", board)
    channel = channel or entry.get("channel")

    comment_impl = _comment_fn or (harness.comment if harness else None)
    complete_impl = (harness.complete if harness else None)
    alert_impl = _alert_fn or (harness.send_alert if harness else None)
    status_impl = _status_fn or (harness.status if harness else None)
    if comment_impl is None or complete_impl is None:
        raise RuntimeError("resolve_anomaly requires a harness or explicit *_fn callbacks")

    # 1. resolution comment
    comment_impl(task_id, RESOLVED_COMMENT_PREFIX + now)
    # 2. auto-complete ONLY if the task is still in an active (ready/todo) state.
    #    If a human already picked it up (in_progress/blocked), leave it open and
    #    note that we did not auto-close.
    completed = False
    status = None
    if status_impl is not None:
        try:
            status = status_impl(task_id, board)
        except Exception:
            status = None
    if status is None or status in ACTIVE_STATUSES:
        complete_impl(
            task_id,
            f"ACRADR self-heal: {report_class} on {source} returned green @ {now}.",
        )
        completed = True
    else:
        comment_impl(
            task_id,
            f"[ACRADR] Report green @ {now} but task already in '{status}'; leaving open for owner.",
        )
    # 3. clear ledger entry
    del entries[key]
    cleared = True
    # 4. recovery alert to originating channel
    alert_ok = None
    if channel and alert_impl is not None:
        alert_ok = alert_impl(
            channel,
            f"✅ ACRADR recovered: {report_class} on {source} is green again @ {now} "
            f"(ticket {task_id} auto-completed).",
        )
    return {"action": "resolved", "task_id": task_id, "key": key,
            "cleared": cleared, "alert_sent": alert_ok}


def count_active(ledger: dict) -> int:
    return len(ledger.get("entries", {}))


def list_active(ledger: dict) -> list[dict]:
    return [dict(v) for v in ledger.get("entries", {}).values()]
