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
  - Subsequent runs      -> anomaly persists -> update only the persisted ledger
                            count/last-seen/fingerprint; do not create a card or
                            comment for a repeat observation.
  - Resolution (green)  -> comment "RESOLVED: Report is green as of <ts>",
                            auto-transition the task to 'completed' when its
                            status is safely closable ('ready', 'todo', or
                            'blocked'), clear the ledger entry, and send recovery
                            alert to the originating channel. An in-progress card
                            keeps its ledger pointer for a later retry.

All side-effects (kanban create/comment/complete, discord send) are injected
via callbacks so tests run fully mocked. In production the wrappers pass the
real `hermes kanban` / `hermes send` shells (see make_harness()).
"""
from __future__ import annotations

import hashlib
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
# Defect-c (t_36d0acad): a green ACRADR condition must CLOSE the prior card even
# when it drifted to 'blocked' (dispatcher crash / auto-block) — a blocked card is
# not being actively worked, so closing it on green is the whole point of the fix.
# 'in_progress' is deliberately excluded: a human may genuinely own that card.
CLOSABLE_ON_GREEN = ("ready", "todo", "blocked")
# Source guards are deliberately conservative: an old report is not evidence of
# a current anomaly, and a revision already admitted after restart is not new.
DEFAULT_SOURCE_MAX_AGE_HOURS = 48.0


def _source_identity(source: str) -> str:
    """Return the stable report identity used by dedupe/high-water state.

    Dated cron reports live below ``<job-id>/``; direct append-only reports such
    as health_canary.jsonl do not.  Never use the dated filename in a key.
    """
    p = Path(source)
    if p.suffix == ".jsonl" or p.parent.name in ("", ".", os.sep, "output"):
        return p.name
    if re.fullmatch(r"[0-9a-f]{8,}", p.parent.name, re.IGNORECASE):
        return p.parent.name
    return p.name


def _legacy_entry_key(report_class: str, source: str) -> str:
    return f"{report_class}::{_source_identity(source)}"


def source_revision(source: str) -> dict:
    """Return a restart-safe filesystem revision for a report source."""
    p = Path(source)
    stat = p.stat()
    digest = hashlib.sha256(p.read_bytes()).hexdigest()
    return {"mtime_ns": stat.st_mtime_ns, "size": stat.st_size, "sha256": digest}


def _source_max_age_hours(value: Optional[float] = None) -> float:
    if value is not None:
        return float(value)
    try:
        configured = float(os.environ.get("ACRADR_SOURCE_MAX_AGE_HOURS", DEFAULT_SOURCE_MAX_AGE_HOURS))
        return configured if configured >= 0 else DEFAULT_SOURCE_MAX_AGE_HOURS
    except (TypeError, ValueError):
        return DEFAULT_SOURCE_MAX_AGE_HOURS


def admit_source(
    ledger: dict,
    source: str,
    *,
    now: Optional[datetime] = None,
    max_age_hours: Optional[float] = None,
    admitted_this_run: Optional[dict] = None,
) -> dict:
    """Apply the persisted stale-source/high-water guard.

    The high-water mark is keyed by stable source identity and ordered by
    ``mtime_ns``.  A run-local map permits multiple rules from one file while
    still suppressing that exact revision after a process restart.  Every
    suppression is retained as a bounded per-source audit record.
    """
    now = now or utc_now()
    identity = _source_identity(source)
    guards = ledger.setdefault("source_guards", {})
    high_water = ledger.setdefault("source_high_water", {})
    audit = guards.setdefault(identity, {})
    try:
        revision = source_revision(source)
    except (OSError, ValueError) as exc:
        reason = "source_unreadable"
        audit.update({"last_checked": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                      "last_reason": reason, "suppression_count": int(audit.get("suppression_count", 0)) + 1,
                      "detail": type(exc).__name__})
        return {"accepted": False, "identity": identity, "reason": reason,
                "source": source}

    age_hours = max(0.0, (now.timestamp() - revision["mtime_ns"] / 1_000_000_000) / 3600.0)
    threshold = _source_max_age_hours(max_age_hours)
    if age_hours > threshold:
        reason = "stale_source"
        audit.update({"last_checked": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                      "last_reason": reason, "last_age_hours": round(age_hours, 3),
                      "threshold_hours": threshold,
                      "last_mtime_ns": revision["mtime_ns"],
                      "suppression_count": int(audit.get("suppression_count", 0)) + 1})
        return {"accepted": False, "identity": identity, "reason": reason,
                "source": source, "age_hours": age_hours, "threshold_hours": threshold}

    previous = high_water.get(identity)
    current_mtime = revision["mtime_ns"]
    run_revision = (admitted_this_run or {}).get(identity)
    if run_revision == current_mtime:
        return {"accepted": True, "identity": identity, "reason": "already_admitted_this_run",
                "revision": revision}
    if previous and current_mtime <= int(previous.get("mtime_ns", -1)):
        reason = "high_water_mark"
        audit.update({"last_checked": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                      "last_reason": reason, "last_mtime_ns": current_mtime,
                      "high_water_mtime_ns": previous.get("mtime_ns"),
                      "suppression_count": int(audit.get("suppression_count", 0)) + 1})
        return {"accepted": False, "identity": identity, "reason": reason,
                "source": source, "revision": revision}

    high_water[identity] = revision
    if admitted_this_run is not None:
        admitted_this_run[identity] = current_mtime
    audit.update({"last_checked": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                  "last_reason": "accepted", "last_mtime_ns": current_mtime,
                  "high_water_mtime_ns": current_mtime})
    return {"accepted": True, "identity": identity, "reason": "new_revision",
            "revision": revision}


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
        return {"version": 2, "entries": {}, "source_high_water": {}, "source_guards": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "entries" not in data:
            data = {"version": 2, "entries": data.get("entries", {})}
        data.setdefault("entries", {})
        data["version"] = 2
        data.setdefault("source_high_water", {})
        data.setdefault("source_guards", {})
        return data
    except Exception:
        # Do not crash the cron loop on a corrupt ledger; start clean but keep
        # the corrupt file renamed for forensics.
        try:
            path.rename(path.with_suffix(path.suffix + ".corrupt"))
        except Exception:
            pass
        return {"version": 2, "entries": {}, "source_high_water": {}, "source_guards": {}}


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

        Used by resolve_anomaly's closable-status guard so we never
        auto-complete a card a human already picked up. Ready, todo, and blocked
        cards are safely closable; in_progress is deliberately retained. Mirrors
        blocked_task_notifier.py's sqlite approach (no agent loop, no LLM).
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

    def list_open_acradr(self, board: Optional[str] = None) -> list:
        """List open (ready/todo/blocked) '[ACRADR]' cards from the board sqlite DB.

        Read-only reconciliation for defect-c (t_36d0acad): finds orphaned ACRADR
        cards the ledger has lost track of. 'in_progress' cards are deliberately
        excluded so we never auto-close a card a human is actively working.
        """
        board = board or self.board
        import sqlite3
        db = Path(os.environ.get("BOARDS_DIR", "/home/frank/.hermes/kanban/boards")) / board / "kanban.db"
        if not db.exists():
            return []
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            rows = con.execute(
                "SELECT id, title, body, status FROM tasks "
                "WHERE title LIKE '[ACRADR]%' AND status IN ('ready','todo','blocked')"
            ).fetchall()
            con.close()
            return [{"task_id": r["id"], "title": r["title"], "body": r["body"] or "",
                     "status": r["status"]} for r in rows]
        except Exception:
            return []


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
    """Return the stable source identity (never the dated report filename)."""
    return _source_identity(source)


def _entry_key(report_class: str, source: str, rule_id: Optional[str] = None) -> str:
    """Build the stable anomaly key.

    Production keys are ``report_class::rule_id::source_identity``.  The
    optional rule preserves the legacy two-argument API for old callers; the
    runner always supplies it so distinct rules in one report remain distinct.
    """
    identity = _source_identity(source)
    if rule_id:
        return f"{report_class}::{rule_id}::{identity}"
    return f"{report_class}::{identity}"


# Defect-c (t_36d0acad) orphan reconciliation. The ledger may lose a card when
# resolve_anomaly could not complete it (it used to clear the entry anyway), so a
# green condition's last card was left open forever. These helpers re-locate open
# '[ACRADR]' cards and reconstruct their key from title + body.
_ACRADR_TITLE_RE = re.compile(r"^\[ACRADR\]\s+\S+\s+([A-Za-z0-9_]+):\s*(\S+)")
_ACRADR_SOURCE_RE = re.compile(r"Source:\s*`([^`]+)`")


def _acradr_key_from_card(card: dict) -> Optional[str]:
    """Reconstruct the current stable key from an ACRADR card."""
    title = card.get("title") or ""
    body = card.get("body") or ""
    mt = _ACRADR_TITLE_RE.match(title)
    if not mt:
        return None
    report_class, rule_id = mt.groups()
    ms = _ACRADR_SOURCE_RE.search(body)
    if not ms:
        return None
    return _entry_key(report_class, ms.group(1), rule_id)

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
    rule_id: Optional[str] = None,
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
      - first time for this rule/source identity: create one ticket and store it.
      - repeats: update only last_seen, last_fingerprint, and occurrences.
    """
    now = now or iso_ts()
    key = _entry_key(report_class, source, rule_id)
    entries: dict = ledger.setdefault("entries", {})

    create_impl = _create_fn or (harness.create_ticket if harness else None)
    if create_impl is None:
        raise RuntimeError("record_anomaly requires a harness or explicit _create_fn callback")

    # Migrate a matching pre-rule ledger entry without discarding its ticket or
    # counters.  An old entry with no provable rule is retained as-is; creating a
    # distinct rule key is safer than guessing which anomaly it represented.
    if rule_id and key not in entries:
        legacy_key = _legacy_entry_key(report_class, source)
        legacy = entries.get(legacy_key)
        legacy_rule = str(legacy.get("rule_id", "")) if legacy else ""
        if not legacy_rule and legacy:
            legacy_rule = str(legacy.get("first_fingerprint", "")).split(":", 1)[0]
        if legacy and legacy_rule == rule_id:
            entries[key] = entries.pop(legacy_key)
            entries[key]["rule_id"] = rule_id
            entries[key]["source_identity"] = _source_identity(source)

    if key in entries:
        entry = entries[key]
        # Repeated observations are ledger-only.  The original ticket is the
        # durable finding; never mint a comment/card for a repeat.
        entry["last_seen"] = now
        entry["occurrences"] = int(entry.get("occurrences", 1)) + 1
        entry["last_fingerprint"] = fingerprint
        return {"action": "deduped", "task_id": entry["task_id"], "key": key,
                "occurrences": entry["occurrences"], "commented": False}

    # First detection.
    tid = create_impl(
        title=title, body=body, assignee=assignee, priority=priority, board=board
    )
    entries[key] = {
        "task_id": tid,
        "report_class": report_class,
        "rule_id": rule_id,
        "source": source,
        "source_identity": _source_identity(source),
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
    return {"action": "created", "task_id": tid, "key": key, "occurrences": 1,
            "commented": False}


def resolve_anomaly(
    ledger: dict,
    *,
    report_class: str,
    source: str,
    rule_id: Optional[str] = None,
    channel: Optional[str] = None,
    board: str = "jarvis-os",
    now: Optional[str] = None,
    _comment_fn: Optional[Callable[[str, str, str], None]] = None,
    _alert_fn: Optional[Callable[[str, str], bool]] = None,
    _status_fn: Optional[Callable[[str, str], str]] = None,
    harness: Any = None,
) -> dict:
    """Self-heal: report went green again for this (class, rule, source).

    Steps (from task body Feature 3):
      1. comment "RESOLVED: Report is green as of <ts>" once per entry;
      2. transition task -> completed only when status is ready, todo, or
         blocked (never stomp an in_progress human-owned card);
      3. clear the ledger entry after successful completion, otherwise retain
         its pointer as resolved_pending for a future retry;
      4. send recovery alert to the originating channel.

    Returns {"action": "resolved"|"no_entry", "task_id": ..., "cleared": bool}.
    If there is no open entry for this key, returns {"action": "no_entry"}.
    """
    now = now or iso_ts()
    key = _entry_key(report_class, source, rule_id)
    entries: dict = ledger.setdefault("entries", {})
    if key not in entries and rule_id:
        # Keep compatibility with a pre-rule ledger written by the old runner.
        legacy_key = _legacy_entry_key(report_class, source)
        if legacy_key in entries:
            key = legacy_key
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

    # 2. auto-complete if the task is not being actively worked. CLOSABLE_ON_GREEN
    #    includes 'blocked' (defect-c t_36d0acad): a green condition must close a
    #    stale blocked card — that is the whole point of the fix. 'in_progress' is
    #    excluded so we never stomp a card a human is genuinely working.
    completed = False
    status = None
    if status_impl is not None:
        try:
            status = status_impl(task_id, board)
        except Exception:
            status = None
    if status is None or status in CLOSABLE_ON_GREEN:
        # comment only on the first attempt (resolved_pending set on a prior skip)
        if not entry.get("resolved_pending"):
            comment_impl(task_id, RESOLVED_COMMENT_PREFIX + now)
        complete_impl(
            task_id,
            f"ACRADR self-heal: {report_class} on {source} returned green @ {now}.",
        )
        completed = True
        # 3. clear ledger entry
        del entries[key]
        cleared = True
    else:
        # Cannot safely auto-complete (e.g. in_progress). KEEP the pointer instead
        # of orphaning the card (defect-c t_36d0acad): a later green run retries.
        if not entry.get("resolved_pending"):
            comment_impl(
                task_id,
                f"[ACRADR] Report green @ {now} but task in '{status}'; not auto-closed "
                "(a worker may own it). Pointer kept — retried on a future green run.",
            )
        entry["resolved_pending"] = True
        entry["last_seen"] = now
        cleared = False
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


def reconcile_orphan_acradr(
    current_keys: set,
    *,
    board: str = "jarvis-os",
    now: Optional[str] = None,
    harness: Any = None,
    _list_open_fn: Optional[Callable[[str], list]] = None,
    _complete_fn: Optional[Callable[[str, str, Optional[str]], None]] = None,
    _comment_fn: Optional[Callable[[str, str, Optional[str]], None]] = None,
) -> list:
    """Close ACRADR cards whose condition is green but that the ledger has lost.

    Defect-c (t_36d0acad): resolve_anomaly used to clear its ledger entry even
    when it could not complete a card (e.g. status=blocked), orphaning that card
    forever — a green condition left its last card open for days. Reconciliation
    re-locates open '[ACRADR]' cards on the board, reconstructs each card's key,
    and completes any whose key is NOT in the current anomaly set (i.e. green this
    run). Only ready/todo/blocked cards are listed (never in_progress), so active
    human work is never stomped. Pure no-op when the harness cannot list cards.

    Returns a list of closed card dicts: {"task_id", "key", "status"}.
    """
    now = now or iso_ts()
    list_impl = _list_open_fn or (getattr(harness, "list_open_acradr", None)
                                  if harness else None)
    complete_impl = _complete_fn or (harness.complete if harness else None)
    comment_impl = _comment_fn or (harness.comment if harness else None)
    if list_impl is None or complete_impl is None:
        return []  # cannot reconcile without list/complete capability — safe no-op
    closed = []
    for card in list_impl(board):
        if (card.get("status") or "") == "in_progress":
            continue  # defense-in-depth: never auto-close a card a human is working
        key = _acradr_key_from_card(card)
        if key is None:
            continue
        if key in current_keys:
            continue  # still anomalous this run — leave open
        complete_impl(
            card["task_id"],
            f"ACRADR reconcile: {key} green @ {now}; closing stale prior card "
            f"(defect-c t_36d0acad).",
            board,
        )
        if comment_impl is not None:
            comment_impl(card["task_id"], RESOLVED_COMMENT_PREFIX + now, board)
        closed.append({"task_id": card["task_id"], "key": key,
                       "status": card.get("status")})
    return closed


def count_active(ledger: dict) -> int:
    return len(ledger.get("entries", {}))


def list_active(ledger: dict) -> list[dict]:
    return [dict(v) for v in ledger.get("entries", {}).values()]
