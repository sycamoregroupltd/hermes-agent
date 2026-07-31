#!/usr/bin/env python3
"""Run exactly one harmless Claude control-worker task through Hermes.

This is intentionally a one-shot activation verifier, not a service, queue
consumer, or scheduler.  It creates one fixed no-op task on the named Hermes
board, starts one restricted Claude session to obtain its declared session ID,
then performs exactly one resume through ``ClaudeResumeExecutor``.  The task's
identity, body, and rendered capsule are hashed into a receipt before either
provider call.  Re-running against the same board refuses because the
idempotency key already exists.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from hermes_cli import kanban_db as kb
from hermes_cli.claude_executor import ClaudeResumeExecutor

TITLE = "Controlled Claude worker activation verification — no-op"
BODY = (
    "Hermes control-plane verification only. Reply exactly "
    "CONTROLLED_WORKER_OK. Do not access files, tools, network resources, "
    "MCP servers, or external systems."
)
INSTRUCTION = (
    "Reply exactly CONTROLLED_WORKER_OK. This is a control-plane no-op. "
    "Do not access files, tools, network resources, MCP servers, or external systems."
)
IDEMPOTENCY_KEY = "controlled-claude-worker-activation-v1"
DENIED_TOOLS = "Bash,Edit,Write,Read,Glob,Grep,WebFetch,WebSearch,Task"
CANONICAL_BOARD = Path("/home/frank/.hermes/kanban/boards/orchestrator-sync/kanban.db")
CANONICAL_WORKSPACE_ROOT = Path("/home/frank/.hermes/controlled-worker-activation")
GLOBAL_ACTIVATION_LEASE = Path("/home/frank/.hermes/control-plane/controlled-claude-worker-activation-v1.json")
RESTRICTED_SURFACE = {
    "permission_mode": "plan",
    "denied_tools": DENIED_TOOLS,
    "safe_mode": True,
    "strict_mcp_config": True,
    "max_turns": 1,
}


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_receipt(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def acquire_global_one_shot_lease() -> str:
    """Atomically consume the global activation slot before any board write."""
    GLOBAL_ACTIVATION_LEASE.parent.mkdir(parents=True, exist_ok=True)
    lease = {
        "schema": "hermes.controlled-claude-worker-global-lease.v1",
        "board_sha256": digest(str(CANONICAL_BOARD)),
        "purpose": "one fixed Claude no-op activation only",
        "created_at": int(time.time()),
        "retirement": "permanent one-shot; do not delete or reuse",
    }
    try:
        fd = os.open(GLOBAL_ACTIVATION_LEASE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise RuntimeError("global controlled-worker activation lease already consumed") from exc
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(lease, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return digest(json.dumps(lease, sort_keys=True, separators=(",", ":")))


def parse_stream(raw: str) -> list[dict]:
    events = [json.loads(line) for line in raw.splitlines() if line.strip()]
    if not all(isinstance(event, dict) for event in events):
        raise RuntimeError("Claude emitted a non-object stream record")
    return events


def initial_session(workspace: Path) -> tuple[str, list[dict]]:
    command = [
        "claude", "--print", "--verbose", "--output-format", "stream-json",
        "--include-hook-events", "--permission-mode", "plan",
        "--disallowedTools", DENIED_TOOLS, "--safe-mode", "--strict-mcp-config",
        "--max-turns", "1",
    ]
    result = subprocess.run(
        command, input=INSTRUCTION + "\n", cwd=workspace, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120, check=False,
    )
    if result.returncode:
        raise RuntimeError(f"restricted bootstrap exited {result.returncode}")
    events = parse_stream(result.stdout)
    inits = [event for event in events if event.get("type") == "system" and event.get("subtype") == "init"]
    results = [event for event in events if event.get("type") == "result"]
    if len(inits) != 1 or len(results) != 1:
        raise RuntimeError("bootstrap stream did not contain exactly one init/result")
    session_id = inits[0].get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise RuntimeError("bootstrap did not return a session ID")
    if results[0].get("session_id") != session_id or "CONTROLLED_WORKER_OK" not in str(results[0].get("result", "")):
        raise RuntimeError("bootstrap marker/session validation failed")
    return session_id, events


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--board", type=Path, required=True)
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--run", action="store_true", help="perform the exactly-two-call activation verification")
    args = parser.parse_args()
    if not args.run:
        raise SystemExit("refusing provider invocation without explicit --run")

    board = args.board.resolve()
    root = args.workspace_root.resolve()
    if board != CANONICAL_BOARD or root != CANONICAL_WORKSPACE_ROOT:
        raise SystemExit("controlled activation is pinned to the canonical board and workspace root")
    receipt: dict[str, object] = {
        "schema": "hermes.controlled-claude-worker-activation-receipt.v1",
        "status": "preparing",
        "scope": "one fixed Hermes no-op task; no scheduler, fallback, deployment, trading, credentials, or external messaging",
        "provider_calls": {"bootstrap": 0, "resume": 0, "total": 0, "max_total": 2},
        "source_sha256": {
            "kanban_db.py": sha256(REPO_ROOT / "hermes_cli/kanban_db.py"),
            "claude_executor.py": sha256(REPO_ROOT / "hermes_cli/claude_executor.py"),
            "harness": sha256(Path(__file__)),
        },
        "started_at": int(time.time()),
    }
    lease_sha256 = acquire_global_one_shot_lease()
    receipt["global_activation_lease_sha256"] = lease_sha256
    receipt["canonical_board_sha256"] = digest(str(CANONICAL_BOARD))
    receipt["restricted_surface_sha256"] = digest(json.dumps(RESTRICTED_SURFACE, sort_keys=True, separators=(",", ":")))
    conn = kb.connect(board)
    workspace: Path | None = None
    try:
        task_id = kb.create_task(
            conn, title=TITLE, body=BODY, created_by="codex-controlled-activation",
            workspace_kind="scratch", provider_override=kb.PROVIDER_CLAUDE_CODE,
            model_override="claude-controlled-noop", initial_status="blocked",
            idempotency_key=IDEMPOTENCY_KEY, project_id="control-plane",
        )
        task = kb.get_task(conn, task_id)
        # The fixed key means a replay cannot receive a new task or a third call.
        if task is None or task.status != "blocked" or task.title != TITLE or task.body != BODY:
            raise RuntimeError("controlled activation key already consumed or task profile drifted")
        workspace = (root / task_id).resolve()
        if workspace.parent != root or workspace.exists():
            raise RuntimeError("controlled task workspace already exists or escapes declared root")
        workspace.mkdir(parents=True)
        assert kb.unblock_task(conn, task_id)
        assert kb.claim_task(conn, task_id, claimer="codex:controlled-bootstrap", ttl_seconds=180)
        bootstrap_run = int(conn.execute("SELECT current_run_id FROM tasks WHERE id=?", (task_id,)).fetchone()["current_run_id"])
        receipt.update({
            "status": "prepared",
            "task_id": task_id,
            "task_profile_sha256": digest(json.dumps({"title": TITLE, "body": BODY, "provider": kb.PROVIDER_CLAUDE_CODE, "model": "claude-controlled-noop"}, sort_keys=True)),
            "workspace_sha256": digest(str(workspace)),
            "bootstrap_run": bootstrap_run,
            "restricted_mode": RESTRICTED_SURFACE,
        })
        write_receipt(args.receipt, receipt)

        if receipt["provider_calls"]["total"] != 0:  # type: ignore[index]
            raise RuntimeError("provider-call counter was not clean")
        session_id, bootstrap_events = initial_session(workspace)
        receipt["provider_calls"] = {"bootstrap": 1, "resume": 0, "total": 1, "max_total": 2}
        receipt["bootstrap"] = {"session_sha256": digest(session_id), "event_types": sorted({str(event.get("type")) for event in bootstrap_events})}
        write_receipt(args.receipt, receipt)

        assert kb.reclaim_task(conn, task_id, reason="controlled bootstrap handoff")
        kb.record_worker_session_provenance(conn, run_id=bootstrap_run, worker_session_id=session_id, source=kb.SESSION_SOURCE_DISPATCHER)
        now = int(time.time())
        kb.record_session_binding(conn, run_id=bootstrap_run, task_id=task_id, provider=kb.PROVIDER_CLAUDE_CODE, session_id=session_id, source=kb.SESSION_SOURCE_DISPATCHER, owner=kb.DISPATCHER_BINDING_OWNER, issued_at=now, expires_at=now + 600, now=now)
        assert bootstrap_run in kb.record_worker_completion_events(conn)
        completion = conn.execute("SELECT payload FROM task_events WHERE run_id=? AND kind=?", (bootstrap_run, kb.BROKER_EVENT_WORKER_COMPLETION)).fetchone()
        if completion is None:
            raise RuntimeError("bootstrap completion was not folded")
        decision = kb.decide_route(completion=json.loads(completion["payload"]), task_row=conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())
        if decision.route != kb.ROUTE_CONTINUE:
            raise RuntimeError(f"bootstrap did not produce a CONTINUE route: {decision.route}")
        assert kb.record_route_decision_event(conn, decision)
        assert kb.claim_task(conn, task_id, claimer="codex:controlled-resume", ttl_seconds=180)
        kb.add_comment(conn, task_id, "codex-controlled-activation", "A3_GATE=GRANTED; fixed no-op control worker only")
        request = kb.prepare_resume_request(conn, decision=decision, instruction=INSTRUCTION, now=int(time.time()), timeout_seconds=120)
        receipt["capsule_sha256"] = digest(request.plan.capsule.to_json())
        receipt["resume_argv"] = list(request.plan.command.argv)
        write_receipt(args.receipt, receipt)

        if receipt["provider_calls"]["total"] != 1:  # type: ignore[index]
            raise RuntimeError("refusing a second resume or third provider call")
        outcome = ClaudeResumeExecutor(armed=True, heartbeat_interval_seconds=20, claim_ttl_seconds=180, workspace_root=root).execute(
            conn, request=request, claimer="codex:controlled-resume", workspace=workspace,
            policy=kb.ExecutorPolicy(allow_real_execution=True), now=int(time.time()),
        )
        receipt["provider_calls"] = {"bootstrap": 1, "resume": 1, "total": 2, "max_total": 2}
        terminal = kb.get_task(conn, task_id)
        if outcome.status != "completed" or not outcome.terminal_write or terminal is None or terminal.status != "done" or "CONTROLLED_WORKER_OK" not in outcome.summary:
            raise RuntimeError("controlled worker did not reach the fixed guarded terminal result")
        if not kb.retire_session_binding(conn, run_id=bootstrap_run):
            raise RuntimeError("controlled session binding was not retired")
        receipt.update({"status": "PASS", "terminal": {"status": outcome.status, "terminal_write": outcome.terminal_write}, "binding_retired": True})
    except Exception as exc:
        receipt.update({"status": "FAIL", "error_type": type(exc).__name__, "error": str(exc)[:500]})
        raise
    finally:
        if workspace is not None and workspace.exists():
            shutil.rmtree(workspace)
        receipt["workspace_removed"] = workspace is not None and not workspace.exists()
        receipt["finished_at"] = int(time.time())
        write_receipt(args.receipt, receipt)
        conn.close()


if __name__ == "__main__":
    main()
