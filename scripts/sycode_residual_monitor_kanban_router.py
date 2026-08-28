#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies.
"""sycode_residual_monitor_kanban_router.py

Real delivery consumer for residual Sycode local-only monitors (t_dd27733b).

The Jul-2026 no-black-holes shard left three enabled local-only jobs with no
named delivery consumer (generic PM/review role names are not proof):

  * 45e0b154b41c  sycode-candle-per-symbol-freshness   (*/30)
  * 965b5d5d4cb4  sycode-trading PIT context-join validation  (daily 07:00)
  * 53d45f13ff65  Drift Monitor (hourly, quiet)

ea20e2bc47c2 signal-fusion-fill-rate-check is the Jul-5 fusion-deploy
acceptance probe. It is SUPERSEDED (standing fusion/journey monitors own
fill-rate now) and must stay paused — this router refuses to create cards
for it.

Consumer (already configured, no new channel):
  hermes kanban --board sycode-trading  (idempotent incident route)
  created_by = sycode-residual-monitor
  assignee   = trading-devops
  ledger     = ~/.hermes/state/sycode-residual-monitor-kanban-ledger.json

Contract:
  * healthy tick  -> silent (no card). Resolve lingering open cards if any.
  * breach tick   -> create one card per monitor key, or comment if open.
  * delivery fail -> exit 2 (never silent).
  * operational detector error is the caller's job to surface; this router
    still fail-visibles if *it* cannot deliver.

CLI:
  --selftest   offline FakeHarness, no hermes CLI / no live board
  --dry-run    print the action, no board/ledger writes
  stdin JSON   {monitor, healthy, findings, fingerprint?}
  env SYCODE_RESIDUAL_HEALTHY=1  treat as healthy even with empty findings
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

HERMES = os.environ.get("HERMES_BIN", "/home/frank/.local/bin/hermes")
BOARD = os.environ.get("SYCODE_RESIDUAL_KANBAN_BOARD", "sycode-trading")
ASSIGNEE = os.environ.get("SYCODE_RESIDUAL_KANBAN_ASSIGNEE", "trading-devops")
CREATED_BY = "sycode-residual-monitor"
LEDGER_PATH = Path(os.environ.get(
    "SYCODE_RESIDUAL_KANBAN_LEDGER",
    "/home/frank/.hermes/state/sycode-residual-monitor-kanban-ledger.json",
))
AUDIT_PATH = Path(os.environ.get(
    "SYCODE_RESIDUAL_KANBAN_AUDIT",
    "/home/frank/.hermes/state/sycode-residual-monitor-kanban-audit.jsonl",
))
WINDOW_DAYS = float(os.environ.get("SYCODE_RESIDUAL_WINDOW_DAYS", "7"))
PRIORITY = int(os.environ.get("SYCODE_RESIDUAL_KANBAN_PRIORITY", "40"))

OPEN_STATUSES = ("ready", "todo", "running", "blocked", "review")
ACTIVE_STATUSES = ("ready", "todo")
CLOSED_STATUSES = ("done", "archived")

# Exact residual jobs from t_2e548a59 / t_ae25e140 / t_db5637df.
MONITORS = {
    "candle-per-symbol-freshness": {
        "job_id": "45e0b154b41c",
        "name": "sycode-candle-per-symbol-freshness",
        "cadence": "*/30 * * * *",
        "detector": "sycode_candle_per_symbol_freshness.py",
        "handoff": (
            "hermes kanban --board sycode-trading create "
            "(idempotency_key=sycode-residual-45e0b154b41c) "
            "every 30m on breach; healthy ticks silent"
        ),
    },
    "pit-context-join": {
        "job_id": "965b5d5d4cb4",
        "name": "sycode-trading PIT context-join validation",
        "cadence": "0 7 * * *",
        "detector": "sycode_pit_context_join.py",
        "handoff": (
            "hermes kanban --board sycode-trading create "
            "(idempotency_key=sycode-residual-965b5d5d4cb4) "
            "daily 07:00 UTC on breach; healthy ticks silent"
        ),
    },
    "drift-monitor": {
        "job_id": "53d45f13ff65",
        "name": "Drift Monitor (hourly, quiet)",
        "cadence": "every 60m",
        "detector": "sycode-drift-monitor.sh",
        "handoff": (
            "hermes kanban --board sycode-trading create "
            "(idempotency_key=sycode-residual-53d45f13ff65) "
            "hourly on breach; healthy ticks silent"
        ),
    },
}

# Obsolete Jul-5 acceptance probe — do not route; keep paused.
SUPERSEDED = {
    "signal-fusion-fill-rate-check": {
        "job_id": "ea20e2bc47c2",
        "reason": (
            "Jul-5 2026 fusion-deploy acceptance probe "
            "(SERVER_DEPLOY_AT=2026-07-05T12:13:00Z). Superseded by standing "
            "fusion/journey monitors (sycode-journey-censor-monitor, "
            "gqt-fingerprint-lag-monitor, tier1-sample-gate). Native pause "
            "is the correct disposition; do not allowlist or re-enable."
        ),
    },
}

_TASKID_RE = re.compile(r"\b(t_[0-9a-f]{8,})\b")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def monitor_key(monitor: str) -> str:
    meta = MONITORS.get(monitor) or SUPERSEDED.get(monitor)
    job_id = (meta or {}).get("job_id", monitor)
    return f"sycode-residual-{job_id}"


def derive_key(monitor: str, findings: list[dict]) -> str:
    """Stable per-monitor key. Finding detail changes do not re-key."""
    base = monitor_key(monitor)
    if not findings:
        return base
    classes = sorted({str(f.get("class") or f.get("status") or "BREACH") for f in findings})
    return base + "-" + hashlib.md5("|".join(classes).encode("utf-8")).hexdigest()[:8]


def load_ledger(path: Path | None = None) -> dict:
    path = Path(path or LEDGER_PATH)
    if not path.exists():
        return {"version": 1, "entries": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "entries" not in data:
            data = {"version": 1, "entries": {}}
        data.setdefault("entries", {})
        data.setdefault("version", 1)
        return data
    except Exception:
        try:
            path.rename(path.with_suffix(path.suffix + ".corrupt"))
        except Exception:
            pass
        return {"version": 1, "entries": {}}


def save_ledger(ledger: dict, path: Path | None = None) -> None:
    path = Path(path or LEDGER_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(ledger, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def append_audit(action: str, payload: dict) -> None:
    path = Path(AUDIT_PATH)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = {"ts": utc_now_iso(), "action": action, **payload}
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, sort_keys=True) + "\n")
    except Exception:
        # Audit is best-effort; delivery failure is tracked by process_tick.
        pass


def _run_hermes(args: list[str], timeout: int = 30, attempts: int = 2,
                base_delay: float = 2.0) -> subprocess.CompletedProcess | None:
    import time
    env = os.environ.copy()
    env.setdefault("HERMES_HOME", os.environ.get("HERMES_HOME", "/home/frank/.hermes"))
    for attempt in range(1, attempts + 1):
        try:
            return subprocess.run(
                [HERMES, *args], capture_output=True, text=True, timeout=timeout, env=env,
            )
        except Exception:
            if attempt < attempts:
                time.sleep(base_delay * (2 ** (attempt - 1)))
                continue
            return None
    return None


def _extract_task_id(stdout: str) -> str | None:
    try:
        data = json.loads(stdout.strip())
        if isinstance(data, dict):
            return data.get("id") or data.get("task_id")
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return data[0].get("id") or data[0].get("task_id")
    except Exception:
        pass
    m = _TASKID_RE.search(stdout or "")
    return m.group(1) if m else None


def _board_db() -> Path:
    hermes_home = Path(os.environ.get("HERMES_HOME", "/home/frank/.hermes"))
    return hermes_home / "kanban" / "boards" / BOARD / "kanban.db"


def _card_status(task_id: str) -> str | None:
    db = _board_db()
    if not db.exists():
        return None
    import sqlite3
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
        row = con.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
        con.close()
        return row[0] if row else None
    except Exception:
        return None


def existing_open_card(key: str) -> str | None:
    db = _board_db()
    if not db.exists():
        return None
    import sqlite3
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
        placeholders = ",".join("?" * len(OPEN_STATUSES))
        row = con.execute(
            f"SELECT id FROM tasks WHERE idempotency_key=? AND created_by=? "
            f"AND status IN ({placeholders}) ORDER BY created_at DESC LIMIT 1",
            (key, CREATED_BY, *OPEN_STATUSES),
        ).fetchone()
        con.close()
        return row[0] if row else None
    except Exception:
        return None


def _format_findings(findings: list[dict]) -> str:
    if not findings:
        return "(no findings)"
    lines = []
    for f in findings:
        cls = f.get("class") or f.get("status") or "BREACH"
        detail = f.get("detail") or f.get("message") or json.dumps(f, sort_keys=True)
        lines.append(f"- {cls}: {detail}")
    return "\n".join(lines)


def create_card(monitor: str, key: str, findings: list[dict], fp: str = "") -> str | None:
    meta = MONITORS[monitor]
    title = f"MONITOR→ACTION: {meta['name']} breach (job {meta['job_id']})"
    body = "\n".join([
        f"Auto-routed by `{CREATED_BY}` (t_dd27733b). This residual Sycode "
        f"monitor was local-only (`deliver=local`) with no named consumer. "
        f"The already-configured sycode-trading kanban incident route is the "
        f"consumer. Generic PM/review role names are not proof of delivery.",
        "",
        f"Job: `{meta['job_id']}`  `{meta['name']}`",
        f"Cadence: `{meta['cadence']}`",
        f"Detector: `{meta['detector']}`",
        f"Handoff: {meta['handoff']}",
        f"Dedupe key: `{key}`  |  fingerprint: `{fp}`",
        f"Detected: {utc_now_iso()}",
        "",
        "Findings:",
        _format_findings(findings),
        "",
        "Acceptance: detector returns healthy; this card resolves; no live "
        "trading / deploy / credential mutation from the monitor path.",
    ])
    args = [
        "kanban", "--board", BOARD, "create", title,
        "--assignee", ASSIGNEE,
        "--priority", str(PRIORITY),
        "--created-by", CREATED_BY,
        "--idempotency-key", key,
        "--body", body,
        "--json",
    ]
    proc = _run_hermes(args)
    if proc is None or proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "") if proc else "subprocess timeout/exception"
        print(f"SYCODE_RESIDUAL_KANBAN_CREATE_FAIL key={key} err={err[:300]}", file=sys.stderr)
        return None
    tid = _extract_task_id(proc.stdout)
    if not tid:
        print(f"SYCODE_RESIDUAL_KANBAN_CREATE_FAIL key={key} no_id_in_output={proc.stdout[:300]}", file=sys.stderr)
        return None
    return tid


def append_comment(task_id: str, key: str, findings: list[dict], occurrence: int) -> bool:
    body = "\n".join([
        f"[{CREATED_BY} occurrence #{occurrence} @ {utc_now_iso()} — still UNHEALTHY]",
        "",
        "Current finding set:",
        _format_findings(findings),
    ])
    proc = _run_hermes(["kanban", "--board", BOARD, "comment", task_id, body])
    if proc is None or proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "") if proc else "timeout"
        print(f"SYCODE_RESIDUAL_KANBAN_COMMENT_FAIL task={task_id} err={err[:300]}", file=sys.stderr)
        return False
    return True


def resolve_card(task_id: str, key: str) -> bool:
    ts = utc_now_iso()
    body = (
        f"RESOLVED: residual monitor returned HEALTHY as of {ts}. "
        f"Key `{key}` is clean; healthy ticks stay silent after this."
    )
    proc = _run_hermes(["kanban", "--board", BOARD, "comment", task_id, body])
    if proc is None or proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "") if proc else "timeout"
        print(f"SYCODE_RESIDUAL_KANBAN_RESOLVE_COMMENT_FAIL task={task_id} err={err[:300]}", file=sys.stderr)
        return False
    status = _card_status(task_id)
    if status in ACTIVE_STATUSES:
        proc2 = _run_hermes([
            "kanban", "--board", BOARD, "complete", task_id,
            "--summary", f"{CREATED_BY} self-heal: key {key} HEALTHY @ {ts}.",
        ])
        if proc2 is None or proc2.returncode != 0:
            err = (proc2.stderr or proc2.stdout or "") if proc2 else "timeout"
            print(f"SYCODE_RESIDUAL_KANBAN_RESOLVE_COMPLETE_FAIL task={task_id} err={err[:300]}", file=sys.stderr)
            return False
    return True


def process_tick(*, monitor: str, healthy: bool, findings: list[dict],
                 fp: str = "", dry_run: bool = False) -> dict:
    if monitor in SUPERSEDED:
        return {
            "action": "superseded",
            "monitor": monitor,
            "job_id": SUPERSEDED[monitor]["job_id"],
            "reason": SUPERSEDED[monitor]["reason"],
        }
    if monitor not in MONITORS:
        return {"action": "unknown_monitor", "monitor": monitor}

    key = derive_key(monitor, findings)
    if dry_run:
        return {
            "action": "dry_run_healthy" if healthy else "dry_run_breach",
            "monitor": monitor,
            "key": key,
            "findings": len(findings),
            "handoff": MONITORS[monitor]["handoff"],
        }

    ledger = load_ledger()
    entries = ledger.setdefault("entries", {})
    now_iso = utc_now_iso()

    if healthy:
        resolved = []
        prefix = monitor_key(monitor)
        for ek, entry in list(entries.items()):
            if not str(ek).startswith(prefix) and entry.get("monitor") != monitor:
                continue
            tid = entry.get("task_id")
            if tid and not resolve_card(tid, ek):
                return {"action": "resolve_failed", "key": ek, "task_id": tid}
            if tid:
                resolved.append(tid)
            del entries[ek]
        if resolved:
            save_ledger(ledger)
            append_audit("resolved", {"monitor": monitor, "task_ids": resolved})
            return {"action": "resolved", "monitor": monitor, "task_ids": resolved}
        return {"action": "silent", "monitor": monitor}

    entry = entries.get(key)
    if entry is not None:
        tid = entry.get("task_id")
        cs = _card_status(tid) if tid else None
        if cs in CLOSED_STATUSES or cs is None:
            # Closed/missing: allow a fresh card (window recovery).
            del entries[key]
            save_ledger(ledger)
            entry = None
    if entry is None:
        existing = existing_open_card(key)
        if existing:
            entries[key] = {
                "task_id": existing, "key": key, "monitor": monitor,
                "assignee": ASSIGNEE, "board": BOARD,
                "first_seen": now_iso, "last_seen": now_iso, "occurrences": 1,
            }
            save_ledger(ledger)
            entry = entries[key]
    if entry is not None:
        entry["occurrences"] = int(entry.get("occurrences", 1)) + 1
        entry["last_seen"] = now_iso
        entry["last_fingerprint"] = fp
        save_ledger(ledger)
        tid = entry["task_id"]
        ok = append_comment(tid, key, findings, entry["occurrences"])
        if not ok:
            return {"action": "comment_failed", "key": key, "task_id": tid}
        append_audit("deduped", {"monitor": monitor, "key": key, "task_id": tid})
        return {"action": "deduped", "key": key, "task_id": tid,
                "occurrences": entry["occurrences"]}

    tid = create_card(monitor, key, findings, fp=fp)
    if tid is None:
        return {"action": "create_failed", "key": key}
    entries[key] = {
        "task_id": tid, "key": key, "monitor": monitor,
        "assignee": ASSIGNEE, "board": BOARD,
        "first_seen": now_iso, "last_seen": now_iso, "occurrences": 1,
        "fingerprint": fp,
    }
    save_ledger(ledger)
    append_audit("created", {"monitor": monitor, "key": key, "task_id": tid})
    return {"action": "created", "key": key, "task_id": tid, "occurrences": 1}


def _fake_run(args, created, commented, completed, statuses, fail_create=False):
    if "create" in args:
        if fail_create:
            return subprocess.CompletedProcess(args=args, returncode=1,
                                               stdout="", stderr="create denied")
        tid = f"t_selftest{len(created) + 1:04d}"
        created.append((args, tid))
        statuses.setdefault(tid, "ready")
        return subprocess.CompletedProcess(args=args, returncode=0,
                                           stdout=json.dumps({"id": tid}), stderr="")
    if "comment" in args:
        tid = args[4] if len(args) > 4 else "?"
        commented.append((tid, args[-1] if args else ""))
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
    if "complete" in args:
        idx = args.index("complete")
        tid = next((a for a in args[idx + 1:] if not a.startswith("--")), None)
        if tid:
            completed.append(tid)
            statuses[tid] = "done"
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
    return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")


def _selftest() -> int:
    failures: list[str] = []
    tmp = Path(tempfile.mkdtemp(prefix="sycode-residual-router-"))
    global LEDGER_PATH, AUDIT_PATH
    LEDGER_PATH = tmp / "ledger.json"
    AUDIT_PATH = tmp / "audit.jsonl"

    findings = [{"class": "ALERT_FLOOR", "detail": "candles[1D]: only 10/340 fresh"}]
    k1 = derive_key("candle-per-symbol-freshness", findings)
    k2 = derive_key("candle-per-symbol-freshness",
                    [{"class": "ALERT_FLOOR", "detail": "different wording"}])
    if k1 != k2:
        failures.append(f"key must be stable under detail-only change: {k1} != {k2}")
    k3 = derive_key("pit-context-join", findings)
    if k3 == k1:
        failures.append("different monitor must re-key")
    if not k1.startswith("sycode-residual-45e0b154b41c"):
        failures.append(f"candle key must name job id, got {k1}")

    created, commented, completed = [], [], []
    statuses: dict[str, str] = {}
    g = globals()
    g["_run_hermes"] = lambda args, **kw: _fake_run(args, created, commented, completed, statuses)
    g["_card_status"] = lambda tid: statuses.get(tid, "ready")
    g["existing_open_card"] = lambda key="x": None

    try:
        # 1. healthy silence
        res0 = process_tick(monitor="candle-per-symbol-freshness", healthy=True, findings=[])
        if res0.get("action") != "silent":
            failures.append(f"healthy should be silent, got {res0}")
        if created:
            failures.append(f"healthy must not create cards, created={created}")

        # 2. breach route
        res1 = process_tick(monitor="candle-per-symbol-freshness", healthy=False,
                            findings=findings, fp="fp1")
        if res1.get("action") != "created":
            failures.append(f"first breach should create, got {res1}")
        if len(created) != 1:
            failures.append(f"expected 1 create, got {len(created)}")

        # 3. dedupe
        res2 = process_tick(monitor="candle-per-symbol-freshness", healthy=False,
                            findings=findings, fp="fp1")
        if res2.get("action") != "deduped":
            failures.append(f"second breach should dedupe, got {res2}")
        if len(created) != 1:
            failures.append(f"dedupe must not create a second card, created={len(created)}")
        if len(commented) != 1:
            failures.append(f"expected 1 dedupe comment, got {len(commented)}")

        # 4. recovery
        res3 = process_tick(monitor="candle-per-symbol-freshness", healthy=True, findings=[])
        if res3.get("action") != "resolved":
            failures.append(f"healthy after breach should resolve, got {res3}")
        if len(completed) != 1:
            failures.append(f"expected 1 auto-complete, got {len(completed)}")

        # 5. delivery failure fail-visible
        created.clear()
        g["_run_hermes"] = lambda args, **kw: _fake_run(
            args, created, commented, completed, statuses, fail_create=True)
        res4 = process_tick(monitor="drift-monitor", healthy=False,
                            findings=[{"class": "DRIFT", "detail": "psi>0.2"}], fp="fp2")
        if res4.get("action") != "create_failed":
            failures.append(f"delivery failure should be create_failed, got {res4}")

        # 6. superseded fill-rate must not route
        res5 = process_tick(monitor="signal-fusion-fill-rate-check", healthy=False,
                            findings=[{"class": "ACCEPTANCE", "detail": "old"}])
        if res5.get("action") != "superseded":
            failures.append(f"fill-rate must be superseded, got {res5}")
        if res5.get("job_id") != "ea20e2bc47c2":
            failures.append(f"fill-rate job id mismatch: {res5}")
    finally:
        g.pop("_run_hermes", None)
        g.pop("_card_status", None)
        g.pop("existing_open_card", None)

    if failures:
        print("SELFTEST_FAIL")
        for fl in failures:
            print(" -", fl)
        return 1
    print("SELFTEST_PASS healthy_silent created=1 deduped=1 resolved=1 "
          "delivery_fail_visible fill_rate_superseded")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in argv:
        return _selftest()
    dry_run = "--dry-run" in argv
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    healthy_env = os.environ.get("SYCODE_RESIDUAL_HEALTHY", "")
    monitor = os.environ.get("SYCODE_RESIDUAL_MONITOR", "")
    findings: list[dict] = []
    fp = ""
    healthy = healthy_env == "1"
    if raw.strip():
        try:
            block = json.loads(raw)
        except Exception as exc:
            print(f"SYCODE_RESIDUAL_KANBAN_ROUTER bad-json: {exc}", file=sys.stderr)
            return 2
        if isinstance(block, dict):
            monitor = monitor or str(block.get("monitor") or "")
            if "healthy" in block:
                healthy = bool(block["healthy"])
            findings = list(block.get("findings") or [])
            fp = str(block.get("fingerprint") or "")
        elif isinstance(block, list):
            findings = block
    if not monitor:
        print("SYCODE_RESIDUAL_KANBAN_ROUTER missing monitor", file=sys.stderr)
        return 2
    if not fp and raw:
        fp = hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]
    try:
        result = process_tick(monitor=monitor, healthy=healthy, findings=findings,
                              fp=fp, dry_run=dry_run)
        print(f"SYCODE_RESIDUAL_KANBAN_ROUTER {json.dumps(result, sort_keys=True)}")
        if result.get("action") in {"create_failed", "comment_failed", "resolve_failed"}:
            return 2
        if result.get("action") == "unknown_monitor":
            return 2
    except Exception as exc:
        print(f"SYCODE_RESIDUAL_KANBAN_ROUTER_FAILURE {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
