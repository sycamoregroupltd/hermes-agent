#!/usr/bin/env python3
"""INDEPENDENT adversarial NO-PROVIDER tests for the CMUX->Claude binding slice.

Second pair of eyes over bin_verify/issue_cmux_claude_session_binding.py and
its consumer (v2_canary_executor.load_session_binding / dispatch_gate_v2 G3c),
written without reusing the slice's own fixtures or assertions. Nothing here
invokes Claude or any provider, touches a CMUX socket, runs ssh, dispatches,
or writes to the live board: every fixture lives in a temp dir and the live
canonical artifacts are hash-proven untouched at the end.

Two kinds of result are reported and they are NOT the same thing:

  PASS/FAIL  a guarantee the slice claims, verified or broken.
  FINDING    behaviour that is not a claimed guarantee but that a reviewer
             should decide about. Findings do not fail the run; they are
             printed, counted, and carried into the evidence file.

Run: python3 tests_v2/test_cmux_binding_independent_adversarial.py
"""

import ast
import datetime as dt
import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ISSUER = os.path.join(ROOT, "bin_verify", "issue_cmux_claude_session_binding.py")
GATE = os.path.join(ROOT, "bin_verify", "dispatch_gate_v2.py")
MINT = os.path.join(ROOT, "bin_verify", "mint_cmux_receipt.py")
EXECUTOR = os.path.join(ROOT, "bin_verify", "v2_canary_executor.py")
LIVE_RESERVATION = ("/home/frank/.hermes/kanban/boards/jarvis-os/workspaces/"
                    "t_d7e6c034/reservation/seat-reservation.json")

FAILURES = []
FINDINGS = []


