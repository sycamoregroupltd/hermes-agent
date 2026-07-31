#!/usr/bin/env python3
"""Deterministic NO-PROVIDER tests for the CMUX Claude binding issuer.

All boards, receipts, reservations, artifacts, and worktrees are temporary.
The issuer has no CMUX/Claude/subprocess boundary; these tests exercise only
receipt parsing, reservation binding, read-only task verification, fingerprint
checks, expiry, and O_EXCL replay prevention.
"""

from __future__ import annotations

import datetime as dt
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


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS" if ok else "FAIL") + ": " + name + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def iso(value: dt.datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def fixture(root: Path) -> tuple[Path, Path, Path, Path]:
    worktree = root / "worktree"
    (worktree / "bin_verify").mkdir(parents=True)
    (worktree / "bin_verify" / "mint_cmux_receipt.py").write_text("# marker\n")
    (worktree / "bin_verify" / "dispatch_gate_v2.py").write_text("# marker\n")
    reservation = {
        "record_kind": "cmux-manual-seat-reservation",
        "schema_version": 1,
        "seat": {
            "cmux_workspace_id": WORKSPACE,
            "cmux_surface_id": SURFACE,
            "cmux_daemon_version": "0.64.20",
            "provider": "claude-code",
            "kind": "cmux-interactive-claude-max",
            "provider_session_uuid": SESSION,
        },
    }
    reservation["reservation_fingerprint"] = issuer.reservation_fingerprint(reservation)
    reservation_path = root / "reservation.json"
    write_json(reservation_path, reservation)
    receipt = {
        "receipt_kind": "mac-cmux-reservation-receipt",
        "schema_version": 2,
        "minted_at_utc": iso(NOW - dt.timedelta(seconds=60)),
        "expires_at_utc": iso(NOW + dt.timedelta(seconds=300)),
        "canary_task": TASK,
        "cmux_workspace_id": WORKSPACE,
        "cmux_surface_id": SURFACE,
        "control_socket": {"bundle_identifier": "com.cmuxterm.app", "cmux_daemon_version": "0.64.20"},
    }
    receipt["receipt_fingerprint"] = issuer.receipt_fingerprint(receipt)
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

    for name, result in cases:
        check(name, result != "DID_NOT_REFUSE", result)
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
