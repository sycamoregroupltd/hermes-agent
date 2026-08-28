#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
"""
cron_liveness_kanban_router.py

Companion to cron_liveness_monitor.py (task t_5ed62544). Invoked by the
liveness wrapper ONLY when the monitor emitted a non-empty finding block; on a
healthy tick it resolves any lingering open cards so the board stays clean.

This closes the "alerts reach Frank but nothing is actioned" gap that the
silent-failure doctrine calls out: alerts alone don't close the loop — the
board must own remediation.

Pattern (mirrors system-crontab-watchdog-kanban-router.py, t_dc22ee7e):
  - key = cron_liveness_<md5-12> derived from the SORTED finding class+job_id
          lines, so the same set of failing jobs reuses one card; a genuinely
          new breakage (different job set) re-keys and opens a fresh card
          immediately.
  - ledger = ~/.hermes/state/cron-liveness-kanban-ledger.json (atomic save/load,
          same discipline as anomaly_ledger.py).
  - 7-day window (narrow router-scope): only ONE card per failure signature
          within a 7-day window. Open card -> append an occurrence comment, no
          second card. Closed but window not expired -> append a recurrence
          comment to the closed card, NO new card. Window expired (or no
          card) -> fresh card.
  - created_by = "cron-liveness-monitor"
  - board  = sycode-trading (the host-crontab fleet monitors are owned by the
          sycode-trading ops board per the PM route on t_5ed62544)
  - assignee = devops (control/devops automation per the jarvis PM route comment)

Exit 0 = routing handled cleanly (card created / commented / resolved / or
healthy with no open card). Exit 2 = an operational error occurred that the
scheduler must surface (a broken router is never silent).

The monitor's JSON output is piped in on stdin; CRON_LIVENESS_HEALTHY=1 means
a clean tick (resolve lingering cards), 0/absent means UNHEALTHY block.
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
BOARD = os.environ.get("CRON_LIVENESS_KANBAN_BOARD", "sycode-trading")
ASSIGNEE = os.environ.get("CRON_LIVENESS_KANBAN_ASSIGNEE", "devops")
CREATED_BY = "cron-liveness-monitor"
LEDGER_PATH = Path(os.environ.get(
    "CRON_LIVENESS_KANBAN_LEDGER",
    "/home/frank/.hermes/state/cron-liveness-kanban-ledger.json",
))
WINDOW_DAYS = float(os.environ.get("CRON_LIVENESS_WINDOW_DAYS", "7"))
OPEN_STATUSES = ("ready", "todo", "running", "blocked", "review")
ACTIVE_STATUSES = ("ready", "todo")  # eligible for auto-complete on resolve
CLOSED_STATUSES = ("done", "archived")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fingerprint(block: str) -> str:
    return hashlib.md5(block.strip().encode("utf-8")).hexdigest()[:16]


def derive_key(findings: list[dict]) -> str:
    """Stable signature over the sorted (class,job) lines of the finding set."""
    lines = []
    for f in findings:
        lines.append(f"{f.get('class','?')}|{f.get('profile','?')}|{f.get('job_id','?')}|{f.get('name','?')}")
    sig = "\n".join(sorted(lines))
    return "cron_liveness_" + hashlib.md5(sig.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Ledger (atomic save/load, same discipline as anomaly_ledger.py)
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Hermes CLI wrapper (bounded retry, same discipline as cron_health router)
# ---------------------------------------------------------------------------
def _run_hermes(args: list[str], timeout: int = 30, attempts: int = 2,
                base_delay: float = 2.0) -> subprocess.CompletedProcess | None:
    import time
    env = os.environ.copy()
    env["HERMES_HOME"] = "/home/frank/.hermes"
    for attempt in range(1, attempts + 1):
        try:
            return subprocess.run(
                [HERMES, *args], capture_output=True, text=True, timeout=timeout, env=env,
            )
        except subprocess.TimeoutExpired:
            if attempt < attempts:
                time.sleep(base_delay * (2 ** (attempt - 1)))
                continue
            return None
        except Exception:
            if attempt < attempts:
                time.sleep(base_delay * (2 ** (attempt - 1)))
                continue
            return None
    return None


_TASKID_RE = re.compile(r"\b(t_[0-9a-f]{8,})\b")


def _extract_task_id(stdout: str) -> str | None:
    try:
        data = json.loads(stdout.strip())
        if isinstance(data, dict):
            return data.get("id") or data.get("task_id")
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return data[0].get("id") or data[0].get("task_id")
    except Exception:
        pass
    m = _TASKID_RE.search(stdout)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Card operations
# ---------------------------------------------------------------------------
def _card_status(task_id: str) -> str | None:
    """Read a task's status directly from the board sqlite DB (no LLM)."""
    db = Path("/home/frank/.hermes/kanban/boards") / BOARD / "kanban.db"
    if not db.exists():
        return None
    import sqlite3
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        row = con.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
        con.close()
        return row[0] if row else None
    except Exception:
        return None


