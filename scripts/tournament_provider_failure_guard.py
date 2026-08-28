#!/usr/bin/env python3
"""Provider-failure alert/cooldown guard for Sycode tournament jobs."""
from __future__ import annotations
import argparse, hashlib, json, os, re, shutil, sqlite3, subprocess, tempfile, time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_PROFILE = Path("/home/frank/.hermes/profiles/sycode-trading-pm")
DEFAULT_JOBS = DEFAULT_PROFILE / "cron/jobs.json"
DEFAULT_STATE = DEFAULT_PROFILE / "cron/state/tournament_provider_failure_guard.json"
DEFAULT_INCIDENT_BOARD = "jarvis-os"
DEFAULT_INCIDENT_TASK = "t_2e843344"
DEFAULT_HERMES = "/home/frank/.local/bin/hermes"
DEFAULT_TARGET_IDS = {"35606b3d2436"}
ERROR_RE = re.compile(r"429|rate limit|quota|provider_error|provider_pre_reasoning|provider pre reasoning|api call failed|no access token|credential pool|exhaust|requires available credits|spawn failure|spawn_failed|pid not alive|can't start new thread|resource temporarily unavailable", re.I)
BOUND_RE = re.compile(r"tournament|risk gate|risk-gate|daily eval", re.I)
EVIDENCE_RE = re.compile(r"PROVIDER_OWNER_EVIDENCE|PROVIDER[-_ ]OWNER[-_ ]EVIDENCE", re.I)

def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

def load_json(path: Path, default: Any) -> Any:
    try: return json.loads(path.read_text())
    except FileNotFoundError: return default

def save_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True); f.write("\n")
        os.replace(tmp, path)
    finally:
        try: os.unlink(tmp)
        except FileNotFoundError: pass

def jobs_iter(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, dict) and isinstance(raw.get("jobs"), list): return raw["jobs"]
    if isinstance(raw, dict): return [v for v in raw.values() if isinstance(v, dict)]
    if isinstance(raw, list): return [v for v in raw if isinstance(v, dict)]
    return []

def job_matches(job: dict[str, Any], target_ids: set[str]) -> bool:
    jid = str(job.get("id") or "")
    name = str(job.get("name") or "")
    prompt = str(job.get("prompt") or "")[:2000]
    return jid in target_ids or bool(BOUND_RE.search(f"{name}\n{prompt}"))

def error_blob(job: dict[str, Any]) -> str:
    return "\n".join(str(job.get(k) or "") for k in ("last_status", "last_error", "last_delivery_error", "name", "id", "last_run_at"))

def fingerprint(parts: list[str]) -> str:
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()[:16]

def backup_and_pause_job(jobs_path: Path, raw: Any, target_job_id: str, reason: str, dry_run: bool) -> str | None:
    changed = False; now = utc_now()
    for job in jobs_iter(raw):
        if str(job.get("id")) == target_job_id and job.get("enabled", True):
            job["enabled"] = False; job["state"] = "paused"; job["paused_at"] = now; job["paused_reason"] = reason; changed = True
    if not changed: return None
    backup = jobs_path.with_name(f"{jobs_path.name}.bak-provider-guard-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")
    if dry_run: return f"DRY_RUN would backup {jobs_path} -> {backup} and pause {target_job_id}"
    shutil.copy2(jobs_path, backup); save_json_atomic(jobs_path, raw); return str(backup)

def task_comments_contain(board: str, task_id: str, pattern: re.Pattern[str]) -> bool:
    db = Path(f"/home/frank/.hermes/kanban/boards/{board}/kanban.db")
    if not db.exists(): return False
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        for (body,) in con.execute("SELECT body FROM task_comments WHERE task_id=? ORDER BY created_at DESC LIMIT 80", (task_id,)):
            if pattern.search(body or ""): return True
    finally: con.close()
    return False

def post_stub_comment(board: str, task_id: str, body: str, dry_run: bool) -> str:
    if dry_run: return "DRY_RUN comment skipped"
    env = os.environ.copy(); env.setdefault("HERMES_HOME", str(DEFAULT_PROFILE)); env.setdefault("HERMES_PROFILE", "sycode-trading-pm")
    try:
        res = subprocess.run([DEFAULT_HERMES, "kanban", "--board", board, "comment", task_id, body], capture_output=True, text=True, timeout=60, env=env)
        return f"comment_rc={res.returncode} stderr={res.stderr.strip()[:200]} stdout={res.stdout.strip()[:200]}"
    except Exception as e:
        return f"comment_exception={type(e).__name__}: {e}"

