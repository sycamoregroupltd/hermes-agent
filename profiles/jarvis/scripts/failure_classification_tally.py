#!/usr/bin/env python3
"""Classify recent kanban failures and emit a Discord-ready tally.

No-agent cron script for the Jarvis profile.  It scans recent kanban events,
runs the read-only `hermes kanban classify-failure` CLI for each eligible task,
comments the resulting failure_class once per event, persists an idempotency
state file, and prints a tally message only when new classifications occurred.

Environment overrides (used by tests/fixtures):
  FAILURE_CLASSIFIER_BOARD           board slug (default: jarvis-os)
  FAILURE_CLASSIFIER_DB              absolute kanban.db path (overrides board)
  FAILURE_CLASSIFIER_STATE           state JSON path
  FAILURE_CLASSIFIER_LOG             JSONL audit log path
  FAILURE_CLASSIFIER_INTERVAL_SECONDS lookback window (default: 1800)
  FAILURE_CLASSIFIER_NOW             epoch seconds for deterministic tests
  FAILURE_CLASSIFIER_HERMES          hermes executable (default: /home/frank/.local/bin/hermes)
  FAILURE_CLASSIFIER_DRY_RUN         if 1, do not append comments/update state
"""
from __future__ import annotations

import collections
import json
import os
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

DEFAULT_BOARD = "jarvis-os"
DEFAULT_ROOT = Path("/home/frank/.hermes")
DEFAULT_PROFILE_HOME = DEFAULT_ROOT / "profiles" / "jarvis"
COMMENT_PREFIX = "failure_class:"
COMMENT_AUTHOR = "failure-classifier-cron"
ELIGIBLE_EVENT_KINDS = {
    "blocked",
    "crashed",
    "protocol_violation",
    "timed_out",
    "spawn_failed",
    "gave_up",
}
MAX_STATE_EVENTS = 10_000
MAX_LOG_BYTES = 5_000_000


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _board_db_path(board: str) -> Path:
    return DEFAULT_ROOT / "kanban" / "boards" / board / "kanban.db"


def _state_path() -> Path:
    return Path(
        os.environ.get(
            "FAILURE_CLASSIFIER_STATE",
            str(DEFAULT_PROFILE_HOME / "state" / "failure_classification_tally_state.json"),
        )
    )


def _log_path() -> Path:
    return Path(
        os.environ.get(
            "FAILURE_CLASSIFIER_LOG",
            str(DEFAULT_PROFILE_HOME / "logs" / "failure_classification_tally.jsonl"),
        )
    )


def _load_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"processed_event_ids": []}
    except Exception as exc:
        # Fail open but do not silently erase evidence; start a new state and log the parse fault.
        return {"processed_event_ids": [], "state_read_error": f"{type(exc).__name__}: {exc}"}
    if not isinstance(data, dict):
        return {"processed_event_ids": [], "state_read_error": "state root was not an object"}
    processed = data.get("processed_event_ids")
    if not isinstance(processed, list):
        data["processed_event_ids"] = []
    return data


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def _append_log(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.exists() and path.stat().st_size > MAX_LOG_BYTES:
            rotated = path.with_suffix(path.suffix + ".1")
            try:
                rotated.unlink()
            except FileNotFoundError:
                pass
            path.replace(rotated)
    except OSError:
        pass
    record = {"ts": int(time.time()), **record}
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _recent_failure_events(conn: sqlite3.Connection, since: int, until: int) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in ELIGIBLE_EVENT_KINDS)
    rows = conn.execute(
        f"""
        SELECT e.id AS event_id,
               e.task_id,
               e.run_id,
               e.kind,
               e.payload,
               e.created_at,
               t.status,
               t.title
          FROM task_events e
          JOIN tasks t ON t.id = e.task_id
         WHERE e.kind IN ({placeholders})
           AND e.created_at >= ?
           AND e.created_at <= ?
         ORDER BY e.created_at ASC, e.id ASC
        """,
        [*sorted(ELIGIBLE_EVENT_KINDS), since, until],
    ).fetchall()
    return [dict(row) for row in rows]


