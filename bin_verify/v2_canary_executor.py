#!/usr/bin/env python3
"""v2_canary_executor — canonical ClaudeResumeExecutor binding for the v2 gate.

Repaired per jarvis-os/t_4d09e0d9 review round 2. The prior revision still
contained a raw direct Claude bootstrap subprocess on the production green
path (real_bootstrap). That is GONE: this module contains NO provider
subprocess of any kind. The ONLY provider boundary in the entire v2 path is
ClaudeProcessRunner inside the armed canonical ClaudeResumeExecutor
(a gate-injected SubprocessClaudeRunner in production, a canned-event stub
in the no-provider suite). This module never constructs that real runner.

Because no session is ever created here, the session to resume MUST already
exist as a PRE-EXISTING persisted interactive session-binding artifact
(reservation/v2-interactive-session-binding.json), declared by the operator
for the reserved interactive subscription seat. load_session_binding() is the
explicit fail-closed gate over that artifact (missing / unparseable / wrong
kind / tampered fingerprint / expired / malformed all refuse) — the artifact
is validated at gate time (dispatch_gate_v2 G3c) AND re-validated here at
dispatch time. It is NEVER created by this code path.

Canonical lifecycle on the sole green --run path (v1 lineage t_4f843ed0,
minus any session-creating provider call):

  claim (handoff run) -> reclaim handoff -> worker session provenance
  (operator_declared, session id from the pre-existing artifact) -> persisted
  dispatcher-owned session binding -> completion fold -> CONTINUE route
  decision -> resume claim -> A3 grant -> prepare_resume_request ->
  ClaudeResumeExecutor.execute (armed; run-bound heartbeat renewal against
  the live claim; POST-LAUNCH A3 revocation latch re-checked at EVERY
  heartbeat; fence current_run_id; canonical `claude --resume <session>`
  re-render check; sealed terminal result + native terminal interpretation)
  -> session binding retired.

Failure at any point after the session binding is recorded retires that
binding (best effort, recorded) and removes the canary workspace; no sealed
terminal result can be accepted on the failure path.

Determinism/test seams: public dispatch_canary(runner=...) is StubRunner-only
and replays canned events from a stub dir. It can simulate adversarial
mid-run conditions (post-launch A3 revocation latch, claim loss) against a
FIXTURE board. The private gate-owned route receives the real runner only
from dispatch_gate_v2 after all gates and the O_EXCL lease have passed.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
BIN_VERIFY_ROOT = Path(__file__).resolve().parent
if str(BIN_VERIFY_ROOT) not in sys.path:
    sys.path.insert(0, str(BIN_VERIFY_ROOT))

from hermes_cli import kanban_db as kb  # noqa: E402
from hermes_cli.claude_executor import ClaudeResumeExecutor  # noqa: E402
import issue_cmux_claude_session_binding as cmux_binding  # noqa: E402

V2_MARKER = "V2_GOVERNED_CANARY_OK"
V2_INSTRUCTION = (
    f"Reply exactly {V2_MARKER}. This is the governed v2 control-plane no-op "
    "canary. Do not access files, tools, network resources, MCP servers, or "
    "external systems."
)
SESSION_BINDING_KIND = cmux_binding.BINDING_KIND
SESSION_BINDING_MAX_WINDOW_SECONDS = cmux_binding.MAX_BINDING_TTL_SECONDS


class DispatchError(RuntimeError):
    """Deterministic canonical-lifecycle failure (fail-closed, no retry)."""


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def artifact_fingerprint(rec: dict) -> str:
    clone = {k: v for k, v in rec.items() if k != "artifact_fingerprint"}
    return "sha256:" + hashlib.sha256(
        json.dumps(clone, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _retirement_path(path: Path) -> Path:
    return path.with_name(path.name + ".retired.json")


def load_session_binding(path, *, expected_task_id=None, board_db=None,
                         cmux_receipt_path=None, reservation_path=None,
                         issuer_path=None, now=None) -> dict:
    """Explicit fail-closed gate over the PRE-EXISTING persisted interactive
    session-binding artifact. This never creates, repairs, or refreshes a
    binding: any defect raises DispatchError and the caller must refuse."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if not path or not os.path.isfile(path):
        raise DispatchError(
            "pre-existing interactive session binding artifact is MISSING at "
            f"{path!r} — the v2 path never creates a session; the operator must "
            "declare the reserved interactive seat session first")
    try:
        rec = json.load(open(path, encoding="utf-8"))
    except ValueError:
        raise DispatchError("session binding artifact is not valid JSON — refuse")
    path = Path(path).resolve()
    if _retirement_path(path).exists():
        raise DispatchError("session binding artifact is retired; replay forbidden")
    if rec.get("binding_kind") != SESSION_BINDING_KIND:
        raise DispatchError(f"wrong binding_kind {rec.get('binding_kind')!r} — refuse")
    if rec.get("artifact_fingerprint") != cmux_binding.artifact_fingerprint(rec):
        raise DispatchError("session binding fingerprint mismatch (tampered/corrupt) — refuse")
    if rec.get("provider") != kb.PROVIDER_CLAUDE_CODE:
        raise DispatchError(f"session binding provider {rec.get('provider')!r} is not claude-code")
    session_id = rec.get("session_id")
    try:
        cmux_binding.require_identifier(session_id, "session id", cmux_binding.UUID_RE)
    except cmux_binding.Refuse as exc:
        raise DispatchError(str(exc)) from exc
    if not rec.get("declared_by"):
        raise DispatchError("session binding has no declared_by — refuse")
    try:
        issued = datetime.datetime.fromisoformat(rec["issued_at_utc"].replace("Z", "+00:00"))
        expires = datetime.datetime.fromisoformat(rec["expires_at_utc"].replace("Z", "+00:00"))
    except (KeyError, ValueError):
        raise DispatchError("session binding timestamps missing/invalid — refuse")
    if issued > now:
        raise DispatchError("session binding issued in the future — refuse")
    if expires <= now:
        raise DispatchError(f"session binding EXPIRED at {rec['expires_at_utc']} — refuse")
    if (expires - issued).total_seconds() > SESSION_BINDING_MAX_WINDOW_SECONDS:
        raise DispatchError("session binding validity window is implausibly long — refuse")
    if not all((expected_task_id, board_db, cmux_receipt_path, reservation_path, issuer_path)):
        raise DispatchError("CMUX-bound binding validation context missing — refuse")
    if rec.get("task_id") != expected_task_id:
        raise DispatchError("session binding task mismatch — refuse")
    if rec.get("board_sha256") != digest(str(Path(board_db).resolve())):
        raise DispatchError("session binding board identity mismatch — refuse")
    try:
        reservation = cmux_binding.load_json(Path(reservation_path), "seat reservation")
        receipt = cmux_binding.load_json(Path(cmux_receipt_path), "CMUX receipt")
        seat, _control, _ri, receipt_expires = cmux_binding.validate_contract(
            reservation=reservation, receipt=receipt, task_id=expected_task_id,
            session_id=session_id, now=now)
    except cmux_binding.Refuse as exc:
        raise DispatchError(str(exc)) from exc
    cmux = rec.get("cmux_seat")
    if not isinstance(cmux, dict) or cmux != {
        "workspace_id": seat["cmux_workspace_id"], "surface_id": seat["cmux_surface_id"],
        "daemon_version": seat["cmux_daemon_version"], "provider_session_uuid": seat["provider_session_uuid"],
    }:
        raise DispatchError("session binding CMUX seat mismatch — refuse")
    if rec.get("reservation_fingerprint") != reservation["reservation_fingerprint"]:
        raise DispatchError("session binding reservation fingerprint mismatch — refuse")
    mint = receipt["mint_control_context"]
    if rec.get("mint_control") != {
        "workspace_id": mint["workspace_id"],
        "surface_id": mint["surface_id"],
        "resolved_workspace_id": mint["resolved_workspace_id"],
        "resolved_surface_id": mint["resolved_surface_id"],
        "anchor_fingerprint": receipt["mint_control_anchor_fingerprint"],
    }:
        raise DispatchError("session binding mint-control anchor mismatch — refuse")
    mac = rec.get("mac_receipt")
    if not isinstance(mac, dict) or mac.get("receipt_fingerprint") != receipt["receipt_fingerprint"]:
        raise DispatchError("session binding receipt fingerprint mismatch — refuse")
    if expires > receipt_expires:
        raise DispatchError("session binding expiry exceeds CMUX receipt — refuse")
    if rec.get("issuer_source_sha256") != cmux_binding.sha256_file(Path(issuer_path)):
        raise DispatchError("session binding issuer fingerprint mismatch — refuse")
    return rec


