#!/usr/bin/env python3
"""Deterministic NO-PROVIDER tests for the governed v2 dispatch gate.

The real provider is never invoked: every --run case uses a recorder stub as
--claude-cmd. Live surfaces are read-only (board DB ro, cmux listing probes).
All mutating fixtures (board DBs, leases, stop files) live in a temp dir.
The v1 receipt is hash-proven untouched. Run: python3 tests_v2/test_dispatch_gate_v2.py
"""

import datetime
import hashlib
import hmac
import importlib.util
import secrets
import time
import json
import os
import shutil
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
    extra = list(extra)
    if "--canary-task" in extra and "--lease-file" in extra:
        li = extra.index("--lease-file")
        lease = extra[li + 1]
        while "--lease-file" in extra:
            li = extra.index("--lease-file")
            del extra[li:li + 2]
        if "--stub-events-dir" not in extra:
            extra.extend(["--stub-events-dir", lease + ".fixture"])
        if "--activation-packet" not in extra:
            board = extra[extra.index("--board-db") + 1] if "--board-db" in extra else lease
            extra.extend(["--activation-packet", activation_packet(os.path.dirname(board))])
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
                     "#!/usr/bin/env python3\nimport argparse,json\n"
                     "p=argparse.ArgumentParser();p.add_argument('--activation-packet');p.add_argument('--task-id');p.add_argument('--json',action='store_true');p.parse_args()\n"
                     "print(json.dumps({'verdict':'ACTIVATION-PREREQUISITES-MET','passed':21,'total':21}))\n")


def stub_verifier_fail(td):
    return make_stub(td, "verifier_fail.py",
                     "#!/usr/bin/env python3\nimport argparse,json,sys\n"
                     "p=argparse.ArgumentParser();p.add_argument('--activation-packet');p.add_argument('--task-id');p.add_argument('--json',action='store_true');p.parse_args()\n"
                     "print(json.dumps({'verdict':'FAIL-CLOSED','passed':19,'total':21}))\nsys.exit(4)\n")


def stub_reservation_ok(td):
    return make_stub(td, "resv_ok.py", "#!/usr/bin/env python3\nprint('OPEN')\n")




def activation_packet(td):
    """Minimal governed packet fixture, independent of untracked live artifacts."""
    path = os.path.join(td, "activation-packet.json")
    if not os.path.exists(path):
        open(path, "w").write(json.dumps({
            "task_id": "t_beefcafe",
            "worker": {"count_exactly": 1, "provider": "claude-code"},
            "caps": {"one_run_only": True, "max_retries": 0},
        }, sort_keys=True))
    return path
CMUX_SESSION = "1194f145-bc7d-4fd6-9762-16b4414eb4d1"
CMUX_WS = "9A3E7E93-963F-45AB-9A00-79E218190B5D"
CMUX_SURFACE = "577E1920-C0EE-4140-A649-361647B6B9A5"
MINT_WS = "44444444-AAAA-BBBB-CCCC-000000000004"
MINT_SURFACE = "66666666-AAAA-BBBB-CCCC-000000000006"


def reservation_path(td):
    """One hermetic dual-anchor reservation shared by this temp test root."""
    path = os.path.join(td, "dual-anchor-reservation.json")
    if os.path.exists(path):
        return path
    res = {
        "record_kind": "cmux-manual-seat-reservation", "schema_version": 2,
        "seat": {"cmux_workspace_id": CMUX_WS, "cmux_surface_id": CMUX_SURFACE,
                 "cmux_daemon_version": "0.64.20", "provider": "claude-code",
                 "kind": "cmux-interactive-claude-max",
                 "provider_session_uuid": CMUX_SESSION},
        "mint_control": {"cmux_workspace_id": MINT_WS, "cmux_surface_id": MINT_SURFACE},
    }
    res["provider_anchor_fingerprint"] = cmux_issuer.dual_anchor.anchor_fingerprint(res["seat"])
    res["mint_control_anchor_fingerprint"] = cmux_issuer.dual_anchor.anchor_fingerprint(res["mint_control"])
    res["reservation_fingerprint"] = cmux_issuer.reservation_fingerprint(res)
    open(path, "w").write(cmux_issuer.canonical_json(res))
    return path


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



