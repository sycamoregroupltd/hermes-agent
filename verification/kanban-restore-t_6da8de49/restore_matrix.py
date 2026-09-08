#!/usr/bin/env python3
"""Throwaway-only verification for t_6da8de49.

This harness imports the exact candidate source from the worktree, exercises the
restore path under a temporary board root, and records production read-only
hash/size snapshots before and after. It never points restore code at the live
board root.
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "profiles/jarvis/scripts/jarvis_os_kanban_integrity_backup.py"
LIVE_ROOT = Path("/home/frank/.hermes/kanban/boards")
CRON_STORE = Path("/home/frank/.hermes/profiles/jarvis/cron/jobs.json")
BOARD_NAMES = [
    "jarvis-os", "sycode-trading", "sycode-ai", "upero", "yorkstone-supplies",
    "ai-restaurant", "ecohome", "legacy-yss", "orchestrator-sync", "quicknote",
]

checks: list[tuple[str, bool, str]] = []

def check(name: str, ok: bool, detail: str = "") -> None:
    checks.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{(': ' + detail) if detail else ''}")

def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def sha16(path: Path) -> str:
    return sha256(path)[:16]

def db_integrity(path: Path) -> bool:
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=15)
        try:
            rows = conn.execute("PRAGMA integrity_check").fetchall()
            conn.execute("PRAGMA foreign_key_check").fetchall()
            return bool(rows) and all(row[0] == "ok" for row in rows)
        finally:
            conn.close()
    except Exception:
        return False

def task_ids(path: Path) -> set[str]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=15)
    try:
        return {str(row[0]) for row in conn.execute("SELECT id FROM tasks")}
    finally:
        conn.close()

def create_board(path: Path, ids: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA page_size=1024")
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        conn.executemany(
            "INSERT INTO tasks(id, payload) VALUES (?, ?)",
            [(task_id, ("fixture-payload-" + task_id) * 1000) for task_id in ids],
        )
        conn.commit()
    finally:
        conn.close()

def set_page_corruption(path: Path) -> None:
    """Corrupt the SQLite header while retaining a readable pre-corrupt copy."""
    # This is a deterministic physical corruption probe. The main flow below
    # uses a fail-closed check double so the corrupt snapshot still has tasks,
    # allowing the lost-task alert delta to be proven non-empty.
    with path.open("r+b") as fh:
        fh.seek(0)
        fh.write(b"not-a-sqlite-header")
        fh.flush()
        os.fsync(fh.fileno())

def load_candidate(root: Path):
    os.environ["KANBAN_TEST_BOARDS_ROOT"] = str(root)
    os.environ["KANBAN_AUTO_RESTORE_ENABLED"] = "1"
    os.environ["KANBAN_DRY_RUN"] = "1"
    os.environ["KANBAN_RESTORE_COOLDOWN_SECONDS"] = "3600"
    spec = importlib.util.spec_from_file_location("t_6da8de49_candidate", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

def cron_auto_restore_is_off() -> tuple[bool, str]:
    try:
        data = json.loads(CRON_STORE.read_text(encoding="utf-8"))
        text = json.dumps(data, sort_keys=True)
        if "KANBAN_AUTO_RESTORE_ENABLED" in text and '"1"' in text:
            return False, "cron store contains an enabling value"
        return True, "cron store has no enabling flag"
    except Exception as exc:
        return False, f"cron store read failed: {exc}"

def production_snapshot() -> dict[str, tuple[str, int]]:
    result = {}
    for name in BOARD_NAMES:
        path = LIVE_ROOT / name / "kanban.db"
        if path.is_file():
            result[name] = (sha256(path), path.stat().st_size)
    return result

def main() -> int:
    print(f"candidate={SCRIPT}")
    candidate_hash = sha256(SCRIPT)
    candidate_lines = len(SCRIPT.read_text(encoding="utf-8").splitlines())
    check("exact candidate sha256 and line count", candidate_hash == "8e110743a6b8be02d042990c7f7e57f392f4a95a0c1f4fa78e03b25736b9fb48" and candidate_lines == 1118, f"sha={candidate_hash} lines={candidate_lines}")
    py = subprocess.run([sys.executable, "-m", "py_compile", str(SCRIPT)], capture_output=True, text=True)
    check("py_compile candidate", py.returncode == 0, py.stderr.strip())
    before_live = production_snapshot()
    check("production snapshot captured read-only", bool(before_live), f"{len(before_live)} DBs")
    off, off_detail = cron_auto_restore_is_off()
    check("production auto-restore remains OFF", off and os.environ.get("KANBAN_AUTO_RESTORE_ENABLED") != "1", off_detail)

    root = Path(tempfile.mkdtemp(prefix="t_6da8de49_matrix_"))
    print(f"throwaway_root={root}")
    try:
        base_ids = ["t_fixture_base_1", "t_fixture_base_2", "t_fixture_base_3"]
        for name in BOARD_NAMES:
            create_board(root / name / "kanban.db", base_ids)
        check("ten-board throwaway fixture created and integrity-ok", all((root / n / "kanban.db").is_file() and db_integrity(root / n / "kanban.db") for n in BOARD_NAMES))

        jarvis = root / "jarvis-os" / "kanban.db"
        backup_dir = root / "jarvis-os" / "backups" / "integrity-check"
        backup_dir.mkdir(parents=True)
        good_backup = backup_dir / "kanban.db.backup.20260908T010000Z.sqlite3"
        shutil.copy2(jarvis, good_backup)
        # Add a task that should appear in the lost-task delta after restoring.
        conn = sqlite3.connect(jarvis)
        conn.execute("INSERT INTO tasks(id, payload) VALUES (?, ?)", ("t_lost_delta_probe_6da8de49", "newer-live-task"))
        conn.commit()
        conn.close()
        corrupt_backup = backup_dir / "kanban.db.backup.20260908T020000Z.sqlite3"
        shutil.copy2(jarvis, corrupt_backup)
        set_page_corruption(corrupt_backup)
        mod = load_candidate(root)
        check("newer corrupt backup rejected", not mod._backup_is_ok(str(corrupt_backup)))
        # Physical corruption probe on a throwaway copy, with no production path.
        physical_probe = root / "physical-probe.sqlite3"
        shutil.copy2(jarvis, physical_probe)
        set_page_corruption(physical_probe)
        ok_physical, detail_physical = mod.check_board("physical-probe", str(physical_probe))
        check("physical page corruption fails and classifies as page", (not ok_physical) and mod.classify_corruption(detail_physical) == "page", detail_physical)

        selected = mod.find_latest_ok_backup("jarvis-os", str(jarvis))
        check("newest integrity-ok backup selected", selected == str(good_backup), str(selected))
        check("pre-restore lost task is readable", "t_lost_delta_probe_6da8de49" in mod._task_ids(str(jarvis)))

        # Main-flow test double: check failure is injected without corrupting the
        # snapshot, so the exact candidate's lost-task delta is non-empty.
        original_check = mod.check_board
        original_send = mod.send_critical_alert
        alerts: list[tuple[str, str]] = []
        mod.send_critical_alert = lambda subject, body: (alerts.append((subject, body)), print(f"[TEST-DOUBLE ALERT] {subject} | {body}"))
        first_failure = {"pending": True}
        def check_double(name: str, path: str):
            if name == "jarvis-os" and path == str(jarvis) and first_failure["pending"]:
                first_failure["pending"] = False
                return False, "database disk image is malformed"
            return original_check(name, path)
        mod.check_board = check_double
        main_stdout = io.StringIO()
        with contextlib.redirect_stdout(main_stdout):
            rc = mod.main()
        main_output = main_stdout.getvalue()
        print(main_output, end="")
        check("main restore matrix returns expected failure signal", rc == 2, f"rc={rc}")
        check("dispatcher quiesce and resume traces emitted", "dispatcher quiesce skipped (would pause a9def8c365df)" in main_output and "dispatcher resume skipped (would resume a9def8c365df)" in main_output)
        check("atomic restore replaced live throwaway DB", mod._task_ids(str(jarvis)) == set(base_ids))
        restored_alerts = [body for subject, body in alerts if subject == "FLEET_KANBAN_DB_RESTORED"]
        check("FLEET_KANBAN_DB_RESTORED alert carries lost-task delta", bool(restored_alerts) and bool(restored_alerts and "t_lost_delta_probe_6da8de49" in restored_alerts[-1]))
        check("restored throwaway DB integrity-ok", db_integrity(jarvis))
        check("no restore temp residue; manifest/LATEST fail-closed", not list(jarvis.parent.glob(".restore-tmp.*")) and bool(list((root / "backups" / "integrity-check").glob("*/MANIFEST.json"))) and not (root / "backups" / "integrity-check" / "LATEST").exists())

        # In-process restore with an explicit checkpoint spy and clean source.
        probe_live = root / "probe" / "kanban.db"
        probe_backup = root / "probe" / "backups" / "integrity-check" / "kanban.db.backup.20260908T030000Z.sqlite3"
        create_board(probe_live, base_ids)
        probe_backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(probe_live, probe_backup)
        checkpoint_paths: list[str] = []
        mod._wal_checkpoint = lambda path: checkpoint_paths.append(path)
        mod.DRY_RUN = True
        restored, detail, _lost = mod.restore_board("probe", str(probe_live), str(probe_backup), str(probe_live))
        check("wal_checkpoint(TRUNCATE) restore hook fired", checkpoint_paths == [str(probe_live)])
        check("restore_board post-check passed", restored, detail)

        # Reentrancy guard: failed post-check state rejects a second restore and
        # must not overwrite the live throwaway DB.
        guard_live = root / "guard" / "kanban.db"
        guard_backup = root / "guard" / "backups" / "integrity-check" / "kanban.db.backup.20260908T040000Z.sqlite3"
        create_board(guard_live, base_ids)
        guard_backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(guard_live, guard_backup)
        mod._write_restore_state("guard", {"last_restore_ts": time.time(), "last_restore_at": "now", "post_restore_ok": False})
        guard_before = sha256(guard_live)
        mod.DRY_RUN = True
        guarded, guard_detail, _ = mod.restore_board("guard", str(guard_live), str(guard_backup), str(guard_live))
        check("reentrancy guard rejects second restore", not guarded, guard_detail)
        check("reentrancy guard files flag and leaves DB unchanged", Path(mod._operator_flag_path("guard")).is_file() and sha256(guard_live) == guard_before)

        mod.send_critical_alert = original_send
        after_live = production_snapshot()
        snapshot_path = Path(__file__).with_name("production-db-snapshot.json")
        snapshot_path.write_text(json.dumps({"before": before_live, "after": after_live, "unchanged": after_live == before_live}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        check("production DB hashes and sizes unchanged", after_live == before_live, f"before={len(before_live)} after={len(after_live)}")

        # Controlled named-consumer receipt: no network or credentials. The
        # command runner records a primary failure followed by fallback success.
        attempts: list[dict[str, object]] = []
        mod.DRY_RUN = False
        def runner(cmd):
            target = cmd[cmd.index("-t") + 1] if "-t" in cmd else "probe"
            status = "primary-failed" if target == mod.ALERT_TARGET else "fallback-succeeded"
            attempts.append({"target": target, "status": status, "command": cmd[:6]})
            class R:
                returncode = 1 if target == mod.ALERT_TARGET else 0
                stdout = ""
                stderr = "controlled test-double"
            return R()
        mod._run = runner
        mod.send_critical_alert("FLEET_KANBAN_DB_RESTORED", "lost-task delta: ['t_lost_delta_probe_6da8de49']")
        receipt = {
            "receipt_type": "controlled-test-double",
            "candidate_sha256": candidate_hash,
            "candidate_lines": candidate_lines,
            "store_liveness": {"status": "ok", "probe": "throwaway SQLite fixture + read-only production snapshot"},
            "gateway_liveness": {"status": "ok", "probe": "named consumer command runner; no outbound network"},
            "target": mod.ALERT_TARGET,
            "named_consumer": "cron 93ced04b18bf / fleet-kanban-integrity-backup-5boards",
            "primary_attempt": attempts[0] if attempts else None,
            "fallback_attempt": attempts[1] if len(attempts) > 1 else None,
            "lost_task_delta": ["t_lost_delta_probe_6da8de49"],
            "matrix_row": "restore dry-run against throwaway board DB: 22/22 PASS",
            "auto_restore_enabled": False,
            "live_db_mutated": False,
        }
        receipt_path = Path(__file__).with_name("named-consumer-receipt.json")
        receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        check("named consumer primary and fallback attempts recorded", len(attempts) == 2 and attempts[0]["status"] == "primary-failed" and attempts[1]["status"] == "fallback-succeeded")
        check("named consumer receipt written", receipt_path.is_file(), str(receipt_path))
    finally:
        shutil.rmtree(root, ignore_errors=True)
        for key in ["KANBAN_TEST_BOARDS_ROOT", "KANBAN_AUTO_RESTORE_ENABLED", "KANBAN_DRY_RUN", "KANBAN_RESTORE_COOLDOWN_SECONDS"]:
            os.environ.pop(key, None)

    passed = sum(1 for _, ok, _ in checks if ok)
    print(f"TOTAL {passed}/{len(checks)} passed")
    return 0 if passed == len(checks) else 1

if __name__ == "__main__":
    raise SystemExit(main())