def retire_session_binding_artifact(path, binding: dict) -> bool:
    """Permanently retire one immutable issued binding; O_EXCL prevents races."""
    target = _retirement_path(Path(path).resolve())
    payload = {"kind": "cmux-interactive-session-binding-retirement", "binding_fingerprint": binding["artifact_fingerprint"], "retired_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")}
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, sort_keys=True) + "\n"); fh.flush(); os.fsync(fh.fileno())
    return True


class StubRunner:
    """TEST ONLY: canned resume stream from <stub_dir>/resume.json.

    Exercises the REAL run-bound heartbeat path (each heartbeat() renews the
    live claim through kb.require_claim_heartbeat) and can simulate
    adversarial POST-LAUNCH conditions against the FIXTURE board named in its
    config: {"midrun": {"latch_a3": true}} durably latches an A3 revocation
    after launch; {"midrun": {"steal_claim": true}} makes another actor take
    the live claim. Both must abort the run with zero sealed terminal result.
    """

    def __init__(self, stub_dir: Path):
        self.stub_dir = Path(stub_dir)

    def _adversary(self, cfg):
        midrun = cfg.get("midrun") or {}
        if not midrun:
            return
        board = Path(cfg["board_db"])
        task_id = cfg["task_id"]
        adversary = kb.connect(board)
        try:
            if midrun.get("latch_a3"):
                kb.latch_a3_revocation(
                    adversary, task_id=task_id,
                    reason="test adversary: post-launch A3 revocation")
            if midrun.get("steal_claim"):
                with kb.write_txn(adversary):
                    adversary.execute(
                        "UPDATE tasks SET claim_lock='adversary-other-owner' WHERE id=?",
                        (task_id,))
        finally:
            adversary.close()

    def run(self, *, argv, input_jsonl, cwd, timeout_seconds, heartbeat,
            heartbeat_interval_seconds, env=None, on_process_started=None):
        cfg = json.loads((self.stub_dir / "resume.json").read_text())
        heartbeat()
        if on_process_started is not None:
            on_process_started(os.getpid())
        if cfg.get("delay"):
            time.sleep(float(cfg["delay"]))
        self._adversary(cfg)
        heartbeat()  # post-adversary: A3 latch / lost claim must abort HERE
        with open(self.stub_dir / "calls.log", "a") as fh:
            fh.write("resume " + json.dumps({"argv": list(argv), "hermes_home": None if env is None else env.get("HERMES_HOME")}) + "\n")
        return list(cfg["events"])


