#!/usr/bin/env python3
"""Deterministic NO-PROVIDER tests for the governed v2 dispatch gate.

The real provider is never invoked: every --run case uses a recorder stub as
--claude-cmd. Live surfaces are read-only (board DB ro, cmux listing probes).
All mutating fixtures (board DBs, leases, stop files) live in a temp dir.
The v1 receipt is hash-proven untouched. Run: python3 tests_v2/test_dispatch_gate_v2.py
"""

import datetime
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATE = os.path.join(ROOT, "bin_verify", "dispatch_gate_v2.py")
V1_RECEIPT = os.path.join(ROOT, "evidence", "controlled-claude-worker-activation-receipt.json")
sys.path.insert(0, os.path.join(ROOT, "bin_verify"))
import issue_cmux_claude_session_binding as cmux_issuer

FAILURES = []


def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL") + f": {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def run_gate(*extra):
    p = subprocess.run([sys.executable, GATE, "--json", *extra], capture_output=True, text=True)
    rep = json.loads(p.stdout) if p.stdout.strip() else {}
    return p.returncode, rep


def gate_status(rep, prefix):
    m = [g for g in rep.get("gates", []) if g["gate"].startswith(prefix)]
    return m[0]["pass"] if m else None


GOOD_DISPATCH = ("APPROVAL A2-DISPATCH by=jarvis-orchestrator canary_task=t_beefcafe "
                 "provider=claude-code seat=interactive-subscription scope=no-op "
                 "cancellation=touch ACTIVATION-STOP then reclaim decision=approved")


def make_board(td, comments, canary_blocked=True):
    db = os.path.join(td, "board.db")
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE task_comments (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, "
                "author TEXT, body TEXT, created_at INTEGER)")
    con.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, assignee TEXT)")
    con.execute("CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT)")
    for author, body in comments:
        con.execute("insert into task_comments (task_id, author, body, created_at) values (?,?,?,1)",
                    ("t_0119603b", author, body))
    con.execute("insert into tasks values ('t_beefcafe', ?, NULL)",
                ("blocked" if canary_blocked else "ready",))
    con.commit(); con.close()
    return db


def make_stub(td, name, script):
    path = os.path.join(td, name)
    open(path, "w").write(script)
    os.chmod(path, 0o755)
    return path


def stub_verifier_ok(td):
    return make_stub(td, "verifier_ok.py",
                     "#!/usr/bin/env python3\nimport json\n"
                     "print(json.dumps({'verdict':'ACTIVATION-PREREQUISITES-MET','passed':21,'total':21}))\n")


def stub_verifier_fail(td):
    return make_stub(td, "verifier_fail.py",
                     "#!/usr/bin/env python3\nimport json,sys\n"
                     "print(json.dumps({'verdict':'FAIL-CLOSED','passed':19,'total':21}))\nsys.exit(4)\n")


def stub_reservation_ok(td):
    return make_stub(td, "resv_ok.py", "#!/usr/bin/env python3\nprint('OPEN')\n")


RESERVATION_JSON = ("/home/frank/.hermes/kanban/boards/jarvis-os/workspaces/"
                    "t_d7e6c034/reservation/seat-reservation.json")
CMUX_SESSION = json.load(open(RESERVATION_JSON))["seat"]["provider_session_uuid"]


def make_stub_events(td, name="stub-events", delay=0.0, session=CMUX_SESSION,
                     midrun=None, board_db=None, task_id=None):
    """Canned resume stream for the canonical executor's single provider
    boundary (there is deliberately NO bootstrap stream any more)."""
    sys.path.insert(0, os.path.join(ROOT, "bin_verify"))
    import v2_canary_executor as v2ce
    stub_dir = os.path.join(td, name)
    os.makedirs(stub_dir, exist_ok=True)
    marker = v2ce.V2_MARKER
    resume_events = [
        {"type": "system", "subtype": "init", "session_id": session},
        {"type": "assistant", "session_id": session},
        {"type": "result", "subtype": "success", "session_id": session,
         "is_error": False, "result": marker},
    ]
    cfg = {"events": resume_events, "delay": delay}
    if midrun:
        cfg.update({"midrun": midrun, "board_db": board_db, "task_id": task_id})
    open(os.path.join(stub_dir, "resume.json"), "w").write(json.dumps(cfg))
    return stub_dir