def _comments_for_event(conn: sqlite3.Connection, task_id: str, event_id: int) -> list[str]:
    marker = f"event_id={event_id}"
    rows = conn.execute(
        "SELECT body FROM task_comments WHERE task_id = ? AND body LIKE ? ORDER BY created_at ASC, id ASC",
        (task_id, f"%{marker}%"),
    ).fetchall()
    return [str(row["body"] or "") for row in rows]


def _run_command(argv: list[str], *, env: dict[str, str]) -> tuple[int, str, str]:
    proc = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=120)
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _hermes_env(db_path: Path, board: str) -> dict[str, str]:
    env = os.environ.copy()
    env["HERMES_KANBAN_DB"] = str(db_path)
    env["HERMES_KANBAN_BOARD"] = board
    env.setdefault("HERMES_HOME", str(DEFAULT_PROFILE_HOME))
    env.setdefault("HERMES_PROFILE", "jarvis")
    return env


def _classify_task(hermes: str, db_path: Path, board: str, task_id: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    argv = [hermes, "kanban", "classify-failure", "--task", task_id, "--json"]
    env = _hermes_env(db_path, board)
    rc, out, err = _run_command(argv, env=env)
    evidence = {
        "argv": " ".join(shlex.quote(part) for part in argv),
        "returncode": rc,
        "stdout_head": out[:1000],
        "stderr_head": err[:1000],
    }
    if rc != 0:
        return None, evidence
    try:
        payload = json.loads(out)
    except json.JSONDecodeError as exc:
        evidence["json_error"] = str(exc)
        return None, evidence
    return payload, evidence


def _comment_task(hermes: str, db_path: Path, board: str, task_id: str, body: str) -> dict[str, Any]:
    argv = [
        hermes,
        "kanban",
        "comment",
        task_id,
        body,
        "--author",
        COMMENT_AUTHOR,
    ]
    env = _hermes_env(db_path, board)
    rc, out, err = _run_command(argv, env=env)
    return {
        "argv": " ".join(shlex.quote(part) for part in argv[:4]) + " <body> --author " + COMMENT_AUTHOR,
        "returncode": rc,
        "stdout_head": out[:1000],
        "stderr_head": err[:1000],
    }


def _comment_body(event: dict[str, Any], classification: dict[str, Any]) -> str:
    evidence = classification.get("evidence_markers") or []
    evidence_text = "; ".join(str(item) for item in evidence[:3]) or "(none)"
    return (
        f"{COMMENT_PREFIX} {classification.get('failure_class', 'indeterminate')}\n"
        f"event_id={event['event_id']} event_kind={event['kind']} run_id={event.get('run_id')}\n"
        f"classifier_version={classification.get('classifier_version', 'unknown')} "
        f"confidence={classification.get('confidence', 'unknown')} read_only={classification.get('read_only')}\n"
        f"safe_recovery_hint={classification.get('safe_recovery_hint', '')}\n"
        f"evidence_markers={evidence_text}"
    )


def _discord_message(board: str, since: int, until: int, counts: collections.Counter[str], processed: list[dict[str, Any]], skipped: list[dict[str, Any]]) -> str:
    lines = [
        "Kanban failure-class tally",
        f"board={board} window={since}..{until} new_classifications={len(processed)} skipped={len(skipped)}",
        "counts:",
    ]
    for cls, count in sorted(counts.items()):
        lines.append(f"- {cls}: {count}")
    lines.append("classified_events:")
    for row in processed[:25]:
        lines.append(
            f"- {row['task_id']} event_id={row['event_id']} event_kind={row['event_kind']} failure_class={row['failure_class']}"
        )
    if len(processed) > 25:
        lines.append(f"- ... {len(processed) - 25} more")
    if skipped:
        lines.append("skips:")
        for row in skipped[:15]:
            lines.append(
                f"- {row.get('task_id', '?')} event_id={row.get('event_id', '?')} reason={row.get('reason', '?')}"
            )
        if len(skipped) > 15:
            lines.append(f"- ... {len(skipped) - 15} more skips")
    return "\n".join(lines)


def main() -> int:
    board = os.environ.get("FAILURE_CLASSIFIER_BOARD", DEFAULT_BOARD).strip() or DEFAULT_BOARD
    db_path = Path(os.environ.get("FAILURE_CLASSIFIER_DB") or _board_db_path(board))
    state_path = _state_path()
    log_path = _log_path()
    interval = _env_int("FAILURE_CLASSIFIER_INTERVAL_SECONDS", 30 * 60)
    now = _env_int("FAILURE_CLASSIFIER_NOW", int(time.time()))
    since = now - interval
    hermes = os.environ.get("FAILURE_CLASSIFIER_HERMES", "/home/frank/.local/bin/hermes")
    dry_run = os.environ.get("FAILURE_CLASSIFIER_DRY_RUN", "").strip() == "1"

    state = _load_state(state_path)
    processed_ids = {int(x) for x in state.get("processed_event_ids", []) if str(x).isdigit()}
    counts: collections.Counter[str] = collections.Counter()
    processed_rows: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    _append_log(log_path, {
        "action": "start",
        "board": board,
        "db_path": str(db_path),
        "state_path": str(state_path),
        "window_since": since,
        "window_until": now,
        "dry_run": dry_run,
        "state_read_error": state.get("state_read_error"),
    })

    try:
        with _connect(db_path) as conn:
            events = _recent_failure_events(conn, since, now)
            _append_log(log_path, {"action": "scan", "candidate_count": len(events)})
            for event in events:
                event_id = int(event["event_id"])
                task_id = str(event["task_id"])
                if event_id in processed_ids:
                    reason = "already_processed_state"
                    skipped.append({"task_id": task_id, "event_id": event_id, "reason": reason})
                    _append_log(log_path, {"action": "skip", "task_id": task_id, "event_id": event_id, "reason": reason})
                    continue
                existing_comments = _comments_for_event(conn, task_id, event_id)
                if any(COMMENT_PREFIX in body for body in existing_comments):
                    reason = "already_classified_comment"
                    processed_ids.add(event_id)
                    skipped.append({"task_id": task_id, "event_id": event_id, "reason": reason})
                    _append_log(log_path, {"action": "skip", "task_id": task_id, "event_id": event_id, "reason": reason})
                    continue

                classification, classify_evidence = _classify_task(hermes, db_path, board, task_id)
                _append_log(log_path, {
                    "action": "classify_command",
                    "task_id": task_id,
                    "event_id": event_id,
                    "evidence": classify_evidence,
                })
                if classification is None:
                    skipped.append({"task_id": task_id, "event_id": event_id, "reason": "classify_command_failed"})
                    continue

                failure_class = str(classification.get("failure_class") or "indeterminate")
                body = _comment_body(event, classification)
                if dry_run:
                    comment_evidence = {"dry_run": True, "returncode": 0}
                else:
                    comment_evidence = _comment_task(hermes, db_path, board, task_id, body)
                _append_log(log_path, {
                    "action": "comment_command",
                    "task_id": task_id,
                    "event_id": event_id,
                    "failure_class": failure_class,
                    "evidence": comment_evidence,
                })
                if int(comment_evidence.get("returncode", 1)) != 0:
                    skipped.append({"task_id": task_id, "event_id": event_id, "reason": "comment_command_failed"})
                    continue

                processed_ids.add(event_id)
                counts[failure_class] += 1
                processed_rows.append({
                    "task_id": task_id,
                    "event_id": event_id,
                    "event_kind": event["kind"],
                    "failure_class": failure_class,
                })
    except Exception as exc:
        _append_log(log_path, {"action": "error", "error": f"{type(exc).__name__}: {exc}"})
        print(f"failure-classifier cron failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if not dry_run:
        state["processed_event_ids"] = sorted(processed_ids)[-MAX_STATE_EVENTS:]
        state["last_run_at"] = now
        state["last_window_since"] = since
        state["last_window_until"] = now
        state["last_new_classifications"] = len(processed_rows)
        _atomic_write_json(state_path, state)
        _append_log(log_path, {"action": "state_write", "processed_event_ids": len(state["processed_event_ids"])})

    if not processed_rows:
        _append_log(log_path, {"action": "delivery_suppressed", "reason": "no_new_classifications", "skipped": skipped})
        return 0

    message = _discord_message(board, since, now, counts, processed_rows, skipped)
    _append_log(log_path, {"action": "discord_delivery_payload", "deliver_target": "cron job deliver field", "message": message})
    print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
