#!/usr/bin/env python3
"""Run one explicit, disposable adverse Claude-executor abort proof.

This is an executable evidence harness, not a worker, scheduler, or provider
router.  It refuses to launch Claude unless ``--run`` is supplied.  A fresh
temporary board and task workspace are created for every invocation.  It then
creates a harmless Claude session, persists an exact dispatcher binding, and
starts precisely one resumed Claude process through ``ClaudeResumeExecutor``.

The runner intercepts the post-launch callback *after* native PID registration
and uses a second SQLite connection to reclaim that run.  The remote-shaped
claimer makes the native reclaim a fence only; the executor must observe its
own lost heartbeat and terminate the process group.  No live Hermes board,
provider configuration, scheduler, deployment, external message, or trading
path is opened by this script.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from hermes_cli import kanban_db as kb
from hermes_cli.claude_executor import ClaudeResumeExecutor, SubprocessClaudeRunner


DENIED_TOOLS = "Bash,Edit,Write,Read,Glob,Grep,WebFetch,WebSearch,Task"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def process_group_gone(pgid: int, *, timeout_seconds: float = 8.0) -> bool:
    """Return only a boolean: evidence never retains the real PID."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.1)
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    return False


class ReclaimAfterRegistrationRunner:
    """Fence the exact live run from a separate board connection.

    The delegated executor callback writes ``external_executor_started`` in
    the primary connection.  Only after that committed fact is visible does
    this wrapper open a second connection and reclaim.  The claimer is
    deliberately non-local, so ``reclaim_task`` cannot terminate the PID
    itself; the proof needs the executor's heartbeat/``killpg`` path.
    """

    def __init__(self, *, board: Path, task_id: str, run_id: int) -> None:
        self.board = board
        self.task_id = task_id
        self.run_id = run_id
        self.reclaimed = False
        self.started_event_seen = False
        self.pid: int | None = None
        self._runner = SubprocessClaudeRunner()

    def run(self, **kwargs):
        registered: Callable[[int], None] | None = kwargs.get("on_process_started")
        if registered is None:
            raise RuntimeError("executor did not provide native PID registration callback")

        def reclaim_after_registration(pid: int) -> None:
            registered(pid)
            self.pid = pid
            separate = kb.connect(self.board)
            try:
                event = separate.execute(
                    "SELECT payload FROM task_events WHERE task_id=? AND run_id=? "
                    "AND kind='external_executor_started' ORDER BY id DESC LIMIT 1",
                    (self.task_id, self.run_id),
                ).fetchone()
                if event is None:
                    raise RuntimeError("PID registration event was not visible to separate connection")
                payload = json.loads(event["payload"])
                if payload.get("pid") != pid or payload.get("run_id") != self.run_id:
                    raise RuntimeError("separate connection saw mismatched executor registration")
                self.started_event_seen = True
                if not kb.reclaim_task(
                    separate, self.task_id, reason="disposable adverse executor abort canary"
                ):
                    raise RuntimeError("separate connection could not reclaim registered task")
                self.reclaimed = True
            finally:
                separate.close()

        kwargs["on_process_started"] = reclaim_after_registration
        return self._runner.run(**kwargs)


