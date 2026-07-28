#!/usr/bin/env python3
"""Read-only worker-visibility preflight for fleet governor anomaly scans.

This script is intentionally side-effect-free with respect to Hermes runtime and
kanban boards: it probes dashboard/CLI/SQLite state, writes only its own JSON
evidence artifact, and emits a compact phone-readable board summary.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(os.environ.get("HERMES_ROOT", "/home/frank/.hermes"))
BOARDS_DIR = Path(os.environ.get("KANBAN_BOARDS_DIR", str(ROOT / "kanban" / "boards")))
HERMES = os.environ.get("HERMES", "/home/frank/.local/bin/hermes")
OUT_JSON = Path(os.environ.get("WORKER_VISIBILITY_PREFLIGHT_JSON", "/home/frank/uaa-rules/worker-visibility-preflight.json"))
DEFAULT_STALE_SECONDS = int(os.environ.get("KANBAN_STALE_SECONDS", str(4 * 3600)))
FAILURE_LIMIT = int(os.environ.get("KANBAN_FAILURE_LIMIT", "2"))

DEDUP_LANES = {
    "jarvis-os": ["t_b0725b13", "t_5bb177d5"],
    "sycode-trading": ["t_d5ca49b5"],
    "upero": ["t_0c9e2570"],
    "orchestrator-sync": ["t_ed3dc6ad", "t_060b754a"],
}

PREFIX = {
    "HEALTHY_IDLE": "OK idle",
    "DASHBOARD_UNAVAILABLE_DEGRADED": "DEGRADED evidence",
    "PROVIDER_AUTH_PRE_REASONING": "AUTH lane",
    "RESPAWN_GUARD_OR_CRASHLOOP": "CRASHLOOP",
    "STALLED_RUN": "STALLED",
    "REVIEW_OR_APPROVAL_GATE": "REVIEW gate",
}

AUTH_RE = re.compile(
    r"\b(oauth|api[-_ ]?key|quota|429|401|403|no[-_ ]?provider|provider auth|auth(?:entication)? failed|credit|spend(?:ing)? limit|rate limit|unauthorized|forbidden)\b",
    re.I,
)
CRASH_RE = re.compile(r"\b(spawn_failed|pid[^\n]{0,40}not alive|failure[- ]limit|consecutive_failures|crash(?:ed|loop)?|timed out|timeout)\b", re.I)
REVIEW_RE = re.compile(r"\b(review[- ]required|needs[- ]approval|critical[- ]list|human approval|human gate|guardian review|REVIEW_VERDICT|approved by reviewer)\b", re.I)
HEARTBEAT_RE = re.compile(r"heartbeat", re.I)
ZERO_KEYS = ("reclaimed", "crashed", "timed_out", "stale", "auto_blocked", "promoted", "spawned")
LIVE_TRANSCRIPT_PATH_RE = re.compile(r"/[^\s'\"`]*cache/delegation/live/deleg_[A-Za-z0-9_-]+/task-\d+\.log")
DELEGATION_ID_RE = re.compile(r"\bdeleg_[A-Za-z0-9_-]+\b")


@dataclass
class CommandResult:
    rc: int | None
    stdout: str = ""
    stderr: str = ""
    error: str | None = None


@dataclass
class TaskSample:
    id: str
    title: str = ""
    status: str = ""
    assignee: str = ""
    created_at: int | None = None
    started_at: int | None = None
    consecutive_failures: int = 0
    last_failure_error: str = ""
    latest_text: str = ""
    latest_run_id: int | None = None
    latest_run_started_at: int | None = None
    latest_heartbeat_at: int | None = None


@dataclass
class BoardEvidence:
    board: str
    source_used: str = "none"
    fallback_reason: str | None = None
    gateway_running: bool | None = None
    dispatch_dry_run: dict[str, int] = field(default_factory=dict)
    status_counts: dict[str, int] = field(default_factory=dict)
    active_worker_ids: list[str] = field(default_factory=list)
    dashboard_run_ids: list[int] = field(default_factory=list)
    dashboard_inspect_run_ids: list[int] = field(default_factory=list)
    dashboard_run_errors: list[str] = field(default_factory=list)
    sampled_task_ids: list[str] = field(default_factory=list)
    sampled_run_ids: list[int] = field(default_factory=list)
    delegation_live_transcripts: list[str] = field(default_factory=list)
    delegation_ids: list[str] = field(default_factory=list)
    classifier: str = "DASHBOARD_UNAVAILABLE_DEGRADED"
    dedupe_lane_id: str | None = None
    existing_lane_id: str | None = None
    reasons: list[str] = field(default_factory=list)


def run_cmd(cmd: list[str], timeout: int = 25) -> CommandResult:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return CommandResult(p.returncode, p.stdout or "", p.stderr or "")
    except FileNotFoundError as exc:
        return CommandResult(None, error=f"not-found: {exc}")
    except Exception as exc:
        return CommandResult(None, error=repr(exc))


def http_get_json(base: str, path: str, timeout: float = 2.0) -> tuple[int | None, Any, str | None]:
    url = base.rstrip("/") + path
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(body) if body else None, None
            except json.JSONDecodeError as exc:
                return resp.status, None, f"json-error: {exc}"
    except urllib.error.HTTPError as exc:
        return exc.code, None, f"http-{exc.code}"
    except Exception as exc:
        return None, None, repr(exc)


def dashboard_bases() -> list[str]:
    explicit = os.environ.get("HERMES_DASHBOARD_BASE_URL")
    if explicit:
        return [explicit]
    ports = os.environ.get("HERMES_DASHBOARD_PORTS", "9119,8080,8642")
    return [f"http://127.0.0.1:{p.strip()}" for p in ports.split(",") if p.strip()]


def probe_dashboard() -> tuple[bool, str | None, dict[str, Any]]:
    errors = []
    for base in dashboard_bases():
        status, active, err = http_get_json(base, "/api/plugins/kanban/workers/active")
        if status != 200:
            errors.append(f"{base}/workers/active status={status} err={err}")
            continue
        # ``/workers/active`` is the availability gate.  Some Hermes builds do
        # not expose a top-level ``/inspect`` endpoint; their read-only process
        # inspection endpoint is ``/runs/{run_id}/inspect``.  Treat top-level
        # ``/inspect`` as optional so a missing route does not hide the working
        # dashboard worker/run endpoints or disable the SQLite fallback path.
        status_i, inspect, err_i = http_get_json(base, "/api/plugins/kanban/inspect")
        inspect_error = None if status_i == 200 else f"{base}/inspect status={status_i} err={err_i}"
        return True, None, {"base": base, "active": active, "inspect": inspect, "inspect_error": inspect_error}
    return False, "; ".join(errors[:4]) or "dashboard API unavailable", {}


def gateway_running_from_text(text: str) -> bool:
    lower = text.lower()
    return "running" in lower and "gateway" in lower and "not running" not in lower and "inactive" not in lower


def parse_dispatch_output(text: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    if not text.strip():
        return counts
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            for key in ZERO_KEYS:
                value = obj.get(key) or obj.get(key.replace("_", "-")) or 0
                if isinstance(value, list):
                    value = len(value)
                try:
                    counts[key] = int(value)
                except Exception:
                    counts[key] = 0
            return counts
    except Exception:
        pass
    label_map = {
        "Reclaimed": "reclaimed",
        "Crashed": "crashed",
        "Timed out": "timed_out",
        "Stale": "stale",
        "Auto-blocked": "auto_blocked",
        "Promoted": "promoted",
        "Spawned": "spawned",
    }
    for label, key in label_map.items():
        m = re.search(re.escape(label) + r"\s*:\s*(\d+)", text, re.I)
        if m:
            counts[key] = int(m.group(1))
    return counts


def safe_ro_connect(db: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
    con.row_factory = sqlite3.Row
    return con


def table_exists(con: sqlite3.Connection, name: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def column_names(con: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


def latest_text_for_task(con: sqlite3.Connection, task_id: str, task: sqlite3.Row) -> tuple[str, int | None, int | None, int | None]:
    parts = [task["title"] or ""]
    for col in ("body", "result", "last_failure_error"):
        if col in task.keys():
            parts.append(task[col] or "")
    latest_run_id = None
    latest_run_started = None
    latest_heartbeat = None
    if table_exists(con, "task_runs"):
        for r in con.execute(
            "SELECT id,status,outcome,summary,error,started_at FROM task_runs WHERE task_id=? ORDER BY started_at DESC, id DESC LIMIT 3",
            (task_id,),
        ):
            latest_run_id = latest_run_id or int(r["id"])
            latest_run_started = latest_run_started or r["started_at"]
            parts.extend([r["status"] or "", r["outcome"] or "", r["summary"] or "", r["error"] or ""])
    if table_exists(con, "task_comments"):
        for r in con.execute("SELECT body FROM task_comments WHERE task_id=? ORDER BY created_at DESC LIMIT 6", (task_id,)):
            parts.append(r["body"] or "")
    if table_exists(con, "task_events"):
        for r in con.execute("SELECT kind,payload,created_at FROM task_events WHERE task_id=? ORDER BY created_at DESC LIMIT 12", (task_id,)):
            kind = r["kind"] or ""
            payload = r["payload"] or ""
            parts.extend([kind, payload])
            if HEARTBEAT_RE.search(kind):
                latest_heartbeat = latest_heartbeat or int(r["created_at"] or 0)
    return "\n".join(p for p in parts if p), latest_run_id, latest_run_started, latest_heartbeat


def sqlite_board_snapshot(board: str, db: Path, *, sample_limit: int = 30) -> tuple[dict[str, int], list[TaskSample]]:
    con = safe_ro_connect(db)
    try:
        if not table_exists(con, "tasks"):
            return {}, []
        cols = column_names(con, "tasks")
        counts = {r["status"]: int(r["n"]) for r in con.execute("SELECT status, COUNT(*) n FROM tasks GROUP BY status")}
        select_cols = ["id", "title", "status"]
        for optional in ("assignee", "created_at", "started_at", "consecutive_failures", "last_failure_error", "body", "result"):
            if optional in cols:
                select_cols.append(optional)
        rows = con.execute(
            f"SELECT {', '.join(select_cols)} FROM tasks WHERE status IN ('ready','running','blocked') ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'blocked' THEN 1 ELSE 2 END, COALESCE(started_at, created_at, 0) ASC LIMIT ?",
            (sample_limit,),
        ).fetchall()
        samples: list[TaskSample] = []
        for row in rows:
            text, run_id, run_started, heartbeat_at = latest_text_for_task(con, row["id"], row)
            samples.append(TaskSample(
                id=row["id"],
                title=row["title"] or "",
                status=row["status"] or "",
                assignee=row["assignee"] if "assignee" in row.keys() else "",
                created_at=row["created_at"] if "created_at" in row.keys() else None,
                started_at=row["started_at"] if "started_at" in row.keys() else None,
                consecutive_failures=int(row["consecutive_failures"] or 0) if "consecutive_failures" in row.keys() else 0,
                last_failure_error=row["last_failure_error"] if "last_failure_error" in row.keys() else "",
                latest_text=text,
                latest_run_id=run_id,
                latest_run_started_at=run_started,
                latest_heartbeat_at=heartbeat_at,
            ))
        return counts, samples
    finally:
        con.close()


def cli_board_snapshot(board: str) -> tuple[CommandResult, CommandResult, dict[str, int], dict[str, int]]:
    env = os.environ.copy()
    env.pop("HERMES_KANBAN_DB", None)
    env.pop("HERMES_KANBAN_BOARD", None)
    # Keep this preflight cheap inside phone-status cron. Some gateway status
    # backends can hang while still leaving kanban CLI/SQLite evidence usable.
    gateway = run_cmd([HERMES, "gateway", "status"], timeout=5)
    dispatch = run_cmd([HERMES, "kanban", "--board", board, "dispatch", "--dry-run", "--max", "5", "--json"], timeout=45)
    listed = run_cmd([HERMES, "kanban", "--board", board, "list", "--json"], timeout=30)
    status_counts: dict[str, int] = {}
    if listed.rc == 0 and listed.stdout.strip():
        try:
            data = json.loads(listed.stdout)
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        st = str(item.get("status") or "")
                        if st:
                            status_counts[st] = status_counts.get(st, 0) + 1
        except Exception:
            pass
    return gateway, dispatch, parse_dispatch_output(dispatch.stdout), status_counts


def known_lane_hit(board: str, samples: list[TaskSample]) -> str | None:
    ids = {s.id for s in samples}
    for lane in DEDUP_LANES.get(board, []):
        if lane in ids:
            return f"{board}/{lane}"
    return None


def active_workers_from_dashboard(active: Any) -> list[dict[str, Any]]:
    if isinstance(active, dict) and isinstance(active.get("workers"), list):
        return [x for x in active["workers"] if isinstance(x, dict)]
    if isinstance(active, list):
        return [x for x in active if isinstance(x, dict)]
    return []


def fetch_active_workers(base: str, board: str) -> tuple[list[dict[str, Any]], str | None]:
    """Fetch active workers for one board through the dashboard REST API."""
    board_q = urllib.parse.quote(board, safe="")
    status, body, err = http_get_json(base, f"/api/plugins/kanban/workers/active?board={board_q}")
    if status == 200:
        return active_workers_from_dashboard(body), None
    return [], f"workers/active board={board} status={status} err={err}"


def collect_dashboard_runs(base: str, board: str, samples: list[TaskSample], *, limit: int = 10) -> tuple[list[int], list[str]]:
    """Fetch dashboard REST detail for current/recent sampled runs."""
    seen: set[int] = set()
    run_ids: list[int] = []
    for sample in samples:
        if sample.latest_run_id is None or sample.latest_run_id in seen:
            continue
        seen.add(sample.latest_run_id)
        run_ids.append(sample.latest_run_id)
        if len(run_ids) >= limit:
            break

    ok_ids: list[int] = []
    errors: list[str] = []
    board_q = urllib.parse.quote(board, safe="")
    by_id = {s.latest_run_id: s for s in samples if s.latest_run_id is not None}
    for run_id in run_ids:
        status, body, err = http_get_json(base, f"/api/plugins/kanban/runs/{run_id}?board={board_q}")
        if status == 200 and isinstance(body, dict) and isinstance(body.get("run"), dict):
            ok_ids.append(run_id)
            run = body["run"]
            sample = by_id.get(run_id)
            if sample is not None:
                sample.latest_text += "\n" + "\n".join(
                    str(run.get(k) or "") for k in ("status", "outcome", "summary", "error")
                )
                sample.latest_run_started_at = sample.latest_run_started_at or run.get("started_at")
            continue
        errors.append(f"run {run_id} status={status} err={err}")
    return ok_ids, errors


def collect_dashboard_run_inspections(base: str, board: str, run_ids: list[int]) -> tuple[list[int], list[str]]:
    """Fetch read-only per-run process inspection for sampled run ids."""
    ok_ids: list[int] = []
    errors: list[str] = []
    board_q = urllib.parse.quote(board, safe="")
    for run_id in run_ids:
        status, body, err = http_get_json(base, f"/api/plugins/kanban/runs/{run_id}/inspect?board={board_q}")
        if status == 200 and isinstance(body, dict) and body.get("run_id") == run_id:
            ok_ids.append(run_id)
            continue
        errors.append(f"inspect run {run_id} status={status} err={err}")
    return ok_ids, errors


def _ordered_regex_hits(pattern: re.Pattern[str], text: str, *, limit: int = 8) -> list[str]:
    """Return stable unique regex matches in first-seen order."""
    out: list[str] = []
    for match in pattern.findall(text or ""):
        if match not in out:
            out.append(match)
        if len(out) >= limit:
            break
    return out


def record_delegation_transcript_evidence(ev: BoardEvidence, samples: list[TaskSample]) -> None:
    """Attach v0.19 delegation side-channel evidence to abnormal boards.

    ``delegate_task`` now returns live transcript paths under
    ``cache/delegation/live/<delegation_id>/task-<n>.log`` at dispatch time and
    emits durable async completion records when the whole background batch
    finishes. Stall diagnostics must surface those paths as investigation
    pointers when they are present, but the paths are not a recovery guarantee:
    a running child may still be legitimately in flight, and a crashed parent
    does not imply child crash recovery.
    """
    for sample in samples:
        for path in _ordered_regex_hits(LIVE_TRANSCRIPT_PATH_RE, sample.latest_text):
            if path not in ev.delegation_live_transcripts:
                ev.delegation_live_transcripts.append(path)
        for deleg_id in _ordered_regex_hits(DELEGATION_ID_RE, sample.latest_text):
            if deleg_id not in ev.delegation_ids:
                ev.delegation_ids.append(deleg_id)


def add_delegation_diagnostic_reason(ev: BoardEvidence) -> None:
    """Add a concise abnormal-path hint without adding clean-path noise."""
    if ev.delegation_live_transcripts:
        ev.reasons.append(
            "delegation live transcript path(s) present; inspect cache/delegation/live logs before declaring a no-black-hole/stall"
        )
    elif ev.delegation_ids:
        ev.reasons.append(
            "delegation id present; check async completion queue and cache/delegation/live/<delegation_id>/manifest.json before declaring a no-black-hole/stall"
        )


def classify_board(ev: BoardEvidence, samples: list[TaskSample], now: int, stale_seconds: int) -> None:
    for sample in samples:
        if sample.id not in ev.sampled_task_ids:
            ev.sampled_task_ids.append(sample.id)
        if sample.latest_run_id is not None and sample.latest_run_id not in ev.sampled_run_ids:
            ev.sampled_run_ids.append(sample.latest_run_id)

    record_delegation_transcript_evidence(ev, samples)

    review_samples = [s for s in samples if s.status == "blocked" and REVIEW_RE.search(s.latest_text)]
    if review_samples:
        ev.classifier = "REVIEW_OR_APPROVAL_GATE"
        ev.reasons.append("review-required/needs-approval/critical-list blocker must preserve gate")
        return

    auth_samples = [s for s in samples if AUTH_RE.search(s.latest_text) and ("protocol" in s.latest_text.lower() or "rc=0" in s.latest_text.lower() or s.status == "blocked")]
    if auth_samples:
        ev.classifier = "PROVIDER_AUTH_PRE_REASONING"
        fallback_lanes = DEDUP_LANES.get(ev.board, [])
        lane = known_lane_hit(ev.board, auth_samples) or known_lane_hit(ev.board, samples)
        if lane is None and fallback_lanes:
            lane = f"{ev.board}/{fallback_lanes[0]}"
        ev.dedupe_lane_id = lane
        ev.existing_lane_id = lane
        ev.reasons.append("provider/auth/quota evidence before useful worker reasoning")
        return

    crash_samples = [s for s in samples if s.consecutive_failures >= FAILURE_LIMIT or CRASH_RE.search(s.latest_text)]
    if crash_samples:
        ev.classifier = "RESPAWN_GUARD_OR_CRASHLOOP"
        ev.reasons.append("consecutive failure, spawn_failed, pid-not-alive, or failure-limit evidence")
        add_delegation_diagnostic_reason(ev)
        return

    stalled = []
    for s in samples:
        if s.status != "running":
            continue
        started = s.started_at or s.latest_run_started_at or s.created_at
        last_heartbeat = s.latest_heartbeat_at or started
        if started and now - int(started) > stale_seconds and (not last_heartbeat or now - int(last_heartbeat) > stale_seconds):
            stalled.append(s)
    if stalled:
        ev.classifier = "STALLED_RUN"
        ev.reasons.append(f"running task older than stale policy ({stale_seconds}s) with no recent heartbeat")
        add_delegation_diagnostic_reason(ev)
        return

    dry = ev.dispatch_dry_run or {}
    dry_all_zero = bool(dry) and all(int(dry.get(k, 0) or 0) == 0 for k in ZERO_KEYS)
    ready_backlog = int(ev.status_counts.get("ready", 0) or 0)
    if ev.gateway_running and dry_all_zero and ready_backlog == 0:
        ev.classifier = "HEALTHY_IDLE"
        ev.reasons.append("gateway running, dispatch dry-run all-zero, no ready backlog requiring workers")
        return

    if ev.fallback_reason:
        ev.classifier = "DASHBOARD_UNAVAILABLE_DEGRADED"
        ev.reasons.append("dashboard unavailable but CLI/SQLite fallback produced evidence")
        return

    ev.classifier = "DASHBOARD_UNAVAILABLE_DEGRADED"
    ev.reasons.append("insufficient high-confidence worker visibility evidence")


def live_collect(boards: list[str], stale_seconds: int) -> dict[str, Any]:
    now = int(time.time())
    dashboard_ok, dashboard_error, dash = probe_dashboard()
    output: dict[str, Any] = {"generated_at": now, "dashboard": {"ok": dashboard_ok, "fallback_reason": dashboard_error}, "boards": []}
    for board in boards:
        ev = BoardEvidence(board=board)
        samples: list[TaskSample] = []
        db = BOARDS_DIR / board / "kanban.db"
        if dashboard_ok:
            ev.source_used = "dashboard_rest"
            base = str(dash.get("base") or "")
            active_workers, active_err = fetch_active_workers(base, board)
            if active_err:
                # Fall back to the base probe payload if the board-specific
                # query is blocked by an older route implementation.
                active_workers = active_workers_from_dashboard(dash.get("active"))
                ev.dashboard_run_errors.append(active_err)
            if dash.get("inspect_error"):
                ev.dashboard_run_errors.append(str(dash["inspect_error"]))
            ev.active_worker_ids = [str(x.get("task_id") or x.get("id")) for x in active_workers]
        else:
            ev.fallback_reason = dashboard_error
            gateway, dispatch, dry, cli_counts = cli_board_snapshot(board)
            cli_ok = (gateway.rc == 0 or dispatch.rc == 0 or bool(cli_counts))
            ev.gateway_running = gateway_running_from_text(gateway.stdout + "\n" + gateway.stderr) if gateway.rc == 0 else None
            ev.dispatch_dry_run = dry
            if cli_counts:
                ev.status_counts = cli_counts
            if cli_ok:
                ev.source_used = "cli_fallback"
            else:
                ev.source_used = "sqlite_ro_fallback"
                ev.fallback_reason = (ev.fallback_reason or "") + f"; CLI unavailable: gateway={gateway.error or gateway.rc} dispatch={dispatch.error or dispatch.rc}"
        if db.exists():
            try:
                sqlite_counts, samples = sqlite_board_snapshot(board, db)
                if sqlite_counts:
                    ev.status_counts = sqlite_counts
                if ev.source_used == "none":
                    ev.source_used = "sqlite_ro_fallback"
            except Exception as exc:
                ev.reasons.append(f"sqlite read failed: {exc!r}")
        else:
            ev.reasons.append(f"board db missing: {db}")
        if dashboard_ok and samples:
            base = str(dash.get("base") or "")
            ev.dashboard_run_ids, run_errors = collect_dashboard_runs(base, board, samples)
            inspect_ids, inspect_errors = collect_dashboard_run_inspections(base, board, ev.dashboard_run_ids)
            ev.dashboard_inspect_run_ids = inspect_ids
            ev.dashboard_run_errors.extend(run_errors)
            ev.dashboard_run_errors.extend(inspect_errors)
        classify_board(ev, samples, now, stale_seconds)
        output["boards"].append(ev.__dict__)
    return output


def fixture_collect(path: Path, stale_seconds: int) -> dict[str, Any]:
    data = json.loads(path.read_text())
    now = int(data.get("generated_at") or time.time())
    out = {"generated_at": now, "dashboard": data.get("dashboard", {}), "boards": []}
    for raw in data.get("boards", []):
        ev = BoardEvidence(
            board=raw["board"],
            source_used=raw.get("source_used", "fixture"),
            fallback_reason=raw.get("fallback_reason"),
            gateway_running=raw.get("gateway_running"),
            dispatch_dry_run={k: int(v) for k, v in raw.get("dispatch_dry_run", {}).items()},
            status_counts={k: int(v) for k, v in raw.get("status_counts", {}).items()},
            active_worker_ids=list(raw.get("active_worker_ids", [])),
        )
        samples = [TaskSample(**s) for s in raw.get("samples", [])]
        classify_board(ev, samples, now, stale_seconds)
        out["boards"].append(ev.__dict__)
    return out


def phone_lines(payload: dict[str, Any]) -> list[str]:
    lines = []
    for board in payload.get("boards", []):
        classifier = board.get("classifier") or "DASHBOARD_UNAVAILABLE_DEGRADED"
        prefix = PREFIX.get(classifier, "DEGRADED evidence")
        counts = board.get("status_counts") or {}
        bits = [
            f"source={board.get('source_used')}",
            f"ready={counts.get('ready', 0)}",
            f"running={counts.get('running', 0)}",
            f"blocked={counts.get('blocked', 0)}",
        ]
        if board.get("dedupe_lane_id"):
            bits.append(f"dedupe={board['dedupe_lane_id']}")
        if board.get("fallback_reason") and classifier == "DASHBOARD_UNAVAILABLE_DEGRADED":
            bits.append("fallback=dashboard-unavailable")
        if classifier in {"RESPAWN_GUARD_OR_CRASHLOOP", "STALLED_RUN"}:
            live_paths = board.get("delegation_live_transcripts") or []
            delegation_ids = board.get("delegation_ids") or []
            if live_paths:
                bits.append("delegation_transcripts=" + ",".join(live_paths[:2]))
            elif delegation_ids:
                bits.append("delegations=" + ",".join(delegation_ids[:3]))
        sample_ids = board.get("sampled_task_ids") or []
        if sample_ids:
            bits.append("samples=" + ",".join(sample_ids[:5]))
        lines.append(f"{prefix} {board.get('board')}: " + " | ".join(bits))
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--boards", default=os.environ.get("WORKER_VISIBILITY_BOARDS", "jarvis-os,sycode-trading,upero,orchestrator-sync"))
    ap.add_argument("--json-out", type=Path, default=OUT_JSON)
    ap.add_argument("--fixture", type=Path, help="Use controlled JSON fixture instead of live probes")
    ap.add_argument("--stale-seconds", type=int, default=DEFAULT_STALE_SECONDS)
    args = ap.parse_args(argv)

    boards = [b.strip() for b in args.boards.split(",") if b.strip()]
    payload = fixture_collect(args.fixture, args.stale_seconds) if args.fixture else live_collect(boards, args.stale_seconds)
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print("# Worker visibility preflight")
    print(f"Evidence JSON: {args.json_out}")
    for line in phone_lines(payload):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