def _dispatch_gate_owned_canary(*, board_db, canary_task, workspace_root,
                    session_binding_path, cmux_receipt_path, reservation_path, issuer_path, hermes_home, runner,
                    instruction=V2_INSTRUCTION,
                    claimer_prefix="v2-governed-canary") -> dict:
    """Execute the one governed no-op canary through the canonical lifecycle.

    Returns the dispatch record dict on success; raises DispatchError (or the
    underlying kb error) on any failure — the caller (dispatch_gate_v2) has
    already consumed the one-shot lease, so a failure is terminal: no retry.
    """
    if runner is None:
        raise DispatchError("gate-owned executor route requires an explicit runner")
    board_db = Path(board_db)
    workspace_root = Path(workspace_root).resolve()
    hermes_home = Path(hermes_home).resolve() if hermes_home else None
    if hermes_home is None or not hermes_home.is_absolute() or not hermes_home.is_dir():
        raise DispatchError("governed canary requires an existing absolute HERMES_HOME; default-profile fallback is forbidden")
    heartbeat_events: list[int] = []
    record: dict = {
        "record_kind": "v2-dispatch-record",
        "executor": "ClaudeResumeExecutor",
        "provider_boundary": "ClaudeProcessRunner inside armed ClaudeResumeExecutor ONLY; "
                             "no bootstrap or other provider subprocess exists in this path",
        "canary_task": canary_task,
        "stubbed": runner is not None,
        "hermes_home": str(hermes_home),
        "profile_propagation": "explicit HERMES_HOME passed only to Claude child",
    }
    binding_artifact = None
    # Fail-closed session gate FIRST: no session is ever created here.
    binding_artifact = load_session_binding(session_binding_path, expected_task_id=canary_task,
        board_db=board_db, cmux_receipt_path=cmux_receipt_path,
        reservation_path=reservation_path, issuer_path=issuer_path)
    session_id = binding_artifact["session_id"]
    record["session_binding_gate"] = {
        "artifact": str(session_binding_path),
        "artifact_sha256": hashlib.sha256(
            Path(session_binding_path).read_bytes()).hexdigest(),
        "declared_by": binding_artifact["declared_by"],
        "session_sha256": digest(session_id),
        "created_here": False,
    }

    conn = kb.connect(board_db)
    workspace: Path | None = None
    handoff_run = None
    binding_recorded = False
    try:
        task = kb.get_task(conn, canary_task)
        if task is None or task.status != "blocked":
            raise DispatchError("canary task is not in the gate-verified blocked state")
        workspace = (workspace_root / canary_task).resolve()
        if workspace.parent != workspace_root or workspace.exists():
            raise DispatchError("canary workspace already exists or escapes the declared root")
        workspace.mkdir(parents=True)

        if not kb.unblock_task(conn, canary_task):
            raise DispatchError("could not unblock the canary task")
        if not kb.claim_task(conn, canary_task, claimer=f"{claimer_prefix}:handoff",
                             ttl_seconds=180):
            raise DispatchError("could not claim the canary task for session handoff")
        handoff_run = int(conn.execute(
            "SELECT current_run_id FROM tasks WHERE id=?", (canary_task,)
        ).fetchone()["current_run_id"])
        if not kb.reclaim_task(conn, canary_task, reason="v2 governed session handoff"):
            raise DispatchError("session handoff reclaim failed")
        kb.record_worker_session_provenance(
            conn, run_id=handoff_run, worker_session_id=session_id,
            source=kb.SESSION_SOURCE_OPERATOR,
        )
        now = int(time.time())
        kb.record_session_binding(
            conn, run_id=handoff_run, task_id=canary_task,
            provider=kb.PROVIDER_CLAUDE_CODE, session_id=session_id,
            source=kb.SESSION_SOURCE_OPERATOR, owner=kb.DISPATCHER_BINDING_OWNER,
            issued_at=now, expires_at=now + 600, now=now,
        )
        binding_recorded = True
        if not kb.record_worker_completion_event(conn, run_id=handoff_run):
            raise DispatchError("handoff completion was not folded for its exact run")
        completion = conn.execute(
            "SELECT payload FROM task_events WHERE run_id=? AND kind=?",
            (handoff_run, kb.BROKER_EVENT_WORKER_COMPLETION),
        ).fetchone()
        if completion is None:
            raise DispatchError("handoff completion event missing")
        decision = kb.decide_route(
            completion=json.loads(completion["payload"]),
            task_row=conn.execute("SELECT * FROM tasks WHERE id=?", (canary_task,)).fetchone(),
        )
        if decision.route != kb.ROUTE_CONTINUE:
            raise DispatchError(f"handoff did not produce a CONTINUE route: {decision.route}")
        if not kb.record_route_decision_event(conn, decision):
            raise DispatchError("route decision event was not recorded")

        if not kb.claim_task(conn, canary_task, claimer=f"{claimer_prefix}:resume",
                             ttl_seconds=180):
            raise DispatchError("could not claim the canary task for resume")
        kb.add_comment(conn, canary_task, "v2-governed-canary",
                       "A3_GATE=GRANTED; governed v2 fixed no-op canary only")
        request = kb.prepare_resume_request(
            conn, decision=decision, instruction=instruction,
            now=int(time.time()), timeout_seconds=120,
        )
        record["resume_argv"] = list(request.plan.command.argv)
        record["capsule_sha256"] = digest(request.plan.capsule.to_json())
        record["fence"] = {
            "handoff_run": handoff_run,
            "current_run_id": request.fence.current_run_id,
        }
        record["a3"] = {
            "revocation_latched_at_launch": bool(kb.a3_revocation_latched(conn, canary_task)),
            "grant_comment": "A3_GATE=GRANTED",
            "post_launch_enforcement": "revocation latch re-checked at every heartbeat; "
                                       "a latch aborts the run before any seal",
        }

        base_runner = runner

        class _GovernedHeartbeatRunner:
            """Wraps the run-bound heartbeat: every renewal FIRST re-checks the
            durable A3 revocation latch (post-launch veto), then renews the
            live claim. Either failing aborts the run before any seal."""

            def run(self, **kwargs):
                inner_heartbeat = kwargs["heartbeat"]

                def governed():
                    if kb.a3_revocation_latched(conn, canary_task):
                        raise kb.ExecutionNotPermitted(
                            "A3 revocation latched post-launch — aborting run "
                            "before any terminal result can be sealed")
                    inner_heartbeat()
                    heartbeat_events.append(int(time.time()))

                kwargs["heartbeat"] = governed
                return base_runner.run(**kwargs)

        executor = ClaudeResumeExecutor(
            armed=True, runner=_GovernedHeartbeatRunner(),
            heartbeat_interval_seconds=20, claim_ttl_seconds=180,
            workspace_root=workspace_root,
            hermes_home=hermes_home,
            require_explicit_hermes_home=True,
        )
        outcome = executor.execute(
            conn, request=request, claimer=f"{claimer_prefix}:resume",
            workspace=workspace, policy=kb.ExecutorPolicy(allow_real_execution=True),
            now=int(time.time()),
        )
        terminal = kb.get_task(conn, canary_task)
        record["terminal"] = {
            "outcome_status": outcome.status,
            "terminal_write": bool(outcome.terminal_write),
            "task_status": None if terminal is None else terminal.status,
            "summary_has_marker": V2_MARKER in str(outcome.summary),
        }
        record["heartbeats"] = {
            "runner_heartbeat_calls": len(heartbeat_events),
            "note": "each call re-checked the A3 latch then renewed the live claim "
                    "via kb.require_claim_heartbeat bound to fence current_run_id; "
                    "a latch or lost lease raises and aborts",
        }
        if outcome.status != "completed" or not outcome.terminal_write \
                or terminal is None or terminal.status != "done" \
                or V2_MARKER not in str(outcome.summary):
            raise DispatchError("canary did not reach the fixed guarded terminal result")
        if len(heartbeat_events) < 2:
            raise DispatchError("run-bound heartbeats were not exercised")
        if not kb.retire_session_binding(conn, run_id=handoff_run):
            raise DispatchError("canary session binding was not retired")
        record["binding_retired"] = True
        record["status"] = "DISPATCHED-ONCE"
        return record
    except BaseException as exc:
        # Failure-path cleanup: never leave a live recorded binding behind a
        # failed dispatch. Retirement is best-effort and recorded; the sealed
        # terminal path was never reached (executor raised before/at seal).
        if binding_recorded and handoff_run is not None:
            try:
                record["binding_retired"] = bool(
                    kb.retire_session_binding(conn, run_id=handoff_run))
            except Exception:
                record["binding_retired"] = False
        try:
            exc.v2_record = record  # same dict the finally block finishes updating
        except Exception:
            pass
        raise
    finally:
        if workspace is not None and workspace.exists():
            shutil.rmtree(workspace)
        record["workspace_removed"] = workspace is None or not workspace.exists()
        if binding_artifact is not None:
            record["binding_artifact_retired"] = retire_session_binding_artifact(session_binding_path, binding_artifact)
        conn.close()


def dispatch_canary(*, board_db, canary_task, workspace_root,
                    session_binding_path, cmux_receipt_path, reservation_path,
                    issuer_path, hermes_home, runner=None, instruction=V2_INSTRUCTION,
                    claimer_prefix="v2-governed-canary") -> dict:
    """Public deterministic fixture API; never a real-provider entry point.

    Real Claude execution is deliberately not reachable through this public
    helper. The only production route is the private gate-owned entry point
    called by dispatch_gate_v2 after G1-G6 and the O_EXCL lease have passed.
    """
    if not isinstance(runner, StubRunner):
        raise DispatchError(
            "public dispatch_canary is fixture-only; an explicit StubRunner is required "
            "and real provider runners are refused")
    return _dispatch_gate_owned_canary(
        board_db=board_db, canary_task=canary_task, workspace_root=workspace_root,
        session_binding_path=session_binding_path, cmux_receipt_path=cmux_receipt_path,
        reservation_path=reservation_path, issuer_path=issuer_path,
        hermes_home=hermes_home, runner=runner, instruction=instruction,
        claimer_prefix=claimer_prefix)