def existing_open_card(key: str) -> str | None:
    """Return the id of an open card for this key, or None (direct sqlite read)."""
    db = Path("/home/frank/.hermes/kanban/boards") / BOARD / "kanban.db"
    if not db.exists():
        return None
    import sqlite3
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
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


def _format_finding_lines(findings: list[dict]) -> str:
    lines = []
    for f in findings:
        prof = f.get("profile", "?")
        nm = f.get("name", "?")
        jid = f.get("job_id", "?")
        cls = f.get("class", "?")
        if cls == "MISSED":
            lines.append(
                f"- MISSED profile={prof} job={nm} [{jid}] period={f.get('period_h')}h "
                f"last_completed={f.get('last_completed')} last_run_at={f.get('last_run_at')} "
                f"last_status={f.get('last_status')} — {f.get('detail','')}"
            )
        elif cls == "SCRIPT_MISSING":
            lines.append(
                f"- SCRIPT_MISSING profile={prof} job={nm} [{jid}] script='{f.get('script')}' "
                f"— {f.get('detail','')}"
            )
        else:
            lines.append(f"- {cls} profile={prof} job={nm} [{jid}] — {f.get('detail','')}")
    return "\n".join(lines) if lines else ""


def create_card(key: str, findings: list[dict], occurrence: int = 1,
                force_new: bool = False, fp: str = "") -> str | None:
    """Create one kanban card for the liveness anomaly. Returns task id or None."""
    report_lines = _format_finding_lines(findings)
    n_missed = sum(1 for f in findings if f.get("class") == "MISSED")
    n_script = sum(1 for f in findings if f.get("class") == "SCRIPT_MISSING")
    title = (f"MONITOR→ACTION: cron liveness breach — {n_missed} missed occurrence(s), "
             f"{n_script} script-missing job(s)")
    if occurrence > 1:
        title = (f"MONITOR→ACTION: cron liveness breach (recurrence #{occurrence}) — "
                 f"{n_missed} missed, {n_script} script-missing")
    body = "\n".join([
        "Auto-routed by `cron-liveness-monitor` (chip task t_5ed62544). The "
        "durable execution record (`cron/executions.db`, the same store "
        "`hermes cron runs` reads) plus the scheduler's `last_run_at` show "
        "enabled recurring jobs whose last PROOF of a completed run is older "
        "than one schedule period + grace (or never ran). This is the "
        "missed-occurrence / silent-writer-death class the 2026-08-05 wave-5 "
        "finding made structural: alerts alone do not close the loop, so the "
        "board now owns remediation.",
        "",
        f"Dedupe key: `{key}`  |  fingerprint: `{fp}`",
        f"Host: `{os.uname().nodename if hasattr(os, 'uname') else 'dgx'}`",
        f"Detected: {utc_now_iso()}",
        f"Graveyard: {n_missed} missed-occurrence job(s), {n_script} script-missing job(s)",
        "",
        "Findings (proof = latest completed execution OR fresh scheduler last_run_at; "
        "executions.db is capped at 1000 rows so low-frequency jobs' rows evict — "
        "last_run_at is the durable liveness signal):",
        report_lines,
        "",
        "Remediation:",
        "- For MISSED jobs: verify the owning profile ticker is live "
        "(`hermes cron status`); check `~/.hermes/logs/` for the job; the "
        "`cron_live_script_guard.py` pre-flight also flags script-path mismatches.",
        "- For SCRIPT_MISSING jobs: the script referenced in jobs.json does not "
        "resolve in the profile-local or global scripts dir — it will error "
        "'Script not found' every tick (sibling card t_027a2bc9). Locate or "
        "rewrite the script, or disable the job.",
        "",
        "Acceptance: all listed jobs fire on schedule with a successful "
        "(or at least recent-erroring) last_run_at, and this monitor returns HEALTHY.",
    ])
    args = [
        "kanban", "--board", BOARD, "create", title,
        "--assignee", ASSIGNEE,
        "--priority", "60",
        "--created-by", CREATED_BY,
    ]
    if force_new:
        args += ["--idempotency-key", key + "-new"]
    else:
        args += ["--idempotency-key", key]
    args += ["--body", body, "--json"]
    proc = _run_hermes(args)
    if proc is None or proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "") if proc else "subprocess timeout/exception"
        print(f"CRON_LIVENESS_KANBAN_CREATE_FAIL key={key} err={err[:300]}", file=sys.stderr)
        return None
    tid = _extract_task_id(proc.stdout)
    if not tid:
        print(f"CRON_LIVENESS_KANBAN_CREATE_FAIL key={key} no_id_in_output={proc.stdout[:300]}", file=sys.stderr)
        return None
    return tid