def seed_unfolded_terminal_backlog(board, count):
    """Create > broker batch-limit reclaimed runs without any provider call.

    This reproduces the live failure: a board-wide completion drain sees old
    rows first and cannot reach the current handoff within its bounded pass.
    The governed lifecycle must fold its exact handoff run instead.
    """
    sys.path.insert(0, ROOT)
    from hermes_cli import kanban_db as kb
    conn = kb.connect(Path(board))
    try:
        for _ in range(count):
            tid = kb.create_task(conn, title="backlog fixture", body="fixture",
                                 created_by="test", workspace_kind="scratch",
                                 initial_status="blocked")
            assert kb.unblock_task(conn, tid)
            assert kb.claim_task(conn, tid, claimer="backlog-fixture", ttl_seconds=60)
            assert kb.reclaim_task(conn, tid, reason="deterministic backlog fixture")
    finally:
        conn.close()

def receipt_fp(rec):
    clone = {k: v for k, v in rec.items() if k != "receipt_fingerprint"}
    return "sha256:" + hashlib.sha256(
        json.dumps(clone, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def make_receipt(td, name="receipt.json", tamper=False, **overrides):
    """Fixture schema3 receipt built from the hermetic dual-anchor reservation."""
    res = json.load(open(reservation_path(td)))
    now = datetime.datetime.now(datetime.timezone.utc)
    rec = {
        "receipt_kind": "mac-cmux-reservation-receipt",
        "schema_version": 3,
        "minted_on": "mac-cmux-control-socket",
        "minted_at_utc": now.isoformat().replace("+00:00", "Z"),
        "expires_at_utc": (now + datetime.timedelta(seconds=300)).isoformat().replace("+00:00", "Z"),
        "canary_task": "t_beefcafe",
        "reservation_fingerprint": res["reservation_fingerprint"],
        "provider_anchor_fingerprint": res["provider_anchor_fingerprint"],
        "mint_control_anchor_fingerprint": res["mint_control_anchor_fingerprint"],
        "cmux_workspace_id": res["seat"]["cmux_workspace_id"],
        "cmux_surface_id": res["seat"]["cmux_surface_id"],
        "mint_control_context": {
            "surface_id": res["mint_control"]["cmux_surface_id"],
            "workspace_id": res["mint_control"]["cmux_workspace_id"],
            "resolved_surface_id": MINT_SURFACE, "resolved_workspace_id": MINT_WS,
            "tty": "/dev/ttys012", "proof": "nonce-read-screen",
            "nonce_sha256": hashlib.sha256(b"gate-fixture-nonce").hexdigest()},
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
        worktree=Path(wt), board_db=Path(board), reservation_path=Path(reservation_path(td)),
        receipt_path=Path(receipt), task_id=task, session_id=CMUX_SESSION,
        declared_by="deterministic v2 fixture", ttl_seconds=120,
    )
    return receipt, str(binding)


def green_args(td, board, *, name="green"):
    """All-green gate arguments using stubs + fixtures (valid Mac-style receipt)."""
    hermes_home = os.path.join(td, "hermes-profile-" + name)
    os.makedirs(hermes_home, exist_ok=True)
    receipt, binding = make_cmux_binding_bundle(td, board, name=name)
    return ["--canary-task", "t_beefcafe",
            "--board-db", board,
            "--lease-file", os.path.join(td, "lease.json"),
            "--stop-file", os.path.join(td, "STOP"),
            "--packet-verifier", stub_verifier_ok(td),
            "--reservation-tool", stub_reservation_ok(td),
            "--reservation-json", reservation_path(td),
            "--cmux-receipt", receipt,
            "--session-binding", binding,
            "--hermes-home", hermes_home,
            "--activation-packet", activation_packet(td)]


def main():
    v1_hash_before = sha(V1_RECEIPT)
    live_lease = os.path.join(ROOT, "reservation", "v2-dispatch-lease.json")
    live_lease_before = open(live_lease, "rb").read() if os.path.exists(live_lease) else None

    # 1. Live default dry-run always refuses; its current details may reflect
    # preserved evidence from an earlier canary, so fixture tests own G2-G5.
    rc, rep = run_gate()
    check("live default dry-run refuses (rc=5)", rc == 5 and rep["verdict"] == "REFUSE")
    check("live default dry-run never dispatches regardless of current G2 state",
          gate_status(rep, "G2 ") in (True, False))
    check("live refusal also on missing Mac receipt (no DGX-local cmux dependency)",
          gate_status(rep, "G3b") is False)
    check("live refusal also on missing pre-existing session binding (G3c, never created)",
          gate_status(rep, "G3c") is False)
    check("dry-run leaves any pre-existing real lease byte-for-byte unchanged",
          (open(live_lease, "rb").read() if os.path.exists(live_lease) else None) == live_lease_before)

    with tempfile.TemporaryDirectory() as td:
        # 2. Dispatch-comment grammar strictness.
        cases = [
            ("valid comment passes G2", [(("jarvis-orchestrator"), GOOD_DISPATCH)], True),
            ("wrong author refused", [("impostor", GOOD_DISPATCH)], False),
            ("missing no-op scope refused",
             [("jarvis-orchestrator", GOOD_DISPATCH.replace(" scope=no-op", ""))], False),
            ("missing cancellation refused",
             [("jarvis-orchestrator", GOOD_DISPATCH.replace(" cancellation=touch ACTIVATION-STOP then reclaim", ""))], False),
            ("A2 command-header suffix refused",
             [("jarvis-orchestrator", GOOD_DISPATCH.replace("A2-DISPATCH", "A2-DISPATCH-evil"))], False),
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
        check("mismatched canary id refused", rc == 5 and gate_status(rep, "G2 ") is False)
        os.remove(board)

        # A spent, valid historical A2 comment for a different disposable task
        # must not poison a fresh exact-task approval on the same packet card.
        historical = GOOD_DISPATCH.replace("t_beefcafe", "t_deadbeef")
        board = make_board(td, [("jarvis-orchestrator", historical),
                                ("jarvis-orchestrator", GOOD_DISPATCH)])
        rc, rep = run_gate("--canary-task", "t_beefcafe", "--board-db", board,
                           "--lease-file", os.path.join(td, "l3.json"),
                           "--stop-file", os.path.join(td, "STOP"),
                           "--packet-verifier", stub_verifier_ok(td),
                           "--reservation-tool", stub_reservation_ok(td))
        check("historical other-task A2 comment does not block requested task",
              gate_status(rep, "G2 ") is True and gate_status(rep, "G2b") is True)
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
            ("legacy schema2 receipt downgrade refused",
             make_receipt(td, "r-schema2.json", schema_version=2)),
            ("missing provider anchor fingerprint refused",
             make_receipt(td, "r-no-provider-anchor.json", provider_anchor_fingerprint=None)),
            ("substituted mint-control anchor refused",
             make_receipt(td, "r-mint-substitution.json", mint_control_context={
                 "surface_id": "77777777-AAAA-BBBB-CCCC-000000000018",
                 "workspace_id": MINT_WS,
                 "resolved_surface_id": MINT_SURFACE,
                 "resolved_workspace_id": MINT_WS,
                 "tty": "/dev/ttys012", "proof": "nonce-read-screen",
                 "nonce_sha256": hashlib.sha256(b"gate-fixture-nonce").hexdigest()})),
            ("tampered receipt (fingerprint) refused",
             make_receipt(td, "r-tamper.json", tamper=True)),
        ]
        for name, rpath in receipt_cases:
            rc, rep = run_gate("--canary-task", "t_beefcafe", "--board-db", board,
                               "--lease-file", os.path.join(td, "lr.json"),
                               "--stop-file", os.path.join(td, "STOP"),
                               "--packet-verifier", stub_verifier_ok(td),
                               "--reservation-tool", stub_reservation_ok(td),
                               "--reservation-json", reservation_path(td),
                               "--cmux-receipt", rpath)
            check(name, rc == 5 and gate_status(rep, "G3b") is False,
                  json.dumps([g for g in rep.get("gates", []) if g["gate"].startswith("G3b")]))
        rc, rep = run_gate("--canary-task", "t_beefcafe", "--board-db", board,
                           "--lease-file", os.path.join(td, "lr2.json"),
                           "--stop-file", os.path.join(td, "STOP"),
                           "--packet-verifier", stub_verifier_ok(td),
                           "--reservation-tool", stub_reservation_ok(td),
                           "--reservation-json", reservation_path(td),
                           "--cmux-receipt", make_receipt(td, "r-valid.json"))
        check("valid fresh task-bound receipt passes G3b", gate_status(rep, "G3b") is True)
        legacy_res = json.load(open(reservation_path(td)))
        legacy_res["schema_version"] = 1
        legacy_res["reservation_fingerprint"] = cmux_issuer.reservation_fingerprint(legacy_res)
        legacy_path = os.path.join(td, "legacy-reservation.json")
        open(legacy_path, "w").write(cmux_issuer.canonical_json(legacy_res))
        rc, rep = run_gate("--canary-task", "t_beefcafe", "--board-db", board,
                           "--lease-file", os.path.join(td, "lr3.json"),
                           "--packet-verifier", stub_verifier_ok(td),
                           "--reservation-tool", stub_reservation_ok(td),
                           "--reservation-json", legacy_path,
                           "--cmux-receipt", make_receipt(td, "r-legacy-res.json"))
        check("legacy reservation downgrade refuses G3b", gate_status(rep, "G3b") is False)
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

        # A named task with no lease override receives its own source-root
        # namespace. A historical global lease must not poison that namespace.
        import importlib.util
        spec = importlib.util.spec_from_file_location("gate_task_scope", GATE)
        gate_task_scope = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gate_task_scope)
        scoped_packet = activation_packet(td)
        scoped_lease = gate_task_scope.task_scoped_lease_path("t_beefcafe", scoped_packet)
        historical_lease = os.path.join(ROOT, "reservation", "v2-dispatch-lease.json")
        historical_before = open(historical_lease, "rb").read() if os.path.exists(historical_lease) else None
        os.makedirs(os.path.join(td, "profile-scoped"), exist_ok=True)
        rc, rep = run_gate("--canary-task", "t_beefcafe", "--board-db", board,
                           "--activation-packet", scoped_packet,
                           "--packet-verifier", stub_verifier_ok(td),
                           "--reservation-tool", stub_reservation_ok(td),
                           "--reservation-json", reservation_path(td),
                           "--cmux-receipt", make_receipt(td, "r-scoped.json"),
                           "--session-binding", make_cmux_binding_bundle(td, board, name="scoped")[1],
                           "--hermes-home", os.path.join(td, "profile-scoped"))
        check("named task derives isolated lease path, not global historical lease",
              rc == 5 and not os.path.exists(scoped_lease)
              and (open(historical_lease, "rb").read() if os.path.exists(historical_lease) else None) == historical_before)
        bad_packet = os.path.join(td, "bad-task-packet.json")
        bad = json.load(open(scoped_packet)); bad["task_id"] = "t_deadbeef"
        open(bad_packet, "w").write(json.dumps(bad))
        try:
            gate_task_scope.task_scoped_lease_path("t_beefcafe", bad_packet)
            mismatch_refused = False
        except ValueError:
            mismatch_refused = True
        check("task-scoped lease rejects packet task mismatch", mismatch_refused)
        arbitrary = os.path.join(td, "arbitrary-production-lease.json")
        raw = subprocess.run([sys.executable, GATE, "--json", "--run", "--canary-task", "t_beefcafe",
                              "--lease-file", arbitrary], capture_output=True, text=True)
        raw_report = json.loads(raw.stdout)
        check("named production run refuses arbitrary lease-file override before gates",
              raw.returncode == 5 and gate_status(raw_report, "G0 task-scoped lease authority") is False
              and not os.path.exists(arbitrary), raw.stdout[:300])

        # Named production canaries derive one trusted task-local stop
        # control. It must veto both inspection and --run; a caller-selected
        # alternate stop file is refused before any lease or provider path.
        stop_task = "t_cafef00d"
        canonical_stop = gate_task_scope.task_scoped_stop_path(stop_task)
        stop_parent = os.path.dirname(canonical_stop)
        os.makedirs(stop_parent, exist_ok=True)
        stop_packet = os.path.join(stop_parent, f"ACTIVATION-PACKET-{stop_task}.json")
        open(stop_packet, "w").write(json.dumps({"task_id": stop_task}))
        open(canonical_stop, "w").write("halt\\n")
        try:
            for armed in (False, True):
                argv = ["--canary-task", stop_task, "--json"]
                if armed:
                    argv.insert(0, "--run")
                rc, rep = run_gate(*argv)
                check(f"canonical named stop blocks {'--run' if armed else 'dry-run'}",
                      rc == 5 and gate_status(rep, "G5a") is False,
                      json.dumps(rep.get("gates", []))[:300])
            alternate_stop = os.path.join(td, "alternate-STOP")
            rc, rep = run_gate("--run", "--canary-task", stop_task, "--stop-file", alternate_stop,
                               "--json")
            check("named production alternate stop override refuses before lease/provider",
                  rc == 5 and gate_status(rep, "G0 real-run canonical-input boundary") is False
                  and not os.path.exists(os.path.join(stop_parent, "v2-dispatch-lease.json")),
                  json.dumps(rep.get("gates", []))[:300])
        finally:
            shutil.rmtree(stop_parent, ignore_errors=True)

        # The public executor API is fixture-only: neither runner=None nor an
        # explicitly supplied real Claude runner may reach the provider path.
        executor_spec = importlib.util.spec_from_file_location(
            "v2_executor_direct_capability", os.path.join(ROOT, "bin_verify", "v2_canary_executor.py"))
        executor_mod = importlib.util.module_from_spec(executor_spec)
        executor_spec.loader.exec_module(executor_mod)
        try:
            executor_mod.dispatch_canary(
                board_db=os.path.join(td, "unused.db"), canary_task="t_beefcafe",
                workspace_root=os.path.join(td, "unused-workspace"),
                session_binding_path=os.path.join(td, "unused-binding.json"),
                cmux_receipt_path=os.path.join(td, "unused-receipt.json"),
                reservation_path=os.path.join(td, "unused-reservation.json"),
                issuer_path=os.path.join(td, "unused-issuer.py"),
                hermes_home=os.path.join(td, "unused-profile"), runner=None)
            direct_real_refused = False
        except executor_mod.DispatchError as exc:
            direct_real_refused = "fixture-only" in str(exc)
        check("public runner=None executor invocation is refused before provider creation",
              direct_real_refused)
        from hermes_cli.claude_executor import SubprocessClaudeRunner
        try:
            executor_mod.dispatch_canary(
                board_db=os.path.join(td, "unused-real.db"), canary_task="t_beefcafe",
                workspace_root=os.path.join(td, "unused-real-workspace"),
                session_binding_path=os.path.join(td, "unused-real-binding.json"),
                cmux_receipt_path=os.path.join(td, "unused-real-receipt.json"),
                reservation_path=os.path.join(td, "unused-real-reservation.json"),
                issuer_path=os.path.join(td, "unused-real-issuer.py"),
                hermes_home=os.path.join(td, "unused-real-profile"),
                runner=SubprocessClaudeRunner())
            direct_explicit_real_refused = False
        except executor_mod.DispatchError as exc:
            direct_explicit_real_refused = "fixture-only" in str(exc)
        check("public explicit real runner is refused before provider call",
              direct_explicit_real_refused)
        check("fixture module exposes no real gate-owned lifecycle",
              not hasattr(executor_mod, "_dispatch_gate_owned_canary"))

        # The real child cannot reach Claude merely by being executed: an
        # inherited authority FD is mandatory and is consumed before the
        # delayed provider import/construction.
        child = os.path.join(ROOT, "bin_verify", "v2_real_executor_child.py")
        raw = subprocess.run([sys.executable, child, "--auth-fd", "0",
                              "--board-db", os.path.join(td, "unused-child.db"),
                              "--canary-task", "t_beefcafe",
                              "--workspace-root", os.path.join(td, "unused-workspace"),
                              "--session-binding", os.path.join(td, "unused-binding.json"),
                              "--cmux-receipt", os.path.join(td, "unused-receipt.json"),
                              "--reservation-json", os.path.join(td, "unused-reservation.json"),
                              "--binding-issuer", os.path.join(td, "unused-issuer.py"),
                              "--hermes-home", os.path.join(td, "unused-profile"),
                              "--lease-file", os.path.join(td, "unused-lease.json")],
                             text=True, capture_output=True)
        check("real child refuses missing private authority before Claude construction",
              raw.returncode != 0 and "authority" in (raw.stdout + raw.stderr))
        child_spec = importlib.util.spec_from_file_location("v2_real_child_wire", child)
        child_mod = importlib.util.module_from_spec(child_spec)
        child_spec.loader.exec_module(child_mod)
        lease = os.path.join(td, "wire-lease.json")
        open(lease, "w").write("consumed\n")
        expected = {"task_id": "t_beefcafe", "board_db": os.path.realpath(os.path.join(td, "wire.db")),
                    "workspace_root": os.path.realpath(os.path.join(td, "wire-workspace")),
                    "session_binding": os.path.realpath(os.path.join(td, "wire-binding.json")),
                    "cmux_receipt": os.path.realpath(os.path.join(td, "wire-receipt.json")),
                    "reservation_json": os.path.realpath(os.path.join(td, "wire-reservation.json")),
                    "binding_issuer": os.path.realpath(os.path.join(td, "wire-issuer.py")),
                    "hermes_home": os.path.realpath(os.path.join(td, "wire-home")),
                    "lease_file": os.path.realpath(lease)}
        rfd, wfd = os.pipe(); os.write(wfd, json.dumps({"grant_id": secrets.token_hex(32)}).encode()); os.close(wfd)
        try:
            child_mod._read_authority(rfd, expected, ""); forged_refused = False
        except child_mod.DispatchError:
            forged_refused = True
        check("self-forged FD grant is refused without verifier-owned authority", forged_refused)
        try:
            child_mod._run_child_lifecycle(board_db=expected["board_db"], canary_task="t_beefcafe", workspace_root=expected["workspace_root"], session_binding_path=expected["session_binding"], cmux_receipt_path=expected["cmux_receipt"], reservation_path=expected["reservation_json"], issuer_path=expected["binding_issuer"], hermes_home=expected["hermes_home"], runner=object())
            direct_child_refused = False
        except child_mod.DispatchError as exc:
            direct_child_refused = "executable-only" in str(exc)
        check("direct imported child lifecycle refuses before provider construction", direct_child_refused)



        # G3d is a provider boundary: it must refuse an otherwise-valid
        # governed invocation before a lease is minted or a stub is reached.
        no_home_args = green_args(td, board, name="green-no-home")
        home_index = no_home_args.index("--hermes-home")
        del no_home_args[home_index:home_index + 2]
        no_home_stub = make_stub_events(td, "stub-no-home")
        rc, rep = run_gate("--run", *no_home_args,
                           "--lease-file", os.path.join(td, "lease-no-home.json"),
                           "--stub-events-dir", no_home_stub)
        check("unset HERMES_HOME is refused before lease/provider dispatch (G3d)",
              rc == 5 and gate_status(rep, "G3d") is False
              and not os.path.exists(os.path.join(no_home_stub, ".test-only-v2-dispatch-lease.json"))
              and stub_calls(no_home_stub) == [])
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
                             "--reservation-json", reservation_path(td),
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
                     "--activation-packet", activation_packet(td),
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
        seed_unfolded_terminal_backlog(board_r, 201)
        run_stub = make_stub_events(td, "stub-run")
        wsroot = os.path.join(td, "wsroot")
        os.makedirs(wsroot)
        real_receipt, real_binding = make_cmux_binding_bundle(td, board_r, task=cid, name="real")
        real_home = os.path.join(td, "hermes-profile-real")
        os.makedirs(real_home)
        real_args = ["--canary-task", cid, "--board-db", board_r, "--packet-card", pkt,
                     "--lease-file", os.path.join(td, "lease.json"),
                     "--stop-file", os.path.join(td, "STOP"),
                     "--packet-verifier", stub_verifier_ok(td),
                     "--reservation-tool", stub_reservation_ok(td),
                     "--reservation-json", reservation_path(td),
                     "--cmux-receipt", real_receipt, "--session-binding", real_binding,
                     "--hermes-home", real_home,
                     "--activation-packet", activation_packet(td),
                     "--workspace-root", wsroot,
                     "--stub-events-dir", run_stub]
        rc, rep = run_gate("--run", *real_args)
        calls = stub_calls(run_stub)
        check("bounded historical completion backlog does not starve exact handoff fold",
              rc == 0 and rep["verdict"] == "DISPATCHED-ONCE")
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
              json.load(open(os.path.join(run_stub, ".test-only-v2-dispatch-lease.json")))["reusable"] is False)
        resume_argv = record.get("resume_argv", [])
        check("canonical resume argv rendered from persisted binding (claude --resume)",
              resume_argv[:2] == ["claude", "--resume"] and CMUX_SESSION in resume_argv)
        check("governed lifecycle passes the explicit profile home to runner",
              json.loads(calls[0].split(" ", 1)[1]).get("hermes_home") == real_home)
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
        race_home = os.path.join(td, "hermes-profile-race")
        os.makedirs(race_home)
        race_args = [sys.executable, GATE, "--json", "--run",
                     "--canary-task", cid_c, "--board-db", board_c, "--packet-card", pkt_c,
                     "--stub-events-dir", race_stub,
                     "--stop-file", os.path.join(td, "STOP"),
                     "--packet-verifier", stub_verifier_ok(td),
                     "--hermes-home", race_home,
                     "--activation-packet", activation_packet(td),
                     "--reservation-tool", stub_reservation_ok(td),
                     "--reservation-json", reservation_path(td),
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
        a3_home = os.path.join(td, "hermes-profile-a3")
        os.makedirs(a3_home)
        wsroot_a3 = os.path.join(td, "wsroot-a3")
        os.makedirs(wsroot_a3)
        a3_receipt, a3_binding = make_cmux_binding_bundle(td, board_a3, task=cid_a3, name="a3")
        rc, rep = run_gate("--run", "--canary-task", cid_a3, "--board-db", board_a3,
                           "--packet-card", pkt_a3,
                           "--hermes-home", a3_home,
                           "--activation-packet", activation_packet(td),
                           "--lease-file", os.path.join(td, "lease-a3.json"),
                           "--stop-file", os.path.join(td, "STOP"),
                           "--packet-verifier", stub_verifier_ok(td),
                           "--reservation-tool", stub_reservation_ok(td),
                           "--reservation-json", reservation_path(td),
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
        cl_home = os.path.join(td, "hermes-profile-claim")
        os.makedirs(cl_home)
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
                           "--hermes-home", cl_home,
                           "--activation-packet", activation_packet(td),
                           "--packet-verifier", stub_verifier_ok(td),
                           "--reservation-tool", stub_reservation_ok(td),
                           "--reservation-json", reservation_path(td),
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