def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL") + f": {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def finding(name, observed, note):
    print(f"FINDING: {name} — {note}")
    FINDINGS.append({"finding": name, "observed": observed, "note": note})


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


sys.path.insert(0, os.path.join(ROOT, "bin_verify"))
issuer = load_module(ISSUER, "issue_cmux_claude_session_binding")
gate_mod = load_module(GATE, "dispatch_gate_v2")
mint_mod = load_module(MINT, "mint_cmux_receipt")
v2ce = load_module(EXECUTOR, "v2_canary_executor")

WS = "AAAAAAAA-1111-2222-3333-000000000001"
SURFACE = "BBBBBBBB-1111-2222-3333-000000000002"
WINDOW = "CCCCCCCC-1111-2222-3333-000000000003"
FOREIGN_SURFACE = "EEEEEEEE-1111-2222-3333-000000000005"
SESSION = "1194f145-bc7d-4fd6-9762-16b4414eb4d1"
OTHER_SESSION = "99999999-8888-7777-6666-555555555555"
TASK = "t_e5fd6f1b"
OTHER_TASK = "t_deadbeef"
DAEMON = "0.64.20"

from pathlib import Path  # noqa: E402  (after module loading, deliberately)


def now():
    return dt.datetime.now(dt.timezone.utc)


def iso(moment):
    return moment.isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------------------
# independent fixtures
# --------------------------------------------------------------------------
def write_reservation(path, *, surface=SURFACE, session=SESSION, version=DAEMON,
                      kind="cmux-manual-seat-reservation", seat_kind="cmux-interactive-claude-max",
                      tamper=False):
    res = {
        "record_kind": kind,
        "schema_version": 1,
        "identity": {"session_bus_id": "claude-cmux-t_d7e6c034"},
        "seat": {"cmux_workspace_id": WS, "cmux_surface_id": surface,
                 "cmux_window_id": WINDOW, "cmux_daemon_version": version,
                 "kind": seat_kind, "provider": "claude-code",
                 "provider_session_uuid": session},
        "task": {"board": "jarvis-os", "task_id": "t_d7e6c034", "required_status": "blocked"},
    }
    res["reservation_fingerprint"] = issuer.reservation_fingerprint(res)
    if tamper:
        res["seat"]["cmux_surface_id"] = FOREIGN_SURFACE
    Path(path).write_text(issuer.canonical_json(res), encoding="utf-8")
    return path


def write_receipt(path, *, task=TASK, surface=SURFACE, caller_surface=None, version=DAEMON,
                  minted_ago=60, ttl=600, workspace=WS):
    minted = now() - dt.timedelta(seconds=minted_ago)
    rec = {
        "receipt_kind": "mac-cmux-reservation-receipt",
        "schema_version": 2,
        "minted_on": "mac-cmux-control-socket",
        "minted_at_utc": iso(minted),
        "expires_at_utc": iso(minted + dt.timedelta(seconds=ttl)),
        "canary_task": task,
        "cmux_workspace_id": workspace,
        "cmux_surface_id": surface,
        "caller_context": {"surface_id": caller_surface or surface, "workspace_id": workspace,
                           "tty": "/dev/ttys999", "proof": "nonce-read-screen",
                           "nonce_sha256": hashlib.sha256(str(path).encode()).hexdigest()},
        "control_socket": {"socket_path": "/Users/fixture/.local/state/cmux/cmux-501.sock",
                           "bundle_identifier": "com.cmuxterm.app",
                           "cmux_daemon_version": version},
    }
    rec["receipt_fingerprint"] = issuer.receipt_fingerprint(rec)
    Path(path).write_text(json.dumps(rec, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def write_board(path, *, task=TASK, status="blocked", runs=0):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, assignee TEXT)")
    con.execute("CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT)")
    con.execute("insert into tasks values (?,?,NULL)", (task, status))
    for _ in range(runs):
        con.execute("insert into task_runs (task_id) values (?)", (task,))
    con.commit()
    con.close()
    return path


class Env:
    """One isolated declared worktree with the markers the issuer requires."""

    def __init__(self, td, label, *, issuer_copy=False, **kw):
        self.root = Path(td) / label
        (self.root / "bin_verify").mkdir(parents=True)
        for marker in ("mint_cmux_receipt.py", "dispatch_gate_v2.py"):
            (self.root / "bin_verify" / marker).write_text("# marker\n")
        self.issuer_path = ISSUER
        if issuer_copy:
            self.issuer_path = str(self.root / "bin_verify" / "issuer_copy.py")
            shutil.copy(ISSUER, self.issuer_path)
        self.reservation = write_reservation(self.root / "seat-reservation.json",
                                             **kw.pop("reservation", {}))
        self.receipt = write_receipt(self.root / "receipt.json", **kw.pop("receipt", {}))
        self.board = write_board(str(self.root / "board.db"), **kw.pop("board", {}))
        self.binding = self.root / issuer.OUTPUT_RELATIVE_PATH

    def issue(self, *, task=TASK, session=SESSION, ttl=300, by="independent-verifier", **kw):
        return issuer.issue_binding(
            worktree=self.root, board_db=Path(self.board),
            reservation_path=Path(self.reservation), receipt_path=Path(self.receipt),
            task_id=task, session_id=session, declared_by=by, ttl_seconds=ttl, **kw)

    def load(self, *, task=TASK, path=None, board=None, receipt=None, reservation=None,
             issuer_path=None, now_=None):
        return v2ce.load_session_binding(
            str(path or self.binding), expected_task_id=task,
            board_db=board or self.board, cmux_receipt_path=receipt or self.receipt,
            reservation_path=reservation or self.reservation,
            issuer_path=issuer_path or self.issuer_path, now=now_)

    def resign(self, mutate):
        rec = json.loads(Path(self.binding).read_text())
        mutate(rec)
        rec.pop("artifact_fingerprint", None)
        rec["artifact_fingerprint"] = issuer.artifact_fingerprint(rec)
        Path(self.binding).write_text(issuer.canonical_json(rec), encoding="utf-8")
        return rec


def refuses(fn, *a, **kw):
    """Returns (refused, message). Any Refuse/DispatchError counts as closed."""
    try:
        fn(*a, **kw)
        return False, "ACCEPTED"
    except (issuer.Refuse, v2ce.DispatchError) as exc:
        return True, str(exc)


def main():
    live_before = (hashlib.sha256(open(LIVE_RESERVATION, "rb").read()).hexdigest()
                   if os.path.isfile(LIVE_RESERVATION) else None)
    canonical_binding = os.path.join(ROOT, "reservation", "cmux-interactive-session-binding.json")
    # The canonical binding path may legitimately hold a PRE-EXISTING legacy
    # binding artifact (earlier live evidence). This suite only ever writes
    # under its own temp dirs, so the hermetic guarantee it can make is that
    # the path is UNCHANGED by the run: absent stays absent, a pre-existing
    # artifact stays byte-identical. It is snapshotted here, before any
    # fixture runs, and re-checked in A13.
    binding_before = (hashlib.sha256(open(canonical_binding, "rb").read()).hexdigest()
                      if os.path.isfile(canonical_binding) else None)
    if binding_before is not None:
        print(f"NOTE: pre-existing legacy binding artifact at {canonical_binding} "
              f"(sha256:{binding_before}) — preserved and re-verified byte-identical in A13; "
              "its existence is prior evidence, not something this suite created or waives")

    with tempfile.TemporaryDirectory() as td:
        # ------------------------------------------------------------- A1
        print("\n== A1. cross-module contract drift canary ==")
        probe = {"receipt_kind": "mac-cmux-reservation-receipt", "canary_task": TASK,
                 "nested": {"b": 1, "a": [1, 2]}, "text": "x y, z: w"}
        check("mint / gate / issuer agree on the receipt fingerprint form",
              mint_mod.receipt_fingerprint(probe) == gate_mod.receipt_fingerprint(probe)
              == issuer.receipt_fingerprint(probe))
        res_probe = {"record_kind": "cmux-manual-seat-reservation", "seat": {"a": 1}, "z": "q r"}
        check("mint and issuer agree on the reservation fingerprint form",
              mint_mod.reservation_fingerprint(res_probe) == issuer.reservation_fingerprint(res_probe))
        check("executor consumes the issuer's own fingerprint/kind constants (no second copy)",
              v2ce.SESSION_BINDING_KIND is issuer.BINDING_KIND
              and v2ce.cmux_binding.artifact_fingerprint is issuer.artifact_fingerprint)

        # ------------------------------------------------------------- A2
        print("\n== A2. happy path, independently re-derived ==")
        e = Env(td, "happy")
        path = e.issue()
        rec = json.loads(Path(path).read_text())
        check("artifact is task-, seat- and session-bound",
              rec["task_id"] == TASK and rec["session_id"] == SESSION
              and rec["cmux_seat"]["workspace_id"] == WS
              and rec["cmux_seat"]["surface_id"] == SURFACE)
        check("fingerprint re-derives independently over every field",
              rec["artifact_fingerprint"] == issuer.artifact_fingerprint(rec))
        receipt = json.loads(Path(e.receipt).read_text())
        check("expiry is short and never exceeds the Mac receipt window",
              0 < (issuer.parse_utc(rec["expires_at_utc"], "e")
                   - issuer.parse_utc(rec["issued_at_utc"], "i")).total_seconds()
              <= issuer.MAX_BINDING_TTL_SECONDS
              and issuer.parse_utc(rec["expires_at_utc"], "e")
              <= issuer.parse_utc(receipt["expires_at_utc"], "r"))
        check("artifact is written owner-read-only (0600)",
              stat.S_IMODE(os.stat(path).st_mode) == 0o600)
        check("the real consumer accepts it with full context",
              e.load()["session_id"] == SESSION)

        # ------------------------------------------------------------- A3
        print("\n== A3. the consumer fails closed on missing validation context ==")
        full = {"expected_task_id": TASK, "board_db": e.board, "cmux_receipt_path": e.receipt,
                "reservation_path": e.reservation, "issuer_path": e.issuer_path}
        for missing in full:
            ctx = dict(full, **{missing: None})
            closed, msg = refuses(v2ce.load_session_binding, str(e.binding), **ctx)
            check(f"consumer refuses when {missing} context is absent", closed, msg[:120])
        check("consumer accepts only with the complete context",
              v2ce.load_session_binding(str(e.binding), **full)["task_id"] == TASK)

        # ------------------------------------------------------------- A4
        print("\n== A4. tampering ==")
        e4 = Env(td, "tamper")
        e4.issue()
        raw = json.loads(Path(e4.binding).read_text())
        raw["session_id"] = OTHER_SESSION
        Path(e4.binding).write_text(json.dumps(raw), encoding="utf-8")
        closed, msg = refuses(e4.load)
        check("edited field with a stale fingerprint refuses", closed and "fingerprint" in msg, msg[:150])
        e4.resign(lambda r: r.__setitem__("session_id", OTHER_SESSION))
        closed, msg = refuses(e4.load)
        check("attacker who RE-SIGNS a foreign session still refuses (seat anchor)",
              closed, msg[:150])
        e4.resign(lambda r: r["cmux_seat"].__setitem__("surface_id", FOREIGN_SURFACE))
        closed, msg = refuses(e4.load)
        check("re-signed foreign seat refuses", closed, msg[:150])

        # ------------------------------------------------------------- A5
        print("\n== A5. mismatched task / board / seat / receipt ==")
        e5 = Env(td, "mismatch")
        e5.issue()
        closed, msg = refuses(e5.load, task=OTHER_TASK)
        check("binding presented for a different task refuses", closed, msg[:120])
        other_board = write_board(str(Path(td) / "other-board.db"))
        closed, msg = refuses(e5.load, board=other_board)
        check("binding presented against a different board refuses", closed, msg[:120])
        drifted = write_reservation(Path(td) / "drifted-reservation.json", surface=FOREIGN_SURFACE)
        closed, msg = refuses(e5.load, reservation=drifted)
        check("seat drift after issuance refuses", closed, msg[:120])
        swapped = write_receipt(Path(td) / "swapped-receipt.json")
        closed, msg = refuses(e5.load, receipt=swapped)
        check("re-anchoring to a different receipt refuses", closed, msg[:120])

        # ------------------------------------------------------------- A6
        print("\n== A6. expiry is real, and cannot be stretched past the receipt ==")
        e6 = Env(td, "expiry")
        e6.issue()
        rec6 = json.loads(Path(e6.binding).read_text())
        closed, msg = refuses(e6.load, now_=issuer.parse_utc(rec6["expires_at_utc"], "e")
                              + dt.timedelta(seconds=1))
        check("an expired binding refuses", closed and "EXPIRED" in msg, msg[:150])
        e6.resign(lambda r: r.__setitem__("expires_at_utc", iso(now() + dt.timedelta(hours=2))))
        closed, msg = refuses(e6.load)
        check("a re-signed binding that outlives its receipt refuses", closed, msg[:150])
        e6b = Env(td, "expiry-b")
        e6b.issue()
        e6b.resign(lambda r: r.__setitem__("issued_at_utc", iso(now() + dt.timedelta(minutes=5))))
        closed, msg = refuses(e6b.load)
        check("a future-dated binding refuses", closed, msg[:150])

        # ------------------------------------------------------------- A7
        print("\n== A7. issuance preconditions ==")
        e7 = Env(td, "pre")
        for ttl, label in ((29, "below the floor"), (601, "above the ceiling"),
                           (True, "boolean"), ("300", "non-integer")):
            closed, msg = refuses(e7.issue, ttl=ttl)
            check(f"ttl {label} refuses", closed, msg[:120])
        closed, msg = refuses(e7.issue, by="   ")
        check("blank declared_by refuses", closed, msg[:120])
        closed, msg = refuses(e7.issue, by="x" * 201)
        check("over-long declared_by refuses", closed, msg[:120])
        closed, msg = refuses(e7.issue, session="not-a-uuid")
        check("malformed session id refuses", closed, msg[:120])
        closed, msg = refuses(e7.issue, task="T_BADCASE")
        check("malformed task id refuses", closed, msg[:120])
        closed, msg = refuses(e7.issue, session=OTHER_SESSION)
        check("a session other than the reserved provider session refuses", closed, msg[:120])

        e7b = Env(td, "pre-board", board={"status": "ready"})
        closed, msg = refuses(e7b.issue)
        check("a non-blocked task refuses", closed, msg[:120])
        e7c = Env(td, "pre-runs", board={"runs": 1})
        closed, msg = refuses(e7c.issue)
        check("a task that already has runs refuses", closed, msg[:120])
        e7d = Env(td, "pre-missing-task")
        closed, msg = refuses(e7d.issue, task=OTHER_TASK)
        check("a task absent from the board refuses", closed, msg[:120])
        e7e = Env(td, "pre-marker")
        os.remove(e7e.root / "bin_verify" / "dispatch_gate_v2.py")
        closed, msg = refuses(e7e.issue)
        check("a worktree missing its canonical markers refuses", closed, msg[:120])

        # ------------------------------------------------------------- A8
        print("\n== A8. receipt/reservation anchor rules ==")
        for label, kw in (("expired receipt", {"receipt": {"minted_ago": 900, "ttl": 600}}),
                          ("receipt for another task", {"receipt": {"task": OTHER_TASK}}),
                          ("receipt naming a foreign surface", {"receipt": {"surface": FOREIGN_SURFACE}}),
                          ("receipt with an over-long window", {"receipt": {"minted_ago": 0, "ttl": 5000}}),
                          ("receipt off the daemon version pin", {"receipt": {"version": "0.63.0"}}),
                          ("tampered reservation", {"reservation": {"tamper": True}}),
                          ("wrong reservation record_kind", {"reservation": {"kind": "other"}}),
                          ("reservation that is not the interactive Claude seat",
                           {"reservation": {"seat_kind": "cmux-plain"}})):
            env = Env(td, "anchor-" + label.replace(" ", "-"), **kw)
            closed, msg = refuses(env.issue)
            check(f"{label} refuses", closed, msg[:120])

        e8 = Env(td, "short-receipt", receipt={"minted_ago": 585, "ttl": 600})
        closed, msg = refuses(e8.issue, ttl=300)
        check("a receipt with less life left than the minimum window refuses", closed, msg[:120])
        e8b = Env(td, "clamp", receipt={"minted_ago": 540, "ttl": 600})
        p8b = e8b.issue(ttl=600)
        rec8b = json.loads(Path(p8b).read_text())
        check("binding expiry is clamped to the receipt, never extended",
              rec8b["expires_at_utc"] == json.loads(Path(e8b.receipt).read_text())["expires_at_utc"])

        # ------------------------------------------------------------- A9
        print("\n== A9. issuer-source pinning ==")
        e9 = Env(td, "pin", issuer_copy=True)
        e9.issue()
        check("consumer accepts when the pinned issuer source is byte-identical",
              e9.load()["task_id"] == TASK)
        with open(e9.issuer_path, "a") as fh:
            fh.write("\n# adversarial edit\n")
        closed, msg = refuses(e9.load)
        check("a modified issuer source invalidates the binding", closed, msg[:150])

        # ------------------------------------------------------------ A10
        print("\n== A10. single use, retirement and replay ==")
        e10 = Env(td, "replay")
        e10.issue()
        closed, msg = refuses(e10.issue)
        check("a second issuance at the same path refuses (O_EXCL)", closed, msg[:120])
        binding10 = e10.load()
        check("retirement marker is created once",
              v2ce.retire_session_binding_artifact(str(e10.binding), binding10) is True)
        check("retiring twice refuses (no race, no double retire)",
              v2ce.retire_session_binding_artifact(str(e10.binding), binding10) is False)
        closed, msg = refuses(e10.load)
        check("a retired binding cannot be replayed", closed and "retired" in msg, msg[:150])

        # replay probes: not claimed guarantees, reported as findings
        os.remove(e10.binding)
        reissued_after_delete, msg = refuses(e10.issue)
        if not reissued_after_delete:
            check("deleting a RETIRED binding does not resurrect it (marker still bites)",
                  refuses(e10.load)[0])
            finding("re-issue after deleting the artifact",
                    "issuer ACCEPTS a fresh binding from the same still-valid receipt",
                    "the O_EXCL path lock is the only single-use guard at ISSUE time; deleting "
                    "the artifact lets one Mac receipt fund a second binding. The consumer is "
                    "still protected at the canonical path by the .retired.json marker, so this "
                    "is bounded — but a receipt is not itself spend-once.")

        e10b = Env(td, "replay-copy")
        e10b.issue()
        shutil.copy(e10b.receipt, Path(td) / "shared-receipt.json")
        e10c = Env(td, "replay-copy-2")
        shutil.copy(Path(td) / "shared-receipt.json", e10c.receipt)
        shutil.copy(e10b.reservation, e10c.reservation)
        second_path_closed, msg = refuses(e10c.issue)
        if not second_path_closed:
            finding("same receipt in a second declared worktree",
                    "issuer ACCEPTS a second binding for the same task/session/receipt",
                    "single-use is per output path, not per receipt: two declared worktrees (or "
                    "one moved aside) yield two live bindings from one Mac mint. Each is still "
                    "task/seat/expiry bound and the v2 lease is one-shot, so dispatch stays "
                    "single, but the receipt is replayable as an authorisation token.")
        else:
            check("a second declared worktree cannot reuse the receipt", True)

        # ------------------------------------------------------------ A11
        print("\n== A11. receipt caller context (defence in depth) ==")
        e11 = Env(td, "caller", receipt={"caller_surface": FOREIGN_SURFACE})
        caller_closed, msg = refuses(e11.issue)
        if caller_closed:
            check("a receipt minted from a foreign caller surface refuses", True)
        else:
            finding("receipt caller_context is not re-checked on DGX",
                    "issuer ACCEPTS a receipt whose caller_context.surface_id is foreign",
                    "mint C9 already refuses to mint such a receipt at the Mac control terminal, "
                    "so this is defence in depth rather than a live hole; the DGX side treats the "
                    "receipt as data and never re-asserts the caller equals the reserved surface.")

        # ------------------------------------------------------------ A12
        print("\n== A12. path containment and inertness ==")
        e12 = Env(td, "escape")
        outside = Path(td) / "outside-reservation"
        outside.mkdir()
        (e12.root / "reservation").symlink_to(outside)
        closed, msg = refuses(e12.issue)
        check("a reservation directory symlinked outside the worktree refuses", closed, msg[:150])

        src = Path(ISSUER).read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)) \
                    and ast.get_docstring(node):
                node.body = node.body[1:]
        code_only = ast.unparse(tree)
        forbidden = ["subprocess", "Popen", "socket.socket", "import socket", "urllib", "requests",
                     "httpx", "os.system", "os.exec", "os.spawn", "pty", "os.fork", "asyncio",
                     "crontab", "systemctl", "ANTHROPIC", "API_KEY", "getenv", "os.environ",
                     "--resume", "--continue", "--dangerously"]
        hits = [tok for tok in forbidden if tok in code_only]
        check("issuer code holds no provider/execution/credential/environment token", not hits, str(hits))
        writes = [tok for tok in ("insert into", "update ", "delete from", "create table")
                  if tok in code_only.lower()]
        check("issuer issues no SQL writes (read-only board access only)",
              not writes and "mode=ro" in code_only, str(writes))

        # ------------------------------------------------------------ A13
        print("\n== A13. live artifacts untouched ==")
        binding_after = (hashlib.sha256(open(canonical_binding, "rb").read()).hexdigest()
                         if os.path.isfile(canonical_binding) else None)
        check("canonical binding path unchanged by this suite "
              "(absent stays absent; pre-existing legacy artifact stays byte-identical)",
              binding_before == binding_after,
              f"before={binding_before} after={binding_after}")
        live_after = (hashlib.sha256(open(LIVE_RESERVATION, "rb").read()).hexdigest()
                      if os.path.isfile(LIVE_RESERVATION) else None)
        check("live seat reservation byte-identical after the whole suite", live_before == live_after)

    print()
    for item in FINDINGS:
        print(f"FINDING RECORDED: {item['finding']} — {item['observed']}")
    if FAILURES:
        print(f"RESULT: {len(FAILURES)} FAILURE(S): {FAILURES}")
        return 1
    print(f"RESULT: ALL INDEPENDENT ADVERSARIAL CHECKS PASS ({len(FINDINGS)} finding(s) recorded)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
