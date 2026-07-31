#!/usr/bin/env python3
"""Run one explicit, disposable Claude resume canary and emit redacted evidence.

This is deliberately a one-shot verifier, not a service, scheduler, worker
pool, or provider router. It creates a fresh temporary Hermes board and task
workspace, runs exactly one initial Claude session plus one exact-session
resume, and removes the temporary root before writing the evidence JSON named
on the command line. It never opens a live Hermes board.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from hermes_cli import kanban_db as kb
from hermes_cli.broker_shadow import CanonicalShadowBroker
from hermes_cli.claude_executor import ClaudeResumeExecutor, SubprocessClaudeRunner


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def restricted_system_subtypes(events: list[dict]) -> list[str]:
    """Prove the restricted provider run did not emit hook lifecycle records."""
    subtypes = sorted({str(event.get("subtype")) for event in events if event.get("type") == "system"})
    if subtypes != ["init"]:
        raise RuntimeError(f"restricted Claude stream emitted unexpected system subtypes: {subtypes}")
    return subtypes


class CapturingRunner:
    """Retain only structured event metadata for the redacted attestation."""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self._runner = SubprocessClaudeRunner()

    def run(self, **kwargs):
        self.events = self._runner.run(**kwargs)
        return self.events


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--run", action="store_true", help="perform the real disposable proof")
    args = parser.parse_args()
    if not args.run:
        raise SystemExit("refusing to invoke Claude without explicit --run")

    repo = REPO_ROOT
    evidence: dict[str, object] = {
        "schema": "hermes.real-claude-executor-canary.v1",
        "started_at": int(time.time()),
        "scope": "temporary board and workspace only; no live Hermes board",
        "source_sha256": {
            "kanban_db.py": sha256(repo / "hermes_cli/kanban_db.py"),
            "claude_executor.py": sha256(repo / "hermes_cli/claude_executor.py"),
            "broker_shadow.py": sha256(repo / "hermes_cli/broker_shadow.py"),
            "harness": sha256(Path(__file__)),
        },
    }
    root = Path(tempfile.mkdtemp(prefix="hermes-real-executor-canary."))
    evidence["temporary_root_sha256"] = digest(str(root))
    conn = None
    try:
        board = root / "canary.db"
        kb.init_db(board)
        conn = kb.connect(board)
        task_id = kb.create_task(
            conn, title="Disposable Claude executor canary",
            body="Temporary isolated protocol proof only.", created_by="codex-canary",
            workspace_kind="scratch", provider_override=kb.PROVIDER_CLAUDE_CODE,
            model_override="claude-canary", initial_status="blocked",
        )
        assert kb.unblock_task(conn, task_id)
        workspace = root / task_id
        workspace.mkdir()
        assert kb.claim_task(conn, task_id, claimer="canary:initial", ttl_seconds=180)
        initial_run = int(conn.execute(
            "SELECT current_run_id FROM tasks WHERE id=?", (task_id,)
        ).fetchone()["current_run_id"])
        initial = subprocess.run(
            ["claude", "--print", "--verbose", "--output-format", "stream-json",
             "--include-hook-events", "--permission-mode", "plan",
             "--disallowedTools", "Bash,Edit,Write,Read,Glob,Grep,WebFetch,WebSearch,Task",
             "--safe-mode", "--strict-mcp-config",
             "--max-turns", "1"],
            input="Reply with exactly CANARY_SESSION_READY. Do not call tools, read files, or take any action.\n",
            cwd=workspace, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=180, check=False,
        )
        if initial.returncode:
            raise RuntimeError(f"initial Claude exit={initial.returncode}")
        initial_events = [json.loads(line) for line in initial.stdout.splitlines() if line.strip()]
        inits = [e for e in initial_events if e.get("type") == "system" and e.get("subtype") == "init"]
        results = [e for e in initial_events if e.get("type") == "result"]
        if len(inits) != 1 or len(results) != 1:
            raise RuntimeError("initial Claude stream did not have exactly one init/result")
        session_id = inits[0]["session_id"]
        if results[0].get("session_id") != session_id or "CANARY_SESSION_READY" not in results[0].get("result", ""):
            raise RuntimeError("initial session binding/marker check failed")
        evidence["initial_stream"] = {
            "event_types": sorted({str(e.get("type")) for e in initial_events}),
            "system_subtypes": restricted_system_subtypes(initial_events),
            "session_sha256": digest(session_id), "marker_verified": True,
        }

        assert kb.reclaim_task(conn, task_id, reason="disposable session handoff")
        kb.record_worker_session_provenance(conn, run_id=initial_run, worker_session_id=session_id,
                                            source=kb.SESSION_SOURCE_DISPATCHER)
        now = int(time.time())
        kb.record_session_binding(conn, run_id=initial_run, task_id=task_id,
                                  provider=kb.PROVIDER_CLAUDE_CODE, session_id=session_id,
                                  source=kb.SESSION_SOURCE_DISPATCHER,
                                  owner=kb.DISPATCHER_BINDING_OWNER,
                                  issued_at=now, expires_at=now + 600, now=now)
        assert initial_run in kb.record_worker_completion_events(conn)
        completion = conn.execute("SELECT payload FROM task_events WHERE run_id=? AND kind=?",
                                  (initial_run, kb.BROKER_EVENT_WORKER_COMPLETION)).fetchone()
        decision = kb.decide_route(completion=json.loads(completion["payload"]),
                                   task_row=conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())
        if decision.route != kb.ROUTE_CONTINUE:
            raise RuntimeError(f"initial reclaimed run did not route CONTINUE: {decision.route}")
        assert kb.claim_task(conn, task_id, claimer="canary:executor", ttl_seconds=180)
        with kb.write_txn(conn):
            conn.execute("INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?,?,?,?)",
                         (task_id, "canary-operator", "A3_GATE=GRANTED", int(time.time())))
        request = kb.prepare_resume_request(
            conn, decision=decision,
            instruction="Reply with exactly CANARY_RESUME_OK. Do not call tools, read files, or take any action.",
            now=int(time.time()), timeout_seconds=120,
        )
        capture = CapturingRunner()
        outcome = ClaudeResumeExecutor(armed=True, runner=capture, heartbeat_interval_seconds=20,
                                       claim_ttl_seconds=180, workspace_root=root).execute(
            conn, request=request, claimer="canary:executor", workspace=workspace,
            policy=kb.ExecutorPolicy(allow_real_execution=True), now=int(time.time()),
        )
        task = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
        resumed_results = [e for e in capture.events if e.get("type") == "result"]
        if len(resumed_results) != 1:
            raise RuntimeError("resumed stream did not have exactly one terminal result")
        resumed_result = resumed_results[0]
        if resumed_result.get("session_id") != session_id:
            raise RuntimeError("resumed stream terminal session did not match persisted binding")
        if "CANARY_RESUME_OK" not in outcome.summary or "CANARY_RESUME_OK" not in resumed_result.get("result", ""):
            raise RuntimeError("resumed stream did not return the exact canary marker")
        if outcome.status != "completed" or not outcome.terminal_write or task["status"] != "done":
            raise RuntimeError("resumed executor did not reach guarded done state")
        assert kb.retire_session_binding(conn, run_id=initial_run)
        broker = CanonicalShadowBroker(enabled=True, limit=8)
        token = "canary-shadow-token-ephemeral"
        cursor_before = kb.ensure_broker_sub(conn, consumer=broker.consumer, token=token)
        receipt = broker.run_once(conn, token=token)
        evidence.update({
            "result": "PASS", "task_sha256": digest(task_id), "initial_run": initial_run,
            "route": decision.route, "a3_granted": True, "binding_retired": True,
            "executor": {"status": outcome.status, "terminal_write": outcome.terminal_write},
            "resume_stream": {
                "event_types": sorted({str(e.get("type")) for e in capture.events}),
                "system_subtypes": restricted_system_subtypes(capture.events),
                "session_sha256": digest(str(resumed_result["session_id"])),
                "terminal_subtype": str(resumed_result.get("subtype")),
                "marker_verified": True,
            },
            "shadow": {"consumer": receipt.consumer, "preprovisioned_cursor": cursor_before,
                       "old_cursor": receipt.old_cursor, "new_cursor": receipt.new_cursor,
                       "cursor_advanced": receipt.new_cursor >= receipt.old_cursor},
        })
    finally:
        if conn is not None:
            conn.close()
        shutil.rmtree(root, ignore_errors=True)
        evidence["temporary_root_removed"] = not root.exists()
        evidence["finished_at"] = int(time.time())
        args.evidence.parent.mkdir(parents=True, exist_ok=True)
        args.evidence.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