def make_session_binding(td, name="session-binding.json", session="v2sess-1",
                         tamper=False, **overrides):
    """PRE-EXISTING persisted interactive session-binding artifact fixture."""
    sys.path.insert(0, os.path.join(ROOT, "bin_verify"))
    import v2_canary_executor as v2ce
    now = datetime.datetime.now(datetime.timezone.utc)
    iso = lambda dt: dt.isoformat().replace("+00:00", "Z")  # noqa: E731
    rec = {
        "binding_kind": "v2-interactive-session-binding",
        "schema_version": 1,
        "provider": "claude-code",
        "session_id": session,
        "declared_by": "test-operator (fixture)",
        "issued_at_utc": iso(now - datetime.timedelta(seconds=60)),
        "expires_at_utc": iso(now + datetime.timedelta(seconds=600)),
    }
    rec.update(overrides)
    rec["artifact_fingerprint"] = v2ce.artifact_fingerprint(rec)
    if tamper:
        rec["session_id"] = "tampered-after-stamp"
    path = os.path.join(td, name)
    open(path, "w").write(json.dumps(rec, indent=2, sort_keys=True))
    return path


def stub_calls(stub_dir):
    log = os.path.join(stub_dir, "calls.log")
    if not os.path.exists(log):
        return []
    return open(log).read().splitlines()


def make_real_board(td, name):
    """A REAL freshly-migrated kb board with a packet card, a blocked canary
    card, and the exact A2 dispatch comment naming that canary."""
    sys.path.insert(0, ROOT)
    from hermes_cli import kanban_db as kb
    from pathlib import Path
    path = os.path.join(td, name, "kanban.db")
    os.makedirs(os.path.dirname(path))
    conn = kb.connect(Path(path))
    pkt = kb.create_task(conn, title="packet card fixture", body="fixture",
                         created_by="test", workspace_kind="scratch",
                         initial_status="blocked")
    cid = kb.create_task(conn, title="v2 canary fixture", body="fixture",
                         created_by="test", workspace_kind="scratch",
                         provider_override=kb.PROVIDER_CLAUDE_CODE,
                         model_override="claude-governed-v2-noop",
                         initial_status="blocked")
    kb.add_comment(conn, pkt, "jarvis-orchestrator",
                   GOOD_DISPATCH.replace("t_beefcafe", cid))
    conn.close()
    return path, pkt, cid


