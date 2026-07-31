"""Explicit, Claude-only edge transport for one persisted Hermes resume.

This module is intentionally not a worker, scheduler, or provider registry.  It
has no import-time side effects and is *disabled by default*.  A caller has to
prepare a native :class:`ResumeRequest`, provide the exact claim owner, and
explicitly arm this object for one invocation.  Native Hermes remains the
authority for binding, A3, fencing, terminal state, and notifications.
"""

from __future__ import annotations

import json
import math
import os
import select
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable, Protocol, Sequence

from . import kanban_db as kb


class ClaudeExecutorDisabled(kb.ExecutorError):
    """The concrete transport was not explicitly armed for this invocation."""


class ClaudeExecutorProtocolError(kb.ExecutorError):
    """The real CLI stream did not satisfy the bounded executor protocol."""


class ClaudeProcessRunner(Protocol):
    """Testable process boundary; no provider discovery or fallback exists."""

    def run(
        self,
        *,
        argv: Sequence[str],
        input_jsonl: str,
        cwd: Path,
        timeout_seconds: int,
        heartbeat: Callable[[], None],
        heartbeat_interval_seconds: float,
        on_process_started: Callable[[int], None] | None = None,
    ) -> list[dict]:
        ...


class SubprocessClaudeRunner:
    """Run one already-rendered Claude command while heartbeating in-band.

    ``select`` keeps the heartbeat on the owning SQLite thread.  A background
    thread would be unsafe for the normal SQLite connection and could keep a
    stale task alive after the controlling executor had failed.
    """

    def run(
        self,
        *,
        argv: Sequence[str],
        input_jsonl: str,
        cwd: Path,
        timeout_seconds: int,
        heartbeat: Callable[[], None],
        heartbeat_interval_seconds: float,
        on_process_started: Callable[[int], None] | None = None,
    ) -> list[dict]:
        if not cwd.is_dir():
            raise ClaudeExecutorProtocolError(f"execution cwd does not exist: {cwd}")
        if not input_jsonl.endswith("\n"):
            raise ClaudeExecutorProtocolError("canonical Claude input must be JSONL")
        started = time.monotonic()
        next_heartbeat = started
        events: list[dict] = []
        # Keep stderr physically separate from the typed stdout protocol.  A
        # temporary file cannot back-pressure a long-running provider the way
        # an unread PIPE can; only a bounded tail is exposed on failure.
        with tempfile.TemporaryFile(mode="w+t", encoding="utf-8", errors="replace") as stderr_file:
            process = subprocess.Popen(
                tuple(argv), cwd=str(cwd), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=stderr_file, text=True, encoding="utf-8", errors="replace",
                start_new_session=True,
            )
            try:
                if on_process_started is not None:
                    on_process_started(process.pid)
                assert process.stdin is not None
                process.stdin.write(input_jsonl)
                process.stdin.close()
                assert process.stdout is not None
                while True:
                    now = time.monotonic()
                    if now - started > timeout_seconds:
                        raise kb.ExecutionTimeoutError(
                            f"Claude exceeded {timeout_seconds}s"
                        )
                    if now >= next_heartbeat:
                        heartbeat()
                        next_heartbeat = now + heartbeat_interval_seconds
                    readable, _, _ = select.select([process.stdout], [], [], min(
                        max(0.0, next_heartbeat - time.monotonic()), 0.5
                    ))
                    if readable:
                        line = process.stdout.readline()
                        if line:
                            try:
                                event = json.loads(line)
                            except json.JSONDecodeError as exc:
                                raise ClaudeExecutorProtocolError(
                                    "Claude produced non-JSON stream output"
                                ) from exc
                            if not isinstance(event, dict):
                                raise ClaudeExecutorProtocolError(
                                    "Claude stream event must be an object"
                                )
                            events.append(event)
                            continue
                    if process.poll() is not None:
                        # Drain any complete trailing stream records before reading
                        # the terminal state.  A blank/non-JSON tail is a protocol
                        # failure, not a success inferred from return code.
                        for line in process.stdout:
                            if not line.strip():
                                continue
                            try:
                                event = json.loads(line)
                            except json.JSONDecodeError as exc:
                                raise ClaudeExecutorProtocolError(
                                    "Claude produced non-JSON trailing output"
                                ) from exc
                            if not isinstance(event, dict):
                                raise ClaudeExecutorProtocolError(
                                    "Claude trailing stream event must be an object"
                                )
                            events.append(event)
                        if process.returncode != 0:
                            stderr_file.seek(0, 2)
                            size = stderr_file.tell()
                            stderr_file.seek(max(0, size - 1000))
                            diagnostic = stderr_file.read().strip()
                            raise kb.ExecutorUnavailableError(
                                f"Claude exited with status {process.returncode}"
                                + (f": {diagnostic}" if diagnostic else "")
                            )
                        return events
            except BaseException:
                # Claude may itself have spawned tool children.  Signal the group
                # even if the leader already exited: otherwise a parent that exits
                # after spawning a tool can leave that tool alive on a protocol or
                # parser failure.  A vanished group is an expected race.
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                if process.poll() is None:
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        process.wait(timeout=5)
                raise


