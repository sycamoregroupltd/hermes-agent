#!/usr/bin/env python3
"""dispatch_gate_v2 — governed Claude worker v2 canary dispatch gate.

v2 is SEPARATELY one-shot and cannot invoke Claude unless EVERY gate passes:

  G1  the current A2 activation-packet verifier exits 0 with all checks green
  G2  a separate exact A2 DISPATCH comment exists on the packet card
      (strict grammar: APPROVAL A2-DISPATCH by the orchestrator naming the v2
      canary task id, provider claude-code, the interactive subscription seat,
      the no-op scope, and a cancellation reference) — exactly one such
      comment, exact author, all fields present
  G3  fresh CMUX conditions: seat reservation writer gate (t_d7e6c034) exits 0
      NOW, and a MAC-MINTED task-bound CMUX reservation receipt validates on
      DGX (workspace/surface/task/expiry/fingerprint). The CMUX control
      socket/CLI lives on the Mac; this gate executes on DGX and therefore
      NEVER calls a DGX-local ~/.cmux — freshness travels as a short-lived
      receipt minted at the control socket (bin_verify/mint_cmux_receipt.py,
      run on the Mac control terminal), validated here fail-closed
  G4  no existing v2 lease — the lease file is minted only by a successful
      --run and is NON-REUSABLE: its mere existence refuses any further run
  G5  stop switch absent; v2 canary card exists, blocked, unassigned, 0 runs
  G6  packet structural caps re-read (one claude-code worker, one run,
      max_retries 0)

Default invocation is a DRY-RUN that always REFUSES to dispatch (rc=5) and
reports each gate. Even with every gate green, dispatch requires the explicit
--run flag. --run re-evaluates all gates, atomically mints+consumes the
one-shot lease (losing the O_EXCL race to a concurrent dispatcher is a
DETERMINISTIC refusal, never an uncaught EEXIST — t_4d09e0d9), and only then
dispatches EXACTLY ONCE through the canonical ClaudeResumeExecutor/task-run
lifecycle (bin_verify/v2_canary_executor.py). There is NO provider subprocess
of any kind outside the armed executor's ClaudeProcessRunner boundary — in
particular no session-creating bootstrap: the session to resume must ALREADY
exist as a persisted interactive session-binding artifact, gated fail-closed
here (G3c) and re-validated at dispatch time (never created). The lifecycle:
session handoff run, operator-declared provenance, persisted dispatcher-owned
binding, completion fold + CONTINUE route, resume via the armed executor with
run-bound heartbeat renewal, POST-LAUNCH A3 revocation latch re-checked at
every heartbeat, terminal fence, sealed native terminal result, binding
retirement (also on the failure path). Any absent, stale, or mismatched gate
refuses — fail closed, no retry, no fallback.

v1 (t_4f843ed0 receipt lineage, evidence/controlled-claude-worker-activation-
receipt.json) is historical evidence only and is never touched or rerun.

Test seams (no-provider tests only): --board-db, --lease-file, --stop-file,
--packet-verifier, --reservation-tool, --cmux-receipt, --packet-card,
--workspace-root, --stub-events-dir. Production defaults point at the real
artifacts, and the no-provider suite always injects canned-event stubs at the
two provider boundaries.
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

BIN_DIR = os.path.dirname(os.path.abspath(__file__))
if BIN_DIR not in sys.path:
    sys.path.insert(0, BIN_DIR)
import cmux_dual_anchor_contract as dual_anchor
import verify_activation_packet as activation_verifier
import v2_grant_authority as grant_authority

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_BOARD_DB = "/home/frank/.hermes/kanban/boards/jarvis-os/kanban.db"
PACKET_CARD = "t_0119603b"
DISPATCH_AUTHOR = "jarvis-orchestrator"
DEFAULT_LEASE = os.path.join(ROOT, "reservation", "v2-dispatch-lease.json")
DEFAULT_WORKSPACE_ROOT = "/home/frank/.hermes/controlled-worker-activation"
# Historical unnamed invocations use this only as a compatibility default.
# A named production canary derives a task-local stop control below.
DEFAULT_STOP = os.path.join(ROOT, "ACTIVATION-STOP")
DEFAULT_VERIFIER = os.path.join(ROOT, "bin_verify", "verify_activation_packet.py")
DEFAULT_RESERVATION_TOOL = ("/home/frank/.hermes/kanban/boards/jarvis-os/workspaces/"
                            "t_d7e6c034/bin/seat_reservation.py")
DEFAULT_RESERVATION_JSON = ("/home/frank/.hermes/kanban/boards/jarvis-os/workspaces/"
                            "t_d7e6c034/reservation/seat-reservation.json")
DEFAULT_CMUX_RECEIPT = os.path.join(ROOT, "reservation", "cmux-reservation-receipt.json")
DEFAULT_SESSION_BINDING = os.path.join(ROOT, "reservation", "cmux-interactive-session-binding.json")
DEFAULT_BINDING_ISSUER = os.path.join(ROOT, "bin_verify", "issue_cmux_claude_session_binding.py")

# Maximum allowed receipt validity window: freshness must be minted-at-source,
# short-lived, and non-extendable. A receipt with a longer window is refused.
RECEIPT_MAX_WINDOW_SECONDS = 600

RC_OK = 0
RC_REFUSE = 5
OUTCOME_POLL_SECONDS = 15


def observe_executor_outcome(authority_socket, grant_id, consume_receipt, *, timeout_seconds=OUTCOME_POLL_SECONDS):
    """Bounded gate-side readback; launcher acceptance is never completion.

    The authority is the sole writer/reader of the consume/outcome pair.  The
    gate accepts only the exact receipt it was issued and a single authenticated
    terminal success tied to that receipt, task and reviewed source head.
    """
    if not isinstance(consume_receipt, dict) or not isinstance(consume_receipt.get("receipt_fingerprint"), str):
        raise RuntimeError("authority did not return a canonical consume receipt")
    deadline = time.monotonic() + max(0, min(int(timeout_seconds), OUTCOME_POLL_SECONDS))
    while True:
        reply = grant_authority.request(authority_socket, {"op": "read_outcome", "grant_id": grant_id,
            "consume_receipt_fingerprint": consume_receipt["receipt_fingerprint"]})
        if reply.get("consume_receipt") != consume_receipt:
            raise RuntimeError("authority outcome readback receipt mismatch")
        if not reply.get("pending"):
            outcome = reply.get("outcome")
            required = {"outcome_kind":"v2-executor-terminal-outcome", "schema_version":1,
                        "grant_id":grant_id, "consume_receipt_fingerprint":consume_receipt["receipt_fingerprint"],
                        "status":"completed", "task_id":consume_receipt["task_id"],
                        "source_head":consume_receipt["source_head"],
                        "terminal":{"guarded_lifecycle_done":True,"terminal_write":True,"marker":True}}
            if outcome != required:
                raise RuntimeError("executor terminal outcome missing, mismatched, or non-success")
            return outcome
        if time.monotonic() >= deadline:
            raise RuntimeError("timed out waiting for matching executor terminal outcome")
        time.sleep(0.05)


def task_scoped_lease_path(canary_task, activation_packet, stub_events_dir=None):
    """Return the non-reusable lease namespace for one named canary.

    A named canary may never inherit the historical/global lease. The packet
    binds the requested task to its declared task id; the namespace itself
    remains inside this reviewed source tree rather than a packet-controlled
    arbitrary output path.
    """
    if not isinstance(canary_task, str) or not re.fullmatch(r"t_[0-9a-f]{8}", canary_task):
        raise ValueError("named canary task id is invalid")
    try:
        packet = json.load(open(activation_packet, encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"activation packet unreadable for task-scoped lease: {exc}") from exc
    if packet.get("task_id") != canary_task and stub_events_dir is None:
        raise ValueError("activation packet task_id does not match --canary-task")
    if stub_events_dir is not None:
        # StubRunner is a test-only executor boundary: it cannot start Claude.
        # Keep its fixture lease inside the fixture directory, never a caller
        # supplied production path.
        return os.path.join(os.path.realpath(stub_events_dir), ".test-only-v2-dispatch-lease.json")
    return os.path.join(ROOT, "reservation", "task-artifacts", canary_task,
                        "v2-dispatch-lease.json")


def task_scoped_stop_path(canary_task):
    """Return the sole stop control for a named production canary.

    This is deliberately derived from the task identifier rather than packet
    input or a CLI-selected path. A caller must not be able to make one
    canary ignore another task's stop control (or select an unrelated empty
    file). The control is checked by both dry and armed invocations.
    """
    if not isinstance(canary_task, str) or not re.fullmatch(r"t_[0-9a-f]{8}", canary_task):
        raise ValueError("named canary task id is invalid")
    return os.path.join(ROOT, "reservation", "task-artifacts", canary_task,
                        "ACTIVATION-STOP")


def receipt_fingerprint(rec):
    return dual_anchor.receipt_fingerprint(rec)


def validate_cmux_receipt(receipt_path, reservation_json, canary_task, now_utc=None):
    """Validate the Mac-minted, task-bound CMUX reservation receipt on DGX.

    Fail-closed: returns (ok, detail). NEVER touches any cmux socket."""
    now = now_utc or datetime.datetime.now(datetime.timezone.utc)
    if not os.path.isfile(receipt_path):
        return False, "receipt missing — mint on the Mac control terminal (mint_cmux_receipt.py)"
    try:
        rec = json.load(open(receipt_path, encoding="utf-8"))
    except ValueError:
        return False, "receipt unparseable — refuse"
    try:
        res = json.load(open(reservation_json, encoding="utf-8"))
    except (OSError, ValueError):
        return False, "seat reservation record unreadable — refuse"
    try:
        dual_anchor.validate_receipt(rec, res, task_id=canary_task, now=now)
    except dual_anchor.ContractRefuse as exc:
        detail = str(exc)
        if "expired" in detail:
            detail = "receipt STALE — re-mint on the Mac"
        return False, detail + " — refuse"
    return True, ""

# Strict dispatch-comment grammar. All groups mandatory.
DISPATCH_RE = re.compile(
    r"^APPROVAL A2-DISPATCH(?:\s|$)"
    r"(?=.*\bby=jarvis-orchestrator\b)"
    r"(?=.*\bcanary_task=(t_[0-9a-f]{8})\b)"
    r"(?=.*\bprovider=claude-code\b)"
    r"(?=.*\bseat=interactive-subscription\b)"
    r"(?=.*\bscope=no-op\b)"
    r"(?=.*\bcancellation=)",
    re.DOTALL)


def gate_result(gates, name, ok, detail=""):
    gates.append({"gate": name, "pass": bool(ok), "detail": detail if not ok else ""})
    return ok


def evaluate_gates(args):
    gates = []

    # G1: packet verifier fully green.
    try:
        verifier_argv = [sys.executable, args.packet_verifier, "--json"]
        if args.canary_task:
            verifier_argv.extend(["--activation-packet", args.activation_packet,
                                  "--task-id", args.canary_task])
        p = subprocess.run(verifier_argv,
                           capture_output=True, text=True, timeout=120)
        v = json.loads(p.stdout) if p.stdout.strip() else {}
        g1 = (p.returncode == 0 and v.get("verdict") == "ACTIVATION-PREREQUISITES-MET"
              and v.get("passed") == v.get("total") and v.get("total", 0) >= 21)
        gate_result(gates, "G1 packet verifier fully green", g1,
                    f"rc={p.returncode} verdict={v.get('verdict')} {v.get('passed')}/{v.get('total')}")
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        gate_result(gates, "G1 packet verifier fully green", False, repr(e))

    # G2: exact A2 dispatch comment (strict grammar, exactly one, exact author).
    dispatch_task = None
    try:
        con = sqlite3.connect(f"file:{args.board_db}?mode=ro", uri=True)
        rows = con.execute(
            "select id, author, body from task_comments where task_id=? "
            "and body like 'APPROVAL A2-DISPATCH%' order by id", (args.packet_card,)).fetchall()
        matches = []
        for cid, author, body in rows:
            fields = activation_verifier.parse_a2_dispatch_fields(
                body, require_packet_pins=False)
            if fields and author == DISPATCH_AUTHOR:
                matches.append((cid, fields["canary_task"], hashlib.sha256(body.encode()).hexdigest()))

        # A supplied canary task narrows approval to that exact disposable
        # task. Historical compliant comments for spent/different canaries
        # remain audit evidence, not a second approval for this task. Duplicate
        # comments naming this same requested task stay ambiguous and refuse.
        relevant = matches if args.canary_task is None else [
            item for item in matches if item[1] == args.canary_task
        ]
        if len(relevant) == 1:
            cid, dispatch_task, body_sha = relevant[0]
            g2 = True
            detail = ""
        elif not relevant:
            g2 = False
            detail = ("no exact A2 dispatch comment naming the requested canary task"
                      if args.canary_task else
                      "no exact A2 dispatch comment present — dispatch not approved")
        else:
            g2 = False
            detail = (f"{len(relevant)} matching dispatch comments for requested task "
                      f"{args.canary_task} — ambiguous, refuse")
        gate_result(gates, "G2 exact A2 dispatch comment (grammar+author, exactly one)", g2, detail)
        if g2:
            gate_result(gates, "G2b dispatch comment names the requested canary task",
                        args.canary_task is None or dispatch_task == args.canary_task)
    except sqlite3.Error as e:
        gate_result(gates, "G2 exact A2 dispatch comment (grammar+author, exactly one)", False, repr(e))
        con = None
    finally:
        try:
            con.close()
        except Exception:
            pass

    # G3: fresh reservation gate + live CMUX workspace match (resolved NOW).
    try:
        p = subprocess.run([sys.executable, args.reservation_tool, "validate"],
                           capture_output=True, text=True, timeout=60)
        gate_result(gates, "G3a seat reservation writer gate open (fresh)", p.returncode == 0,
                    f"rc={p.returncode}")
    except (OSError, subprocess.TimeoutExpired) as e:
        gate_result(gates, "G3a seat reservation writer gate open (fresh)", False, repr(e))
    ok, detail = validate_cmux_receipt(args.cmux_receipt, args.reservation_json,
                                       args.canary_task)
    gate_result(gates, "G3b Mac-minted CMUX reservation receipt valid (fresh/bound/untampered)",
                ok, detail)

    # G3c: the session to resume must ALREADY exist as a persisted interactive
    # session-binding artifact. The v2 path never creates a session — absence
    # or any defect fails closed here and again at dispatch time.
    try:
        import v2_canary_executor as v2ce
        v2ce.load_session_binding(args.session_binding, expected_task_id=args.canary_task,
                                  board_db=args.board_db, cmux_receipt_path=args.cmux_receipt,
                                  reservation_path=args.reservation_json,
                                  issuer_path=args.binding_issuer)
        gate_result(gates, "G3c pre-existing interactive session binding valid (never created here)",
                    True)
    except Exception as e:
        gate_result(gates, "G3c pre-existing interactive session binding valid (never created here)",
                    False, str(e)[:300])

    # G3d: governed provider child must carry an explicit, existing profile
    # home; an unset value would silently fall back to the global default.
    gate_result(gates, "G3d explicit HERMES_HOME profile directory",
                isinstance(args.hermes_home, str) and os.path.isabs(args.hermes_home)
                and os.path.isdir(args.hermes_home),
                "missing/non-absolute/nonexistent HERMES_HOME — default-profile fallback forbidden")

    # G4: non-reusable lease — existence refuses.
    gate_result(gates, "G4 no existing v2 lease (one-shot, non-reusable)",
                not os.path.exists(args.lease_file),
                f"lease exists at {args.lease_file} — v2 already consumed; no reuse, no retry")

    # G5: stop switch + canary card state.
    gate_result(gates, "G5a stop switch absent", not os.path.exists(args.stop_file),
                "stop file present — dispatch forbidden")
    if args.canary_task:
        try:
            con = sqlite3.connect(f"file:{args.board_db}?mode=ro", uri=True)
            row = con.execute("select status, assignee from tasks where id=?",
                              (args.canary_task,)).fetchone()
            runs = con.execute("select count(*) from task_runs where task_id=?",
                               (args.canary_task,)).fetchone()[0]
            con.close()
            gate_result(gates, "G5b canary card blocked/unassigned/0-runs",
                        row == ("blocked", None) and runs == 0, f"row={row} runs={runs}")
        except sqlite3.Error as e:
            gate_result(gates, "G5b canary card blocked/unassigned/0-runs", False, repr(e))
    else:
        gate_result(gates, "G5b canary card blocked/unassigned/0-runs", False,
                    "no --canary-task provided — nothing to dispatch, refuse")

    # G6: packet structural caps re-read.
    try:
        pkt = json.load(open(args.activation_packet, encoding="utf-8"))
        gate_result(gates, "G6 packet caps: one claude-code worker, one run, no retry",
                    pkt["worker"]["count_exactly"] == 1
                    and pkt["worker"]["provider"] == "claude-code"
                    and pkt["caps"]["one_run_only"] is True
                    and pkt["caps"]["max_retries"] == 0)
    except (OSError, ValueError, KeyError) as e:
        gate_result(gates, "G6 packet caps: one claude-code worker, one run, no retry", False, repr(e))

    return gates, dispatch_task


def mint_lease(lease_file, canary_task):
    """Atomically mint the one-shot lease. Returns False on contention (EEXIST)
    instead of raising: a concurrent runner losing the O_EXCL race must get a
    deterministic REFUSE, never an uncaught traceback (t_4d09e0d9)."""
    lease = {
        "lease_kind": "v2-dispatch-lease", "one_shot": True, "reusable": False,
        "canary_task": canary_task,
        "minted_at_utc": datetime.datetime.now(datetime.timezone.utc)
            .isoformat().replace("+00:00", "Z"),
        "consumed": True,
        "note": "existence of this file refuses all future v2 runs (G4); no retry",
    }
    os.makedirs(os.path.dirname(lease_file), exist_ok=True)
    try:
        fd = os.open(lease_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(lease, fh, indent=2, sort_keys=True)
    return True


def main(argv):
    ap = argparse.ArgumentParser(prog="dispatch_gate_v2")
    ap.add_argument("--run", action="store_true",
                    help="EXPLICIT dispatch. Without it this is a dry-run that always refuses.")
    ap.add_argument("--canary-task", default=None)
    ap.add_argument("--board-db", default=DEFAULT_BOARD_DB)
    ap.add_argument("--lease-file", default=None,
                    help="legacy unnamed-task seam; named tasks always derive their lease")
    ap.add_argument("--stop-file", default=None,
                    help="legacy/stub seam; named production canaries derive their stop control")
    ap.add_argument("--packet-verifier", default=DEFAULT_VERIFIER)
    ap.add_argument("--reservation-tool", default=DEFAULT_RESERVATION_TOOL)
    ap.add_argument("--reservation-json", default=None)
    ap.add_argument("--workspace-root", default=None,
                    help="declared root under which the canary task workspace is created")
    ap.add_argument("--stub-events-dir", default=None,
                    help="TEST ONLY: canned bootstrap/resume streams for the canonical "
                         "executor harness; production leaves this unset")
    ap.add_argument("--packet-card", default=None,
                    help="packet card holding the A2 dispatch comment (test seam)")
    ap.add_argument("--activation-packet", default=None,
                    help="activation packet whose structural caps are re-read (test seam)")
    ap.add_argument("--session-binding", default=None,
                    help="PRE-EXISTING persisted interactive session-binding artifact "
                         "(G3c); the v2 path never creates a session")
    ap.add_argument("--binding-issuer", default=DEFAULT_BINDING_ISSUER,
                    help="reviewed CMUX binding issuer whose exact source hash is pinned by the artifact")
    ap.add_argument("--hermes-home", default=None,
                    help="required existing absolute Hermes profile directory passed explicitly to Claude child")
    ap.add_argument("--cmux-receipt", default=None,
                    help="Mac-minted task-bound CMUX reservation receipt (validated on DGX)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--grant-authority-socket", default=os.environ.get("HERMES_GRANT_AUTHORITY_SOCKET"), help="production verifier-owned grant authority socket")
    ap.add_argument("--grant-issuer-secret-file", default=os.environ.get("HERMES_GRANT_ISSUER_SECRET_FILE"), help="production gate-only issuer token file")
    ap.add_argument("--grant-install-config", default=os.environ.get("HERMES_GRANT_INSTALL_CONFIG"), help="production authority install configuration")
    ap.add_argument("--outcome-timeout-seconds", type=int, default=OUTCOME_POLL_SECONDS,
                    help="bounded real-run outcome observation; values are capped fail-closed")
    args = ap.parse_args(argv)

    # Real runner derives the task packet itself; callers cannot select it.
    if args.canary_task and args.stub_events_dir is None:
        supplied_task_args = {k: getattr(args, k) for k in ("reservation_json", "cmux_receipt", "session_binding", "workspace_root", "packet_card", "activation_packet", "stop_file") if getattr(args, k) is not None}
        if supplied_task_args:
            report = {"verdict":"REFUSE", "run_flag":args.run, "all_gates_green":False,
                      "gates":[{"gate":"G0 real-run canonical-input boundary", "pass":False,
                                "detail":"explicit task-bound override: " + ",".join(sorted(supplied_task_args))}], "rc":RC_REFUSE}
            print(json.dumps(report, indent=2, sort_keys=True) if args.json else "REFUSE: real-run canonical-input boundary")
            return RC_REFUSE
        task_artifacts = os.path.join(ROOT, "reservation", "task-artifacts", args.canary_task)
        args.activation_packet = os.path.join(task_artifacts, f"ACTIVATION-PACKET-{args.canary_task}.json")
        args.reservation_json = os.path.join("/home/frank/.hermes/kanban/boards/jarvis-os/workspaces", args.canary_task, "reservation", "seat-reservation.json")
        args.cmux_receipt = os.path.join(task_artifacts, "cmux-reservation-receipt.json")
        args.session_binding = os.path.join(task_artifacts, "cmux-interactive-session-binding.json")
        args.packet_card = args.canary_task
        args.workspace_root = os.path.join(ROOT, "reservation", "task-workspaces", args.canary_task)
        args.stop_file = task_scoped_stop_path(args.canary_task)
        canonical = {"packet_verifier": DEFAULT_VERIFIER, "board_db": DEFAULT_BOARD_DB,
            "reservation_tool": DEFAULT_RESERVATION_TOOL, "reservation_json": args.reservation_json,
            "cmux_receipt": args.cmux_receipt, "session_binding": args.session_binding,
            "binding_issuer": DEFAULT_BINDING_ISSUER, "workspace_root": args.workspace_root,
            "packet_card": args.packet_card}
        alternate = [k for k, v in canonical.items() if getattr(args, k) != v]
        if alternate:
            report = {"verdict":"REFUSE", "run_flag":args.run, "all_gates_green":False,
                      "gates":[{"gate":"G0 real-run canonical-input boundary", "pass":False,
                                "detail":"noncanonical override: " + ",".join(alternate)}], "rc":RC_REFUSE}
            print(json.dumps(report, indent=2, sort_keys=True) if args.json else "REFUSE: real-run canonical-input boundary")
            return RC_REFUSE

    if args.stub_events_dir is not None or not args.canary_task:
        # Fixture/legacy mode only: restore omitted historical defaults. StubRunner
        # cannot reach ClaudeProcessRunner; real named mode never enters here.
        args.reservation_json = args.reservation_json or DEFAULT_RESERVATION_JSON
        args.cmux_receipt = args.cmux_receipt or DEFAULT_CMUX_RECEIPT
        args.session_binding = args.session_binding or DEFAULT_SESSION_BINDING
        args.workspace_root = args.workspace_root or DEFAULT_WORKSPACE_ROOT
        args.packet_card = args.packet_card or PACKET_CARD
        args.activation_packet = args.activation_packet or os.path.join(ROOT, "ACTIVATION-PACKET-CLAUDE-WORKER.json")
        args.stop_file = args.stop_file or DEFAULT_STOP

    # Preserve the historical global default only for the legacy unnamed path.
    # Named canaries never accept a caller-supplied lease. Test fixture runs
    # derive a private lease only when StubRunner is selected; that branch is
    # structurally incapable of reaching ClaudeProcessRunner.
    if args.canary_task and args.lease_file is not None:
        report = {"verdict": "REFUSE", "run_flag": args.run, "all_gates_green": False,
                  "gates": [{"gate": "G0 task-scoped lease authority", "pass": False,
                             "detail": "named canary may not override its derived lease path"}], "rc": RC_REFUSE}
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print("FAIL: G0 task-scoped lease authority — named canary may not override its derived lease path")
            print("\nVERDICT: REFUSE")
        return RC_REFUSE
    if args.lease_file is None:
        try:
            args.lease_file = (task_scoped_lease_path(args.canary_task, args.activation_packet,
                                                       args.stub_events_dir)
                               if args.canary_task else DEFAULT_LEASE)
        except ValueError as exc:
            report = {"verdict": "REFUSE", "run_flag": args.run, "all_gates_green": False,
                      "gates": [{"gate": "G0 task-scoped packet/lease binding", "pass": False,
                                 "detail": str(exc)}], "rc": RC_REFUSE}
            if args.json:
                print(json.dumps(report, indent=2, sort_keys=True))
            else:
                print("FAIL: G0 task-scoped packet/lease binding — " + str(exc))
                print("\nVERDICT: REFUSE")
            return RC_REFUSE

    gates, dispatch_task = evaluate_gates(args)
    all_green = all(g["pass"] for g in gates)

    if not args.run:
        verdict = "WOULD-DISPATCH (dry-run: --run required)" if all_green else "REFUSE"
        rc = RC_REFUSE  # a dry-run NEVER authorizes and never mints a lease
    elif not all_green:
        verdict, rc = "REFUSE", RC_REFUSE
    elif not mint_lease(args.lease_file, dispatch_task):
        # A concurrent runner won the O_EXCL race between gate evaluation and
        # minting: deterministic refusal, no provider touched, no retry.
        gate_result(gates, "G4r one-shot lease race lost (concurrent dispatcher)", False,
                    f"lease appeared at {args.lease_file} after gate evaluation — "
                    "another dispatcher won the one-shot lease; refuse, no retry")
        verdict, rc = "REFUSE (lease contention)", RC_REFUSE
    else:
        # Lease consumed: the fixture remains in-process; the real provider
        # route is a separate child, authority-gated over an inherited private
        # FD.  No provider class is imported or constructed in this gate.
        import v2_canary_executor as v2ce
        rec_path = os.path.join(ROOT, "evidence", "v2-dispatch-record.json")
        try:
            if args.stub_events_dir:
                record = v2ce.dispatch_canary(
                    board_db=args.board_db, canary_task=dispatch_task,
                    workspace_root=args.workspace_root,
                    session_binding_path=args.session_binding,
                    cmux_receipt_path=args.cmux_receipt,
                    reservation_path=args.reservation_json,
                    issuer_path=args.binding_issuer,
                    hermes_home=args.hermes_home,
                    runner=v2ce.StubRunner(args.stub_events_dir))
            else:
                if not args.grant_authority_socket or not args.grant_issuer_secret_file or not args.grant_install_config:
                    raise RuntimeError("real dispatch requires authority socket, gate-only issuer token and verified install configuration")
                install=grant_authority.verify_install(args.grant_install_config)
                if os.geteuid()!=install["gate_uid"]:
                    raise RuntimeError("real dispatch gate must run as configured gate UID")
                issuer_secret = Path(args.grant_issuer_secret_file).read_text(encoding="utf-8").strip()
                if len(issuer_secret) < 32:
                    raise RuntimeError("gate issuer token is missing or too short")
                envelope = {
                    "task_id": dispatch_task,
                    "board_db": str(Path(args.board_db).resolve()),
                    "workspace_root": str(Path(args.workspace_root).resolve()),
                    "session_binding": str(Path(args.session_binding).resolve()),
                    "cmux_receipt": str(Path(args.cmux_receipt).resolve()),
                    "reservation_json": str(Path(args.reservation_json).resolve()),
                    "binding_issuer": str(Path(args.binding_issuer).resolve()),
                    "hermes_home": str(Path(args.hermes_home).resolve()),
                    "lease_file": str(Path(args.lease_file).resolve()),
                    "lease_realpath": str(Path(args.lease_file).resolve()),
                    "lease_sha256": hashlib.sha256(Path(args.lease_file).read_bytes()).hexdigest(),
                    "source_head": subprocess.check_output(["git", "-C", ROOT, "rev-parse", "HEAD"], text=True).strip(),
                    "expires_at": int(time.time()) + 30,
                }
                issued = grant_authority.request(args.grant_authority_socket,
                    {"op": "issue", "issuer_token": issuer_secret, "grant": envelope})
                grant_id = issued["grant_id"]
                # Arming is separate from issuing. Only the configured root-owned
                # narrow launcher can start `hermes-real-executor@<grant>` as the
                # non-login executor UID; the gate never execs the child itself.
                grant_authority.request(args.grant_authority_socket,{"op":"arm","grant_id":grant_id})
                launch=subprocess.run([install["launcher"],grant_id],text=True,capture_output=True,timeout=30)
                if launch.returncode:
                    raise RuntimeError("executor launcher refused: "+launch.stderr[-300:])
                # A successful launcher only means systemd accepted the unit.
                # It is not a terminal executor result and MUST NOT produce
                # RC_OK until the authority returns the grant-bound outcome.
                consumed = grant_authority.request(args.grant_authority_socket,
                    {"op":"read_outcome","grant_id":grant_id,
                     "consume_receipt_fingerprint": issued["consume_receipt"]["receipt_fingerprint"]})
                receipt = consumed.get("consume_receipt")
                if receipt != issued.get("consume_receipt"):
                    raise RuntimeError("launcher did not yield matching authority consume receipt")
                outcome = observe_executor_outcome(args.grant_authority_socket, grant_id, receipt,
                    timeout_seconds=args.outcome_timeout_seconds)
                record={"record_kind":"v2-dispatch-record","canary_task":dispatch_task,
                        "executor":"ClaudeResumeExecutor","status":"DISPATCHED-ONCE",
                        "grant_id":grant_id,"launcher":install["launcher"],
                        "consume_receipt_fingerprint":receipt["receipt_fingerprint"],"terminal_outcome":outcome}
            record["lease_file"] = args.lease_file
            verdict, rc = "DISPATCHED-ONCE", RC_OK
        except Exception as exc:
            record = dict(getattr(exc, "v2_record", {}) or locals().get("record", {}) or {})
            record.update({"record_kind": "v2-dispatch-record", "canary_task": dispatch_task,
                           "executor": "ClaudeResumeExecutor", "status": "DISPATCH-ERRORED",
                           "error_type": type(exc).__name__, "error": str(exc)[:500],
                           "lease_file": args.lease_file})
            verdict, rc = "DISPATCH-ERRORED (no retry)", RC_REFUSE
        with open(rec_path, "w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=2, sort_keys=True)

    report = {"verdict": verdict, "run_flag": args.run, "all_gates_green": all_green,
              "gates": gates, "rc": rc}
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for g in gates:
            print(("PASS" if g["pass"] else "FAIL") + f": {g['gate']}"
                  + (f" — {g['detail']}" if g["detail"] else ""))
        print(f"\nVERDICT: {verdict}")
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