def receipt_fp(rec):
    clone = {k: v for k, v in rec.items() if k != "receipt_fingerprint"}
    return "sha256:" + hashlib.sha256(
        json.dumps(clone, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def make_receipt(td, name="receipt.json", tamper=False, **overrides):
    """Fixture Mac-style receipt built from the REAL reservation record (read-only)."""
    res = json.load(open(RESERVATION_JSON))
    now = datetime.datetime.now(datetime.timezone.utc)
    rec = {
        "receipt_kind": "mac-cmux-reservation-receipt",
        "schema_version": 1,
        "minted_on": "mac-cmux-control-socket",
        "minted_at_utc": now.isoformat().replace("+00:00", "Z"),
        "expires_at_utc": (now + datetime.timedelta(seconds=300)).isoformat().replace("+00:00", "Z"),
        "canary_task": "t_beefcafe",
        "cmux_workspace_id": res["seat"]["cmux_workspace_id"],
        "cmux_surface_id": res["seat"]["cmux_surface_id"],
        "control_socket": {"bundle_identifier": "com.cmuxterm.app", "cmux_daemon_version": res["seat"]["cmux_daemon_version"]},
    }
    rec.update(overrides)
    rec["receipt_fingerprint"] = cmux_issuer.receipt_fingerprint(rec)
    if tamper:
        rec["cmux_workspace_id"] = "TAMPERED-AFTER-STAMP"
    path = os.path.join(td, name)
    open(path, "w").write(json.dumps(rec, indent=2, sort_keys=True))
    return path


def make_cmux_binding_bundle(td, board, task="t_beefcafe", name="binding"):
    """Build the real issuer contract entirely under this test temp dir."""
    receipt = make_receipt(td, name + "-receipt.json", canary_task=task)
    wt = os.path.join(td, name + "-worktree")
    os.makedirs(os.path.join(wt, "bin_verify"))
    open(os.path.join(wt, "bin_verify", "mint_cmux_receipt.py"), "w").write("# marker\n")
    open(os.path.join(wt, "bin_verify", "dispatch_gate_v2.py"), "w").write("# marker\n")
    binding = cmux_issuer.issue_binding(
        worktree=Path(wt), board_db=Path(board), reservation_path=Path(RESERVATION_JSON),
        receipt_path=Path(receipt), task_id=task, session_id=CMUX_SESSION,
        declared_by="deterministic v2 fixture", ttl_seconds=120,
    )
    return receipt, str(binding)


def green_args(td, board):
    receipt, binding = make_cmux_binding_bundle(td, board, name="green")
    """All-green gate arguments using stubs + fixtures (valid Mac-style receipt)."""
    return ["--canary-task", "t_beefcafe",
            "--board-db", board,
            "--lease-file", os.path.join(td, "lease.json"),
            "--stop-file", os.path.join(td, "STOP"),
            "--packet-verifier", stub_verifier_ok(td),
            "--reservation-tool", stub_reservation_ok(td),
            "--cmux-receipt", receipt,
            "--session-binding", binding]


def main():
    v1_hash_before = sha(V1_RECEIPT)

    # 1. Live default dry-run: REFUSES (no dispatch comment exists for real; we never create one).
    rc, rep = run_gate()
    check("live default dry-run refuses (rc=5)", rc == 5 and rep["verdict"] == "REFUSE")
    check("live refusal driven by absent dispatch comment", gate_status(rep, "G2 ") is False)
    check("live refusal also on missing Mac receipt (no DGX-local cmux dependency)",
          gate_status(rep, "G3b") is False)
    check("live refusal also on missing pre-existing session binding (G3c, never created)",
          gate_status(rep, "G3c") is False)
    check("dry-run minted no lease",
          not os.path.exists(os.path.join(ROOT, "reservation", "v2-dispatch-lease.json")))

    with tempfile.TemporaryDirectory() as td:
        # 2. Dispatch-comment grammar strictness.
        cases = [
            ("valid comment passes G2", [(("jarvis-orchestrator"), GOOD_DISPATCH)], True),
            ("wrong author refused", [("impostor", GOOD_DISPATCH)], False),
            ("missing no-op scope refused",
             [("jarvis-orchestrator", GOOD_DISPATCH.replace(" scope=no-op", ""))], False),
            ("missing cancellation refused",
             [("jarvis-orchestrator", GOOD_DISPATCH.replace(" cancellation=touch ACTIVATION-STOP then reclaim", ""))], False),
            ("duplicate comments ambiguous -> refused",
             [("jarvis-orchestrator", GOOD_DISPATCH), ("jarvis-orchestrator", GOOD_DISPATCH)], False),
        ]
        for name, comments, expect in cases:
            board = make_board(td, comments)
            rc, rep = run_gate("--canary-task", "t_beefcafe", "--board-db", board,
                               "--lease-file", os.path.join(td, "l1.json"),
                               "--stop-file", os.path.join(td, "STOP"),
                               "--packet-verifier", stub_verifier_ok(td),
                               "--reservation-tool", stub_reservation_ok(td))
            check(name, gate_status(rep, "G2 ") is expect, json.dumps(rep.get("gates", []))[:200])
            os.remove(board)
        # mismatched canary id vs comment
        board = make_board(td, [("jarvis-orchestrator", GOOD_DISPATCH)])
        rc, rep = run_gate("--canary-task", "t_00000001", "--board-db", board,
                           "--lease-file", os.path.join(td, "l2.json"),
                           "--stop-file", os.path.join(td, "STOP"),
                           "--packet-verifier", stub_verifier_ok(td),
                           "--reservation-tool", stub_reservation_ok(td))
        check("mismatched canary id refused", rc == 5 and gate_status(rep, "G2b") is False)
        os.remove(board)

        # 2b. Mac-receipt validation matrix (G3b): stale / mismatched / missing / tampered.
        board = make_board(td, [("jarvis-orchestrator", GOOD_DISPATCH)])
        now = datetime.datetime.now(datetime.timezone.utc)
        iso = lambda dt: dt.isoformat().replace("+00:00", "Z")  # noqa: E731
        receipt_cases = [
            ("missing receipt refused", os.path.join(td, "no-such-receipt.json")),
            ("stale (expired) receipt refused",
             make_receipt(td, "r-stale.json",
                          minted_at_utc=iso(now - datetime.timedelta(seconds=400)),
                          expires_at_utc=iso(now - datetime.timedelta(seconds=100)))),
            ("future-minted receipt refused",
             make_receipt(td, "r-future.json",
                          minted_at_utc=iso(now + datetime.timedelta(seconds=120)),
                          expires_at_utc=iso(now + datetime.timedelta(seconds=420)))),
            ("over-window receipt refused",
             make_receipt(td, "r-long.json",
                          expires_at_utc=iso(now + datetime.timedelta(seconds=7200)))),
            ("wrong-workspace receipt refused",
             make_receipt(td, "r-ws.json", cmux_workspace_id="00000000-DEAD-BEEF-0000-000000000000")),
            ("wrong-surface receipt refused",
             make_receipt(td, "r-surf.json", cmux_surface_id="00000000-DEAD-BEEF-0000-000000000001")),
            ("wrong-task-bound receipt refused",
             make_receipt(td, "r-task.json", canary_task="t_00000002")),
            ("tampered receipt (fingerprint) refused",
             make_receipt(td, "r-tamper.json", tamper=True)),
        ]
        for name, rpath in receipt_cases:
            rc, rep = run_gate("--canary-task", "t_beefcafe", "--board-db", board,
                               "--lease-file", os.path.join(td, "lr.json"),
                               "--stop-file", os.path.join(td, "STOP"),
                               "--packet-verifier", stub_verifier_ok(td),
                               "--reservation-tool", stub_reservation_ok(td),
                               "--cmux-receipt", rpath)
            check(name, rc == 5 and gate_status(rep, "G3b") is False,
                  json.dumps([g for g in rep.get("gates", []) if g["gate"].startswith("G3b")]))
        rc, rep = run_gate("--canary-task", "t_beefcafe", "--board-db", board,
                           "--lease-file", os.path.join(td, "lr2.json"),
                           "--stop-file", os.path.join(td, "STOP"),
                           "--packet-verifier", stub_verifier_ok(td),
                           "--reservation-tool", stub_reservation_ok(td),
                           "--cmux-receipt", make_receipt(td, "r-valid.json"))
        check("valid fresh task-bound receipt passes G3b", gate_status(rep, "G3b") is True)
        os.remove(board)

        # 3. All-green DRY-RUN: WOULD-DISPATCH but still rc=5, no lease, provider untouched.
        board = make_board(td, [("jarvis-orchestrator", GOOD_DISPATCH)])
        dry_stub = make_stub_events(td, "stub-dry")
        rc, rep = run_gate(*green_args(td, board), "--stub-events-dir", dry_stub)
        check("all-green dry-run reports WOULD-DISPATCH", rep["verdict"].startswith("WOULD-DISPATCH")
              and rep["all_gates_green"] is True, rep.get("verdict"))
        check("all-green dry-run still refuses (rc=5)", rc == 5)
        check("all-green dry-run: no lease, provider not called",
              not os.path.exists(os.path.join(td, "lease.json")) and stub_calls(dry_stub) == [])

        # 4. Stale/failing packet verifier refuses even with --run (provider untouched).
        rc, rep = run_gate("--run", "--canary-task", "t_beefcafe", "--board-db", board,
                           "--lease-file", os.path.join(td, "lease.json"),
                           "--stop-file", os.path.join(td, "STOP"),
                           "--packet-verifier", stub_verifier_fail(td),
                           "--reservation-tool", stub_reservation_ok(td),
                           "--stub-events-dir", dry_stub)
        check("--run with failing packet verifier refuses", rc == 5 and rep["verdict"] == "REFUSE"
              and stub_calls(dry_stub) == [])

        # 5. Stop file present refuses --run.
        open(os.path.join(td, "STOP2"), "w").write("halt\n")
        rc2, rep2 = run_gate("--run", "--canary-task", "t_beefcafe", "--board-db", board,
                             "--lease-file", os.path.join(td, "lease-b.json"),
                             "--stop-file", os.path.join(td, "STOP2"),
                             "--packet-verifier", stub_verifier_ok(td),
                             "--reservation-tool", stub_reservation_ok(td),
                             "--cmux-receipt", make_receipt(td, "r-stop.json"),
                             "--stub-events-dir", dry_stub)
        check("stop file present refuses --run", rc2 == 5 and gate_status(rep2, "G5a") is False
              and not os.path.exists(os.path.join(td, "lease-b.json")))
        os.remove(board)

        # 5b. G3c session-binding gate is fail-closed (dry-run, fake board):
        # missing / expired / tampered / wrong-kind artifacts each refuse and
        # the artifact is never created by the gate.
        board_g3c = make_board(td, [("jarvis-orchestrator", GOOD_DISPATCH)])
        g3c_cases = [
            ("missing session binding refused (G3c)", os.path.join(td, "no-binding.json")),
            ("expired session binding refused (G3c)",
             make_session_binding(td, "b-expired.json",
                                  expires_at_utc="2020-01-01T00:00:00Z",
                                  issued_at_utc="2019-12-31T23:59:00Z")),
            ("tampered session binding refused (G3c)",
             make_session_binding(td, "b-tampered.json", tamper=True)),
            ("wrong-kind session binding refused (G3c)",
             make_session_binding(td, "b-kind.json", binding_kind="something-else")),
        ]
        for name, bpath in g3c_cases:
            rc, rep = run_gate("--canary-task", "t_beefcafe", "--board-db", board_g3c,
                               "--lease-file", os.path.join(td, "lg3c.json"),
                               "--stop-file", os.path.join(td, "STOP"),
                               "--packet-verifier", stub_verifier_ok(td),
                               "--reservation-tool", stub_reservation_ok(td),
                               "--session-binding", bpath)
            check(name, rc == 5 and gate_status(rep, "G3c") is False
                  and not os.path.exists(bpath + ".created"))
        os.remove(board_g3c)

        # 4b/6. All-green --run on a REAL kb board dispatches EXACTLY ONCE
        # through the canonical ClaudeResumeExecutor/task-run lifecycle. The
        # session comes from the PRE-EXISTING binding artifact; there is no
        # bootstrap provider call anywhere.
        board_r, pkt, cid = make_real_board(td, "realboard")
        run_stub = make_stub_events(td, "stub-run")
        wsroot = os.path.join(td, "wsroot")
        os.makedirs(wsroot)
        real_receipt, real_binding = make_cmux_binding_bundle(td, board_r, task=cid, name="real")
        real_args = ["--canary-task", cid, "--board-db", board_r, "--packet-card", pkt,
                     "--lease-file", os.path.join(td, "lease.json"),
                     "--stop-file", os.path.join(td, "STOP"),
                     "--packet-verifier", stub_verifier_ok(td),
                     "--reservation-tool", stub_reservation_ok(td),
                     "--cmux-receipt", real_receipt, "--session-binding", real_binding,
                     "--workspace-root", wsroot,
                     "--stub-events-dir", run_stub]
        rc, rep = run_gate("--run", *real_args)
        calls = stub_calls(run_stub)
        check("--run all-green dispatches exactly once via stub", rc == 0
              and rep["verdict"] == "DISPATCHED-ONCE"
              and len(calls) == 1 and calls[0].startswith("resume "),
              json.dumps({"rc": rc, "verdict": rep.get("verdict"), "calls": calls})[:400])
        check("no bootstrap provider call exists anywhere in the dispatch",
              not any("bootstrap" in c for c in calls))
        rec_path = os.path.join(ROOT, "evidence", "v2-dispatch-record.json")
        record = json.load(open(rec_path)) if os.path.isfile(rec_path) else {}
        check("dispatch went through ClaudeResumeExecutor (no raw provider path)",
              record.get("executor") == "ClaudeResumeExecutor"
              and record.get("status") == "DISPATCHED-ONCE"
              and "ClaudeProcessRunner" in record.get("provider_boundary", ""))
        check("session taken from pre-existing artifact, never created here",
              record.get("session_binding_gate", {}).get("created_here") is False
              and record.get("session_binding_gate", {}).get("session_sha256")
              == hashlib.sha256(CMUX_SESSION.encode()).hexdigest())
        check("one-shot lease minted and marked non-reusable",
              json.load(open(os.path.join(td, "lease.json")))["reusable"] is False)
        resume_argv = record.get("resume_argv", [])
        check("canonical resume argv rendered from persisted binding (claude --resume)",
              resume_argv[:2] == ["claude", "--resume"] and CMUX_SESSION in resume_argv)
        check("run-bound heartbeats proven (live claim renewed by the runner)",
              record.get("heartbeats", {}).get("runner_heartbeat_calls", 0) >= 2)
        check("terminal fence + A3 + native terminal result recorded",
              record.get("fence", {}).get("current_run_id") is not None
              and record.get("a3", {}).get("revocation_latched_at_launch") is False
              and record.get("terminal", {}).get("task_status") == "done"
              and record.get("terminal", {}).get("terminal_write") is True
              and record.get("terminal", {}).get("summary_has_marker") is True)
        check("canary session binding retired and workspace removed",
              record.get("binding_retired") is True
              and record.get("workspace_removed") is True
              and os.listdir(wsroot) == [])

        # 7. Second --run refuses on existing lease (non-reusable), provider NOT re-called.
        rc, rep = run_gate("--run", *real_args)
        check("second --run refused by existing lease", rc == 5 and gate_status(rep, "G4 ") is False
              and stub_calls(run_stub) == calls)

        # 7b. Lease O_EXCL contention is a deterministic refusal, never an
        # uncaught EEXIST (direct unit probe of the mint primitive).
        import importlib.util
        spec = importlib.util.spec_from_file_location("gate_mod", GATE)
        gate_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gate_mod)
        contended = os.path.join(td, "contended-lease.json")
        first = gate_mod.mint_lease(contended, "t_deadbeef")
        second = gate_mod.mint_lease(contended, "t_deadbeef")
        check("lease mint: first wins, loser gets deterministic False (no EEXIST raise)",
              first is True and second is False
              and json.load(open(contended))["reusable"] is False)

        # 7c. CONCURRENT contention: two simultaneous --run dispatchers, one
        # lease. Exactly one dispatches; the loser refuses cleanly (rc=5, no
        # traceback); combined provider-boundary calls stay exactly one pair.
        board_c, pkt_c, cid_c = make_real_board(td, "raceboard")
        race_stub = make_stub_events(td, "stub-race", delay=1.0)
        wsroot_c = os.path.join(td, "wsroot-race")
        os.makedirs(wsroot_c)
        race_receipt, race_binding = make_cmux_binding_bundle(td, board_c, task=cid_c, name="race")
        race_args = [sys.executable, GATE, "--json", "--run",
                     "--canary-task", cid_c, "--board-db", board_c, "--packet-card", pkt_c,
                     "--lease-file", os.path.join(td, "race-lease.json"),
                     "--stop-file", os.path.join(td, "STOP"),
                     "--packet-verifier", stub_verifier_ok(td),
                     "--reservation-tool", stub_reservation_ok(td),
                     "--cmux-receipt", race_receipt, "--session-binding", race_binding,
                     "--workspace-root", wsroot_c,
                     "--stub-events-dir", race_stub]
        procs = [subprocess.Popen(race_args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True) for _ in range(2)]
        outs = [p.communicate(timeout=180) for p in procs]
        rcs = sorted(p.returncode for p in procs)
        race_calls = stub_calls(race_stub)
        check("concurrent --run: exactly one dispatcher wins, one refuses (rc {0,5})",
              rcs == [0, 5], repr(rcs))
        check("concurrent --run: exactly one provider-boundary resume total",
              len(race_calls) == 1 and race_calls[0].startswith("resume "),
              repr(race_calls))
        check("concurrent loser refuses cleanly (no traceback on either process)",
              all("Traceback" not in err for _out, err in outs))

        # 8. INTEGRATION NEGATIVES (t_4d09e0d9 round 2): adversarial
        # POST-LAUNCH conditions must abort the run with ZERO accepted sealed
        # terminal result, plus cleanup and binding retirement.
        import sqlite3 as sqlite3mod

        def task_status_of(board, tid):
            con = sqlite3mod.connect(f"file:{board}?mode=ro", uri=True)
            try:
                return con.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0]
            finally:
                con.close()

        # 8a. Post-launch A3 revocation latch: latched mid-run by an adversary
        # connection; the next governed heartbeat must abort before any seal.
        board_a3, pkt_a3, cid_a3 = make_real_board(td, "a3board")
        a3_stub = make_stub_events(td, "stub-a3", midrun={"latch_a3": True},
                                   board_db=board_a3, task_id=cid_a3)
        wsroot_a3 = os.path.join(td, "wsroot-a3")
        os.makedirs(wsroot_a3)
        a3_receipt, a3_binding = make_cmux_binding_bundle(td, board_a3, task=cid_a3, name="a3")
        rc, rep = run_gate("--run", "--canary-task", cid_a3, "--board-db", board_a3,
                           "--packet-card", pkt_a3,
                           "--lease-file", os.path.join(td, "lease-a3.json"),
                           "--stop-file", os.path.join(td, "STOP"),
                           "--packet-verifier", stub_verifier_ok(td),
                           "--reservation-tool", stub_reservation_ok(td),
                           "--cmux-receipt", a3_receipt, "--session-binding", a3_binding,
                           "--workspace-root", wsroot_a3,
                           "--stub-events-dir", a3_stub)
        record = json.load(open(rec_path)) if os.path.isfile(rec_path) else {}
        check("post-launch A3 revocation aborts the run (rc=5, DISPATCH-ERRORED)",
              rc == 5 and rep.get("verdict") == "DISPATCH-ERRORED (no retry)"
              and record.get("status") == "DISPATCH-ERRORED"
              and "A3 revocation latched post-launch" in record.get("error", ""),
              json.dumps({"rc": rc, "err": record.get("error", "")})[:300])
        check("post-launch A3: zero accepted sealed terminal result (task not done)",
              task_status_of(board_a3, cid_a3) != "done"
              and "terminal" not in record and stub_calls(a3_stub) == [])
        check("post-launch A3: cleanup + binding retirement on failure path",
              record.get("binding_retired") is True
              and record.get("workspace_removed") is True
              and os.listdir(wsroot_a3) == [])

        # 8b. Mid-run loss/reclaim of the live current run/fence: the next
        # run-bound heartbeat must raise ClaimLeaseLost and abort.
        board_cl, pkt_cl, cid_cl = make_real_board(td, "claimboard")
        cl_stub = make_stub_events(td, "stub-claim", midrun={"steal_claim": True},
                                   board_db=board_cl, task_id=cid_cl)
        wsroot_cl = os.path.join(td, "wsroot-claim")
        os.makedirs(wsroot_cl)
        cl_receipt, cl_binding = make_cmux_binding_bundle(td, board_cl, task=cid_cl, name="claim")
        rc, rep = run_gate("--run", "--canary-task", cid_cl, "--board-db", board_cl,
                           "--packet-card", pkt_cl,
                           "--lease-file", os.path.join(td, "lease-cl.json"),
                           "--stop-file", os.path.join(td, "STOP"),
                           "--packet-verifier", stub_verifier_ok(td),
                           "--reservation-tool", stub_reservation_ok(td),
                           "--cmux-receipt", cl_receipt, "--session-binding", cl_binding,
                           "--workspace-root", wsroot_cl,
                           "--stub-events-dir", cl_stub)
        record = json.load(open(rec_path)) if os.path.isfile(rec_path) else {}
        check("mid-run claim loss aborts via ClaimLeaseLost (rc=5, DISPATCH-ERRORED)",
              rc == 5 and rep.get("verdict") == "DISPATCH-ERRORED (no retry)"
              and record.get("status") == "DISPATCH-ERRORED"
              and record.get("error_type") == "ClaimLeaseLost",
              json.dumps({"rc": rc, "et": record.get("error_type"),
                          "err": record.get("error", "")})[:300])
        check("claim loss: zero accepted sealed terminal result (task not done)",
              task_status_of(board_cl, cid_cl) != "done"
              and "terminal" not in record and stub_calls(cl_stub) == [])
        check("claim loss: cleanup + binding retirement on failure path",
              record.get("binding_retired") is True
              and record.get("workspace_removed") is True
              and os.listdir(wsroot_cl) == [])

        # cleanup evidence record written by the stub dispatch (test artifact, not real evidence)
        if os.path.isfile(rec_path):
            d = json.load(open(rec_path))
            if d.get("stubbed") is True or d.get("canary_task") in (cid, cid_c):
                os.remove(rec_path)
                print("note: removed stub-run v2-dispatch-record.json (test artifact)")

    # 8. v1 receipt untouched.
    check("v1 receipt hash unchanged (historical evidence preserved)", sha(V1_RECEIPT) == v1_hash_before)

    print()
    if FAILURES:
        print(f"RESULT: {len(FAILURES)} FAILURE(S): {FAILURES}")
        return 1
    print("RESULT: ALL V2 GATE CHECKS PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
