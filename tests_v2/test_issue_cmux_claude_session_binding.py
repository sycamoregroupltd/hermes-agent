#!/usr/bin/env python3
"""Deterministic NO-PROVIDER tests for the CMUX Claude binding issuer.

All boards, receipts, reservations, artifacts, and worktrees are temporary.
The issuer has no CMUX/Claude/subprocess boundary; these tests exercise only
receipt parsing, reservation binding, read-only task verification, fingerprint
checks, expiry, and O_EXCL replay prevention.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
import sqlite3
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ISSUER_PATH = ROOT / "bin_verify" / "issue_cmux_claude_session_binding.py"
spec = importlib.util.spec_from_file_location("cmux_binding_issuer", ISSUER_PATH)
issuer = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(issuer)

FAILURES: list[str] = []
NOW = dt.datetime(2026, 7, 31, 12, 0, tzinfo=dt.timezone.utc)
TASK = "t_beefcafe"
SESSION = "1194f145-bc7d-4fd6-9762-16b4414eb4d1"
WORKSPACE = "9A3E7E93-963F-45AB-9A00-79E218190B5D"
SURFACE = "577E1920-C0EE-4140-A649-361647B6B9A5"
FOREIGN_SURFACE = "55555555-AAAA-BBBB-CCCC-000000000005"
# Stable CMUX refs, as written verbatim into ref-pinned reservations/receipts
# by the repaired mint; mint_control_context then carries the resolved raw tree id.
WORKSPACE_REF = "workspace:26"
SURFACE_REF = "surface:26"
NONCE_SHA256 = hashlib.sha256(b"CMUX-CALLER-PROOF-fixture").hexdigest()


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS" if ok else "FAIL") + ": " + name + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def iso(value: dt.datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def fixture(root: Path, *, workspace: str = WORKSPACE, surface: str = SURFACE,
            caller_surface: str | None = None,
            caller_workspace: str | None = None) -> tuple[Path, Path, Path, Path]:
    worktree = root / "worktree"
    (worktree / "bin_verify").mkdir(parents=True)
    (worktree / "bin_verify" / "mint_cmux_receipt.py").write_text("# marker\n")
    (worktree / "bin_verify" / "dispatch_gate_v2.py").write_text("# marker\n")
    reservation = {
        "record_kind": "cmux-manual-seat-reservation",
        "schema_version": 2,
        "seat": {
            "cmux_workspace_id": workspace,
            "cmux_surface_id": surface,
            "cmux_daemon_version": "0.64.20",
            "provider": "claude-code",
            "kind": "cmux-interactive-claude-max",
            "provider_session_uuid": SESSION,
        },
        "mint_control": {"cmux_workspace_id": workspace, "cmux_surface_id": surface},
    }
    reservation["reservation_fingerprint"] = issuer.reservation_fingerprint(reservation)
    reservation_path = root / "reservation.json"
    write_json(reservation_path, reservation)
    receipt = {
        "receipt_kind": "mac-cmux-reservation-receipt",
        "schema_version": 3,
        "minted_at_utc": iso(NOW - dt.timedelta(seconds=60)),
        "expires_at_utc": iso(NOW + dt.timedelta(seconds=300)),
        "canary_task": TASK,
        "cmux_workspace_id": workspace,
        "cmux_surface_id": surface,
        "mint_control_context": {
            "surface_id": caller_surface if caller_surface is not None else surface,
            "workspace_id": caller_workspace if caller_workspace is not None else workspace,
            "tty": "/dev/ttys012", "proof": "nonce-read-screen", "nonce_sha256": NONCE_SHA256,
        },
        "control_socket": {"bundle_identifier": "com.cmuxterm.app", "cmux_daemon_version": "0.64.20"},
    }
    receipt["receipt_fingerprint"] = issuer.receipt_fingerprint(receipt)
    receipt_path = root / "receipt.json"
    write_json(receipt_path, receipt)
    receipt_path = root / "receipt.json"
    write_json(receipt_path, receipt)
    board = root / "board.db"
    conn = sqlite3.connect(board)
    conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT)")
    conn.execute("CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT)")
    conn.execute("INSERT INTO tasks VALUES (?, 'blocked')", (TASK,))
    conn.commit()
    conn.close()
    return worktree, board, reservation_path, receipt_path


def invoke(worktree: Path, board: Path, reservation: Path, receipt: Path, **overrides) -> Path:
    values = {
        "worktree": worktree,
        "board_db": board,
        "reservation_path": reservation,
        "receipt_path": receipt,
        "task_id": TASK,
        "session_id": SESSION,
        "declared_by": "no-provider test operator",
        "ttl_seconds": 120,
        "output_rel": None,
        "now": NOW,
    }
    values.update(overrides)
    return issuer.issue_binding(**values)


def refuses(worktree: Path, board: Path, reservation: Path, receipt: Path, **overrides) -> str:
    try:
        invoke(worktree, board, reservation, receipt, **overrides)
    except issuer.Refuse as exc:
        return str(exc)
    return "DID_NOT_REFUSE"


def main() -> int:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        worktree, board, reservation, receipt = fixture(root)
        output = invoke(worktree, board, reservation, receipt)
        binding = json.loads(output.read_text())
        check("valid binding issues only to canonical artifact path", output == worktree / issuer.OUTPUT_RELATIVE_PATH)
        check("valid binding carries exact task, session, and CMUX seat",
              binding["task_id"] == TASK and binding["session_id"] == SESSION
              and binding["cmux_seat"]["workspace_id"] == WORKSPACE
              and binding["cmux_seat"]["surface_id"] == SURFACE)
        check("artifact fingerprint verifies", binding["artifact_fingerprint"] == issuer.artifact_fingerprint(binding))
        check("binding expiry is short and never later than receipt expiry",
              30 <= (dt.datetime.fromisoformat(binding["expires_at_utc"].replace("Z", "+00:00")) - NOW).total_seconds() <= 120)
        check("replay refuses O_EXCL replacement",
              "already exists" in refuses(worktree, board, reservation, receipt))

    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp); worktree, board, reservation, receipt = fixture(root)
        scoped = Path("reservation") / "task-artifacts" / TASK / "cmux-interactive-session-binding.json"
        output = invoke(worktree, board, reservation, receipt, output_rel=scoped)
        check("task-scoped override issues to exact named task artifact path", output == worktree / scoped)
        check("task-scoped override remains one-shot O_EXCL",
              "already exists" in refuses(worktree, board, reservation, receipt, output_rel=scoped))

    for unsafe in ("/tmp/binding.json", "../reservation/task-artifacts/t_beefcafe/cmux-interactive-session-binding.json",
                   "reservation/task-artifacts/t_deadbeef/cmux-interactive-session-binding.json",
                   "reservation/task-artifacts/t_beefcafe/../t_beefcafe/cmux-interactive-session-binding.json",
                   "reservation/task-artifacts/t_beefcafe/other.json"):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); worktree, board, reservation, receipt = fixture(root)
            check(f"unsafe binding override {unsafe!r} refuses",
                  refuses(worktree, board, reservation, receipt, output_rel=unsafe) != "DID_NOT_REFUSE")

    # Fresh fixtures per negative case avoid the expected one-shot artifact.
    cases = []
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp); worktree, board, reservation, receipt = fixture(root)
        cases.append(("wrong task refuses", refuses(worktree, board, reservation, receipt, task_id="t_deadbeef")))
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp); worktree, board, reservation, receipt = fixture(root)
        cases.append(("wrong session refuses", refuses(worktree, board, reservation, receipt,
                                                        session_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")))
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp); worktree, board, reservation, receipt = fixture(root)
        data = json.loads(receipt.read_text()); data["cmux_surface_id"] = "TAMPERED"; write_json(receipt, data)
        cases.append(("tampered receipt fingerprint refuses", refuses(worktree, board, reservation, receipt)))
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp); worktree, board, reservation, receipt = fixture(root)
        data = json.loads(reservation.read_text()); data["seat"]["cmux_workspace_id"] = "TAMPERED"; write_json(reservation, data)
        cases.append(("tampered reservation fingerprint refuses", refuses(worktree, board, reservation, receipt)))
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp); worktree, board, reservation, receipt = fixture(root)
        data = json.loads(receipt.read_text()); data["expires_at_utc"] = iso(NOW - dt.timedelta(seconds=1)); data["receipt_fingerprint"] = issuer.receipt_fingerprint(data); write_json(receipt, data)
        cases.append(("expired receipt refuses", refuses(worktree, board, reservation, receipt)))
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp); worktree, board, reservation, receipt = fixture(root)
        conn = sqlite3.connect(board); conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (TASK,)); conn.commit(); conn.close()
        cases.append(("non-blocked task refuses", refuses(worktree, board, reservation, receipt)))
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp); worktree, board, reservation, receipt = fixture(root)
        conn = sqlite3.connect(board); conn.execute("INSERT INTO task_runs (task_id) VALUES (?)", (TASK,)); conn.commit(); conn.close()
        cases.append(("task with existing run refuses", refuses(worktree, board, reservation, receipt)))

    # -- mint_control_context defence in depth (t_a6365be3) -------------------------
    # Every hostile receipt below is RE-SIGNED (fingerprint recomputed), so
    # only the caller-context re-check itself can refuse it.
    def resign(receipt_path: Path, mutate) -> None:
        data = json.loads(receipt_path.read_text())
        mutate(data)
        data["receipt_fingerprint"] = issuer.receipt_fingerprint(data)
        write_json(receipt_path, data)

    def caller_case(name: str, mutate) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); worktree, board, reservation, receipt = fixture(root)
            resign(receipt, mutate)
            cases.append((name, refuses(worktree, board, reservation, receipt)))

    caller_case("re-signed receipt with mint_control_context ABSENT refuses",
                lambda d: d.pop("mint_control_context"))
    caller_case("re-signed receipt with non-object mint_control_context refuses",
                lambda d: d.update(mint_control_context="proven, trust me"))
    caller_case("re-signed receipt with FOREIGN caller surface refuses",
                lambda d: d["mint_control_context"].update(surface_id=FOREIGN_SURFACE))
    caller_case("re-signed receipt with FOREIGN caller workspace refuses",
                lambda d: d["mint_control_context"].update(
                    workspace_id="44444444-AAAA-BBBB-CCCC-000000000004"))
    caller_case("re-signed receipt with wrong proof marker refuses",
                lambda d: d["mint_control_context"].update(proof="focus-inferred"))
    caller_case("re-signed receipt with missing proof marker refuses",
                lambda d: d["mint_control_context"].pop("proof"))
    caller_case("re-signed receipt with truncated nonce digest refuses",
                lambda d: d["mint_control_context"].update(nonce_sha256=NONCE_SHA256[:40]))
    caller_case("re-signed receipt with non-hex nonce digest refuses",
                lambda d: d["mint_control_context"].update(nonce_sha256="Z" * 64))
    caller_case("re-signed receipt with uppercase nonce digest refuses (not hexdigest form)",
                lambda d: d["mint_control_context"].update(nonce_sha256=NONCE_SHA256.upper()))
    caller_case("re-signed receipt with empty tty refuses",
                lambda d: d["mint_control_context"].update(tty="   "))
    caller_case("re-signed receipt with missing tty refuses",
                lambda d: d["mint_control_context"].pop("tty"))

    for name, result in cases:
        check(name, result != "DID_NOT_REFUSE", result)

    # -- stable-ref receipt compatibility (repaired mint normalizer) ----------
    # A ref-pinned reservation keeps workspace:26/surface:26 verbatim in the
    # reservation, receipt top level, and mint_control_context.workspace_id, while
    # mint_control_context.surface_id carries the raw tree id resolved on the Mac.
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        worktree, board, reservation, receipt = fixture(
            root, workspace=WORKSPACE_REF, surface=SURFACE_REF, caller_surface=SURFACE)
        output = invoke(worktree, board, reservation, receipt)
        binding = json.loads(output.read_text())
        check("stable-ref receipt with resolved caller tree id ISSUES",
              binding["cmux_seat"]["workspace_id"] == WORKSPACE_REF
              and binding["cmux_seat"]["surface_id"] == SURFACE_REF)
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        worktree, board, reservation, receipt = fixture(
            root, workspace=WORKSPACE_REF, surface=SURFACE_REF, caller_surface=SURFACE_REF)
        check("stable-ref receipt whose caller surface is the UNRESOLVED ref refuses",
              refuses(worktree, board, reservation, receipt) != "DID_NOT_REFUSE")
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        worktree, board, reservation, receipt = fixture(
            root, workspace=WORKSPACE_REF, surface=SURFACE_REF, caller_surface="TAMPERED")
        check("stable-ref receipt whose caller surface is not a raw tree id refuses",
              refuses(worktree, board, reservation, receipt) != "DID_NOT_REFUSE")
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        worktree, board, reservation, receipt = fixture(
            root, workspace=WORKSPACE_REF, surface=SURFACE_REF, caller_surface=SURFACE,
            caller_workspace="workspace:27")
        check("stable-ref receipt whose caller workspace is a different ref refuses",
              refuses(worktree, board, reservation, receipt) != "DID_NOT_REFUSE")
    # Static boundary check: the issuer's source must contain no provider/CMUX invocation transport.
    source = ISSUER_PATH.read_text(encoding="utf-8")
    check("issuer contains no subprocess/provider execution boundary", "subprocess" not in source and "Popen" not in source and "claude --" not in source)
    if FAILURES:
        print(f"FAILURES: {', '.join(FAILURES)}")
        return 1
    print("PASS: deterministic CMUX-to-Claude issuer contract; no provider invoked")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