def scan_spawn_failures(boards: list[str], since_seconds: int = 86400) -> list[dict[str, Any]]:
    cutoff = int(time.time()) - since_seconds; hits = []
    for board in boards:
        db = Path(f"/home/frank/.hermes/kanban/boards/{board}/kanban.db")
        if not db.exists(): continue
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True); con.row_factory = sqlite3.Row
        try:
            sql = """
            SELECT id,title,status,assignee,last_failure_error,consecutive_failures,started_at,created_at
            FROM tasks
            WHERE COALESCE(started_at, created_at, 0) >= ?
              AND (title LIKE '%Tournament%' OR title LIKE '%risk gate%' OR body LIKE '%tournament%' OR body LIKE '%risk gate%')
              AND (COALESCE(last_failure_error,'') <> '' OR consecutive_failures > 0)
            ORDER BY COALESCE(started_at, created_at) DESC LIMIT 20
            """
            for row in con.execute(sql, (cutoff,)):
                blob = f"{row['title']}\n{row['last_failure_error'] or ''}"
                if ERROR_RE.search(blob): hits.append({"board": board, **dict(row)})
        finally: con.close()
    return hits

def self_test() -> None:
    assert ERROR_RE.search("RuntimeError: HTTP 429: exceeded the rate limit")
    assert ERROR_RE.search("provider_pre_reasoning")
    assert ERROR_RE.search("pid not alive")
    assert BOUND_RE.search("Tournament risk gate 2026-07-15")
    assert job_matches({"id": "35606b3d2436", "name": "tournament-daily-evaluation"}, DEFAULT_TARGET_IDS)
    print("SELF_TEST_PASS")

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=Path, default=DEFAULT_JOBS); ap.add_argument("--state", type=Path, default=DEFAULT_STATE)
    ap.add_argument("--target-job-id", action="append", default=[]); ap.add_argument("--incident-board", default=DEFAULT_INCIDENT_BOARD); ap.add_argument("--incident-task", default=DEFAULT_INCIDENT_TASK)
    ap.add_argument("--no-pause", action="store_true"); ap.add_argument("--dry-run", action="store_true"); ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test: self_test(); return 0
    target_ids = set(DEFAULT_TARGET_IDS) | set(args.target_job_id or [])
    raw = load_json(args.jobs, []); state = load_json(args.state, {"seen": {}}); state.setdefault("seen", {})
    alerts = []; changed_state = False
    for job in jobs_iter(raw):
        if not job_matches(job, target_ids): continue
        if str(job.get("last_status") or "") != "error" or not ERROR_RE.search(error_blob(job)): continue
        fp = fingerprint([str(job.get("id")), str(job.get("last_run_at")), str(job.get("last_error")), str(job.get("last_delivery_error"))])
        if state["seen"].get(fp): continue
        evidence_present = task_comments_contain(args.incident_board, args.incident_task, EVIDENCE_RE)
        pause_result = None
        if not args.no_pause and not evidence_present:
            pause_result = backup_and_pause_job(args.jobs, raw, str(job.get("id")), f"provider-failure-cooldown {utc_now()} pending PROVIDER_OWNER_EVIDENCE on {args.incident_board}/{args.incident_task}", args.dry_run)
        stub = f"PROVIDER_FAILURE_COOLDOWN_STUB {utc_now()}\njob={job.get('id')} name={job.get('name')} last_status={job.get('last_status')} last_run_at={job.get('last_run_at')}\ndetected_terms=429/provider_error/provider_pre_reasoning/spawn-failure guard\ncooldown={'paused' if pause_result else 'alert-only-or-already-paused'}; backup={pause_result}\nprovider_owner_action_required: add PROVIDER_OWNER_EVIDENCE with scope/root-cause/reset evidence before re-enabling.\nerror={str(job.get('last_error') or job.get('last_delivery_error') or '')[:1000]}"
        comment_result = post_stub_comment(args.incident_board, args.incident_task, stub, args.dry_run)
        alerts.append("CRITICAL Sycode tournament provider-failure guard fired\n" + f"- job: {job.get('id')} {job.get('name')}\n- last_run_at: {job.get('last_run_at')}\n- cooldown: {'paused pending provider-owner evidence' if pause_result else 'alert-only/already-paused/evidence-present'}\n- evidence task: {args.incident_board}/{args.incident_task}\n- jobs backup: {pause_result}\n- comment: {comment_result}\n- error: {str(job.get('last_error') or job.get('last_delivery_error') or '')[:1200]}")
        state["seen"][fp] = {"at": utc_now(), "job_id": job.get("id"), "paused_backup": pause_result}; changed_state = True
    spawn_hits = scan_spawn_failures(["sycode-trading", "jarvis-os"])
    if spawn_hits:
        fp = fingerprint([json.dumps(spawn_hits, sort_keys=True, default=str)])
        if not state["seen"].get(fp):
            rows = [f"- {h['board']}/{h['id']} {h['status']} cf={h['consecutive_failures']} title={h['title']} err={str(h['last_failure_error'])[:240]}" for h in spawn_hits[:10]]
            alerts.append("WARN tournament/risk-gate spawn/provider failures detected in kanban:\n" + "\n".join(rows))
            state["seen"][fp] = {"at": utc_now(), "kind": "spawn_hits", "count": len(spawn_hits)}; changed_state = True
    if changed_state and not args.dry_run: save_json_atomic(args.state, state)
    if alerts: print("\n\n".join(alerts))
    return 0
if __name__ == "__main__": raise SystemExit(main())