class ClaudeResumeExecutor:
    """One explicit Claude Code resume, protected by native Hermes gates.

    It cannot create a session, use ``--continue``, pick a model, route to a
    fallback, or schedule another pass.  It accepts only the canonical argv
    and JSONL already rendered by ``ClaudeCodeAdapter`` from a persisted
    dispatcher binding.
    """

    name = "claude-resume-executor"
    provider = kb.PROVIDER_CLAUDE_CODE

    def __init__(
        self,
        *,
        armed: bool = False,
        runner: ClaudeProcessRunner | None = None,
        heartbeat_interval_seconds: float = 30.0,
        claim_ttl_seconds: int = 300,
        workspace_root: Path | None = None,
    ) -> None:
        if isinstance(claim_ttl_seconds, bool) or not isinstance(claim_ttl_seconds, int) or claim_ttl_seconds <= 0:
            raise ValueError("claim_ttl_seconds must be a positive integer")
        if not isinstance(heartbeat_interval_seconds, (int, float)) or isinstance(heartbeat_interval_seconds, bool):
            raise ValueError("heartbeat_interval_seconds must be a finite number")
        if not math.isfinite(heartbeat_interval_seconds) or not (0 < heartbeat_interval_seconds <= claim_ttl_seconds / 3):
            raise ValueError(
                "heartbeat_interval_seconds must be finite and no greater than one third of claim_ttl_seconds"
            )
        self.armed = bool(armed)
        self.runner = runner or SubprocessClaudeRunner()
        self.heartbeat_interval_seconds = float(heartbeat_interval_seconds)
        self.claim_ttl_seconds = claim_ttl_seconds
        self.workspace_root = Path(workspace_root).resolve() if workspace_root is not None else None

    def execute(
        self,
        conn,
        *,
        request: kb.ResumeRequest,
        claimer: str,
        workspace: Path,
        policy: kb.ExecutorPolicy,
        now: int | None = None,
    ) -> kb.TerminalInterpretation:
        if not self.armed:
            raise ClaudeExecutorDisabled("Claude resume executor is disabled")
        if not isinstance(request, kb.ResumeRequest) or request.executed:
            raise ClaudeExecutorProtocolError("requires an unexecuted ResumeRequest")
        if not isinstance(policy, kb.ExecutorPolicy) or not policy.allow_real_execution:
            raise kb.ExecutionNotPermitted(
                "real Claude executor requires explicit allow_real_execution policy"
            )
        if request.plan.binding.provider != self.provider:
            raise ClaudeExecutorProtocolError("request is not a Claude Code binding")
        if request.fence.current_run_id is None:
            raise kb.ExecutionFenceLost("prepared fence has no live execution run")
        if self.workspace_root is None:
            raise kb.ExecutionNotPermitted(
                "armed Claude executor requires a declared task workspace root"
            )
        expected_workspace = (self.workspace_root / request.fence.task_id).resolve()
        actual_workspace = Path(workspace).resolve()
        if actual_workspace != expected_workspace:
            raise kb.ExecutionNotPermitted(
                "Claude executor workspace is not the declared task workspace"
            )
        stamp = int(time.time()) if now is None else int(now)
        kb.validate_binding_freshness(request.binding, now=stamp)
        policy.permit(conn, self, request.fence.task_id)
        if kb.a3_revocation_latched(conn, request.fence.task_id):
            raise kb.ExecutionNotPermitted("A3 revocation is latched")

        command = kb.ClaudeCodeAdapter().build_command(request.plan)
        if (
            command.argv != request.plan.command.argv
            or command.output_schema_json != request.plan.command.output_schema_json
            or command.timeout_seconds != request.plan.command.timeout_seconds
        ):
            raise ClaudeExecutorProtocolError("canonical Claude command rendering drifted")

        def renew() -> None:
            kb.require_claim_heartbeat(
                conn,
                request.fence.task_id,
                claimer=claimer,
                # The route decision identifies the *completed* antecedent
                # run.  The native fence identifies the task's current live
                # execution run, and that is the lease that must be renewed.
                expected_run_id=request.fence.current_run_id,
                ttl_seconds=self.claim_ttl_seconds,
            )

        def register_process(pid: int) -> None:
            kb.register_claim_process(
                conn,
                request.fence.task_id,
                claimer=claimer,
                expected_run_id=request.fence.current_run_id,
                pid=pid,
            )

        # Ownership is confirmed before launch, periodically while waiting,
        # and once more before a provider result can reach terminal handling.
        renew()
        try:
            events = self.runner.run(
                argv=command.argv,
                input_jsonl=command.input_jsonl,
                cwd=Path(workspace),
                timeout_seconds=command.timeout_seconds,
                heartbeat=renew,
                heartbeat_interval_seconds=self.heartbeat_interval_seconds,
                on_process_started=register_process,
            )
            renew()
            result = kb.parse_claude_stream_output(
                events,
                expected_run_id=request.fence.run_id,
                expected_session_id=request.binding.session_id,
            )
            receipt = kb.seal_adapter_result(
                conn,
                adapter=self,
                request=request,
                result=result,
                policy=policy,
                now=stamp,
            )
            return kb.interpret_terminal_result(conn, receipt=receipt, policy=policy)
        except (kb.ExecutorError, kb.ClaimLeaseLost):
            raise
        except Exception as exc:  # pragma: no cover - defensive edge conversion
            raise kb.ExecutorUnavailableError(f"Claude executor failed: {exc}") from exc