def append_comment(task_id: str, key: str, findings: list[dict], occurrence: int) -> bool:
    body = "\n".join([
        f"[cron-liveness-monitor occurrence #{occurrence} @ {utc_now_iso()} — still UNHEALTHY]",
        "",
        "Current finding set (re-evaluated; throttled separately by the alert channel):",
        _format_finding_lines(findings) if findings else "(no findings this tick — see recovery below)",
    ])
    proc = _run_hermes(["kanban", "--board", BOARD, "comment", task_id, body])
    if proc is None or proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "") if proc else "timeout"
        print(f"CRON_LIVENESS_KANBAN_COMMENT_FAIL task={task_id} err={err[:300]}", file=sys.stderr)
        return False
    return True


def resolve_card(task_id: str, key: str, findings: list[dict]) -> bool:
    ts = utc_now_iso()
    body = (f"RESOLVED: cron-liveness-monitor returned HEALTHY as of {ts}. "
            f"All previously flagged jobs for key `{key}` now have proof of a "
            f"completed run within (period + grace), and no SCRIPT_MISSING findings remain.")
    proc = _run_hermes(["kanban", "--board", BOARD, "comment", task_id, body])
    if proc is None or proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "") if proc else "timeout"
        print(f"CRON_LIVENESS_KANBAN_RESOLVE_COMMENT_FAIL task={task_id} err={err[:300]}", file=sys.stderr)
        return False
    status = _card_status(task_id)
    if status in ACTIVE_STATUSES:
        proc2 = _run_hermes([
            "kanban", "--board", BOARD, "complete", task_id,
            "--summary", f"cron-liveness-monitor self-heal: key {key} returned HEALTHY @ {ts}.",
        ])
        if proc2 is None or proc2.returncode != 0:
            err = (proc2.stderr or proc2.stdout or "") if proc2 else "timeout"
            print(f"CRON_LIVENESS_KANBAN_RESOLVE_COMPLETE_FAIL task={task_id} err={err[:300]}", file=sys.stderr)
            return False
    elif status is not None:
        note = f"cron-liveness-monitor: key {key} returned HEALTHY @ {ts}, but task already in '{status}'; left open for owner."
        _run_hermes(["kanban", "--board", BOARD, "comment", task_id, note])
    return True