def initial_session(*, workspace: Path) -> tuple[str, list[str]]:
    """Create the one disposable session to be resumed by the real executor."""
    command = [
        "claude", "--print", "--verbose", "--output-format", "stream-json",
        "--include-hook-events", "--permission-mode", "plan",
        "--disallowedTools", DENIED_TOOLS, "--safe-mode", "--strict-mcp-config",
        "--max-turns", "1",
    ]
    completed = subprocess.run(
        command,
        input=(
            "Reply with exactly ABORT_CANARY_SESSION_READY. Do not call tools, "
            "read files, or take any action.\n"
        ),
        cwd=workspace,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=180,
        check=False,
    )
    if completed.returncode:
        raise RuntimeError(f"initial Claude exited with status {completed.returncode}")
    events = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
    init = [
        event for event in events
        if event.get("type") == "system" and event.get("subtype") == "init"
    ]
    result = [event for event in events if event.get("type") == "result"]
    if len(init) != 1 or len(result) != 1:
        raise RuntimeError("initial Claude stream did not have exactly one init/result")
    session_id = init[0].get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise RuntimeError("initial Claude stream did not expose a session id")
    if result[0].get("session_id") != session_id:
        raise RuntimeError("initial terminal result did not match its init session")
    if "ABORT_CANARY_SESSION_READY" not in str(result[0].get("result", "")):
        raise RuntimeError("initial Claude marker was not returned")
    system_subtypes = sorted({str(event.get("subtype")) for event in events if event.get("type") == "system"})
    if system_subtypes != ["init"]:
        raise RuntimeError(f"restricted bootstrap emitted unexpected system subtypes: {system_subtypes}")
    return session_id, system_subtypes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--run", action="store_true", help="perform the real disposable abort proof")
    args = parser.parse_args()
    if not args.run:
        raise SystemExit("refusing to invoke Claude without explicit --run")

    evidence: dict[str, object] = {
        "schema": "hermes.real-claude-executor-abort-canary.v2",
        "status": "FAIL",
        "started_at": int(time.time()),
        "scope": "fresh temporary Hermes board and task workspace only; no live board",
        "exclusions": [
            "trading", "deployments", "credentials", "external messages",
            "scheduler", "daemon", "provider fallback",
        ],
        "source_sha256": {
            "kanban_db.py": sha256(REPO_ROOT / "hermes_cli/kanban_db.py"),
            "claude_executor.py": sha256(REPO_ROOT / "hermes_cli/claude_executor.py"),
            "harness": sha256(Path(__file__)),
        },
    }
    root = Path(tempfile.mkdtemp(prefix="hermes-real-executor-abort-canary."))
    evidence["temporary_root_sha256"] = digest(str(root))
    conn = None
    failure: BaseException | None = None
    try:
        board = root / "canary.db"
        kb.init_db(board)
        conn = kb.connect(board)
        task_id = kb.create_task(
            conn,
            title="Disposable adverse Claude executor abort canary",
            body="Temporary isolated reclaim/killpg protocol proof only.",
            created_by="codex-canary",
            workspace_kind="scratch",
            provider_override=kb.PROVIDER_CLAUDE_CODE,
            model_override="claude-canary",
            initial_status="blocked",
        )
        if not kb.unblock_task(conn, task_id):
            raise RuntimeError("could not prepare disposable task")
        workspace = root / task_id
        workspace.mkdir()

        # First run creates exactly one fresh persisted dispatcher session.
        if kb.claim_task(conn, task_id, claimer="canary:initial", ttl_seconds=180) is None:
            raise RuntimeError("could not claim initial disposable task")
        initial_run = int(conn.execute(
            "SELECT current_run_id FROM tasks WHERE id=?", (task_id,)
        ).fetchone()["current_run_id"])
        session_id, initial_system_subtypes = initial_session(workspace=workspace)
        if not kb.reclaim_task(conn, task_id, reason="disposable session handoff"):
            raise RuntimeError("could not release initial disposable task")
        now = int(time.time())
        kb.record_worker_session_provenance(
            conn, run_id=initial_run, worker_session_id=session_id,
            source=kb.SESSION_SOURCE_DISPATCHER,
        )
        kb.record_session_binding(
            conn,
            run_id=initial_run,
            task_id=task_id,
            provider=kb.PROVIDER_CLAUDE_CODE,
            session_id=session_id,
            source=kb.SESSION_SOURCE_DISPATCHER,
            owner=kb.DISPATCHER_BINDING_OWNER,
            issued_at=now,
            expires_at=now + 600,
            now=now,
        )
        if initial_run not in kb.record_worker_completion_events(conn):
            raise RuntimeError("initial run did not produce native completion event")
        completion = conn.execute(
            "SELECT payload FROM task_events WHERE run_id=? AND kind=?",
            (initial_run, kb.BROKER_EVENT_WORKER_COMPLETION),
        ).fetchone()
        if completion is None:
            raise RuntimeError("missing initial completion payload")
        decision = kb.decide_route(
            completion=json.loads(completion["payload"]),
            task_row=conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone(),
        )
        if decision.route != kb.ROUTE_CONTINUE:
            raise RuntimeError(f"initial task did not route CONTINUE: {decision.route}")

        # This remote-shaped claimer prevents reclaim from issuing a direct
        # local PID signal.  The real executor must observe its lost lease and
        # kill its own process group.
        claimer = "remote-canary:executor"
        if kb.claim_task(conn, task_id, claimer=claimer, ttl_seconds=180) is None:
            raise RuntimeError("could not claim resumed disposable task")
        with kb.write_txn(conn):
            conn.execute(
                "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?,?,?,?)",
                (task_id, "canary-operator", "A3_GATE=GRANTED", int(time.time())),
            )
        request = kb.prepare_resume_request(
            conn,
            decision=decision,
            instruction=(
                "Reply with exactly ABORT_CANARY_SHOULD_NOT_COMPLETE. Do not call tools, "
                "read files, or take any action."
            ),
            now=int(time.time()),
            timeout_seconds=120,
        )
        if request.fence.current_run_id is None:
            raise RuntimeError("resume fence has no current run")
        runner = ReclaimAfterRegistrationRunner(
            board=board, task_id=task_id, run_id=request.fence.current_run_id
        )
        try:
            ClaudeResumeExecutor(
                armed=True,
                runner=runner,
                heartbeat_interval_seconds=20,
                claim_ttl_seconds=180,
                workspace_root=root,
            ).execute(
                conn,
                request=request,
                claimer=claimer,
                workspace=workspace,
                policy=kb.ExecutorPolicy(allow_real_execution=True),
                now=int(time.time()),
            )
        except kb.ClaimLeaseLost:
            claim_lost = True
        else:
            raise RuntimeError("adverse executor unexpectedly retained its claim")

        task = conn.execute(
            "SELECT status, worker_pid FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        completed_events = conn.execute(
            "SELECT COUNT(*) AS n FROM task_events WHERE task_id=? AND kind=?",
            (task_id, kb.EXEC_EVENT_COMPLETED),
        ).fetchone()["n"]
        run = conn.execute(
            "SELECT status, outcome, worker_pid FROM task_runs WHERE id=?",
            (request.fence.current_run_id,),
        ).fetchone()
        if runner.pid is None:
            raise RuntimeError("real executor did not expose a process pid")
        group_gone = process_group_gone(runner.pid)
        if not (runner.started_event_seen and runner.reclaimed and claim_lost and group_gone):
            raise RuntimeError("adverse reclaim/abort invariants were not all proven")
        if task["status"] != "ready" or task["worker_pid"] is not None:
            raise RuntimeError("reclaimed task was not ready with cleared worker pid")
        if run["status"] != "reclaimed" or run["outcome"] != "reclaimed" or run["worker_pid"] is not None:
            raise RuntimeError("reclaimed run did not retain native reclaimed state")
        if completed_events != 0:
            raise RuntimeError("reclaimed executor wrote a forbidden completion event")
        evidence.update({
            "status": "PASS",
            "result": {
                "external_executor_started_seen_from_separate_connection": True,
                "separate_connection_reclaimed": True,
                "claim_lease_lost": True,
                "process_group_gone": True,
                "completed_events": 0,
                "task_status": "ready",
                "task_worker_pid_cleared": True,
                "run_status": "reclaimed",
                "run_worker_pid_cleared": True,
            },
            "redacted": {
                "task_sha256": digest(task_id),
                "initial_run": initial_run,
                "resumed_run": request.fence.current_run_id,
                "session_sha256": digest(session_id),
                "process_group_sha256": digest(str(runner.pid)),
            },
            "restricted_surface": {
                "permission_mode": "plan",
                "denied_tools": DENIED_TOOLS,
                "safe_mode": True,
                "strict_mcp_config": True,
                "max_turns": 1,
                "bootstrap_system_subtypes": initial_system_subtypes,
                "resume_command_system_subtypes": "not observable because reclaim follows native PID registration before stream read; current positive canary attests init-only resume stream for the same canonical command",
            },
            "meaning": (
                "The separate connection reclaimed the exact registered run. The executor "
                "then observed ClaimLeaseLost and its own process-group cleanup completed "
                "before a terminal result could be accepted."
            ),
        })
    except BaseException as exc:
        failure = exc
        evidence["failure_class"] = type(exc).__name__
        evidence["failure_message"] = str(exc)[:300]
    finally:
        if conn is not None:
            conn.close()
        shutil.rmtree(root, ignore_errors=True)
        evidence["temporary_root_removed"] = not root.exists()
        evidence["finished_at"] = int(time.time())
        args.evidence.parent.mkdir(parents=True, exist_ok=True)
        args.evidence.write_text(
            json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    if failure is not None:
        raise failure


if __name__ == "__main__":
    main()