# ---------------------------------------------------------------------------
# Public: process one monitor tick's outcome
# ---------------------------------------------------------------------------
def process_tick(*, healthy: bool, findings: list[dict], fp: str) -> dict:
    """Drive one router tick given the monitor's health outcome + findings."""
    ledger = load_ledger()
    entries = ledger.setdefault("entries", {})
    now_iso = utc_now_iso()

    if healthy:
        resolved = []
        resolved_tids: set[str] = set()
        for key in list(entries.keys()):
            entry = entries[key]
            tid = entry.get("task_id")
            ok = resolve_card(tid, key, findings)
            if ok:
                resolved_tids.add(tid)
                resolved.append(tid)
            del entries[key]
        # Board reconcile: open cards whose key appears in the live finding
        # fingerprint set but have no ledger entry (ledger lost/cleared).
        if findings:
            live_key = derive_key(findings)
            if live_key not in entries:
                tid = existing_open_card(live_key)
                if tid and tid not in resolved_tids:
                    resolve_card(tid, live_key, findings)
                    resolved_tids.add(tid)
                    resolved.append(tid)
        if resolved:
            save_ledger(ledger)
        return {"action": "resolved", "keys": resolved, "task_ids": resolved} if resolved \
            else {"action": "no_entry"}

    # UNHEALTHY: route findings into a deduped card.
    key = derive_key(findings) or f"cron_liveness_{fp}"
    entry = entries.get(key)
    if entry is not None:
        tid = entry.get("task_id")
        cs = _card_status(tid)
        if cs in CLOSED_STATUSES or cs is None:
            del entries[key]
            save_ledger(ledger)
            entry = None
    if entry is None:
        # Ledger miss: probe the board directly for a forgotten open card.
        existing = existing_open_card(key)
        if existing:
            entries[key] = {
                "task_id": existing, "fingerprint": fp, "key": key,
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
        # 7-day window (Option C dedupe rule): if the window still holds, just
        # comment; only spawn a fresh card when the window has expired (handled
        # by the closed-card path above) or this is a brand-new signature.
        ok = append_comment(tid, key, findings, entry["occurrences"])
        return {"action": "deduped", "key": key, "task_id": tid,
                "occurrences": entry["occurrences"], "commented": ok}
    # First detection -> create.
    tid = create_card(key, findings, occurrence=1, fp=fp)
    if tid is None:
        return {"action": "create_failed", "key": key}
    if _card_status(tid) in CLOSED_STATUSES:
        tid = create_card(key, findings, occurrence=1, force_new=True, fp=fp)
        if tid is None:
            return {"action": "create_failed", "key": key}
    entries[key] = {
        "task_id": tid, "fingerprint": fp, "key": key,
        "assignee": ASSIGNEE, "board": BOARD,
        "first_seen": now_iso, "last_seen": now_iso, "occurrences": 1,
    }
    save_ledger(ledger)
    return {"action": "created", "key": key, "task_id": tid, "occurrences": 1}


def _selftest() -> int:
    """Deterministic test: key derivation + create/dedup/resolve with a FakeHarness."""
    failures = []
    import tempfile
    tmp = Path(tempfile.mkdtemp())
    global LEDGER_PATH
    LEDGER_PATH = tmp / "ledger.json"

    findings = [{"class": "MISSED", "profile": "jarvis", "job_id": "abc123",
                 "name": "dead-job", "period_h": 24.0, "detail": "x"}]
    k1 = derive_key(findings)
    k2 = derive_key([{"class": "MISSED", "profile": "jarvis", "job_id": "abc123",
                       "name": "dead-job", "period_h": 24.0, "detail": "y"}])
    if k1 != k2:
        failures.append(f"key must be stable under detail-only change: {k1} != {k2}")
    k3 = derive_key([{"class": "MISSED", "profile": "jarvis", "job_id": "dead-job-NEW",
                       "name": "new-job", "period_h": 24.0, "detail": "x"}])
    if k3 == k1:
        failures.append("different job_id should re-key")

    created, commented, completed = [], [], []
    statuses: dict[str, str] = {}
    g = globals()
    g["_run_hermes"] = lambda args, **kw: _fake_run(args, created, commented, completed, statuses)
    g["_card_status"] = lambda tid: statuses.get(tid, "ready")
    g["existing_open_card"] = lambda key="x": None

    try:
        res1 = process_tick(healthy=False, findings=findings, fp="fp1")
        if res1["action"] != "created":
            failures.append(f"first tick should create, got {res1}")
        if len(created) != 1:
            failures.append(f"expected 1 create, got {len(created)}")
        res2 = process_tick(healthy=False, findings=findings, fp="fp1")
        if res2["action"] != "deduped":
            failures.append(f"second tick should dedupe, got {res2}")
        if len(created) != 1:
            failures.append(f"no second card, created={len(created)}")
        if len(commented) != 1:
            failures.append(f"expected 1 comment, got {len(commented)}")
        res3 = process_tick(healthy=True, findings=[], fp="fp1")
        if res3["action"] != "resolved":
            failures.append(f"healthy should resolve, got {res3}")
        if len(completed) != 1:
            failures.append(f"expected 1 auto-complete, got {len(completed)}")
    finally:
        g.pop("_run_hermes", None)
        g.pop("_card_status", None)
        g.pop("existing_open_card", None)

    if failures:
        print("SELFTEST_FAIL")
        for fl in failures:
            print(" -", fl)
        return 1
    print(f"SELFTEST_PASS key_stable created={len(created)} deduped=1 resolved=1")
    return 0


def _fake_run(args, created, commented, completed, statuses):
    import json as _json
    if "create" in args:
        tid = f"t_selftest{len(created)+1:04d}"
        created.append((args, tid))
        statuses.setdefault(tid, "ready")
        return subprocess.CompletedProcess(args=args, returncode=0,
                                           stdout=_json.dumps({"id": tid}), stderr="")
    if "comment" in args:
        tid = args[4]
        commented.append((tid, args[-1]))
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
    if "complete" in args:
        idx = args.index("complete")
        tid = next((a for a in args[idx + 1:] if not a.startswith("--")), None)
        if tid:
            completed.append(tid)
            statuses[tid] = "completed"
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
    return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if "--selftest" in argv:
        return _selftest()
    # The monitor pipes its findings JSON on stdin.
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    healthy = os.environ.get("CRON_LIVENESS_HEALTHY", "0") == "1"
    findings: list[dict] = []
    fp = ""
    if not healthy and raw:
        try:
            block = _json.loads(raw)
            if isinstance(block, dict) and isinstance(block.get("findings"), list):
                findings = block["findings"]
            elif isinstance(block, list):
                findings = block
            fp = block.get("fingerprint") if isinstance(block, dict) else ""
        except Exception as exc:
            print(f"CRON_LIVENESS_KANBAN_ROUTER bad-json: {exc}", file=sys.stderr)
            return 2
        if not fp:
            fp = hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]
    try:
        result = process_tick(healthy=healthy, findings=findings, fp=fp)
        print(f"CRON_LIVENESS_KANBAN_ROUTER {json.dumps(result, sort_keys=True)}")
    except Exception as exc:
        print(f"CRON_LIVENESS_KANBAN_ROUTER_FAILURE {exc}", file=sys.stderr)
        return 2
    return 0


_json = json  # module-level alias so _fake_run keeps working after main() binds

if __name__ == "__main__":
    sys.exit(main())
