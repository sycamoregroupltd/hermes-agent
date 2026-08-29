#!/usr/bin/env python3
"""
cron_necromancer_sweep.py — deterministic dead-pin / script-missing / error-streak sweep.

Origin: fleet gap-hunt 2026-08-10 (t_8645b1fc). Owner: devops.
Runs as a NO_AGENT cron job in the TRADING-DEVOPS store (independent of the jarvis
store it guards). On each tick:

  1. Scans every profiles/*/cron/jobs.json for three fatal classes:
       - DEAD-PIN    : paused_reason contains 'dead-pin' (auto-paused on missing script)
       - SCRIPT-MISS : enabled job whose script does not resolve under the store
       - ERROR-STREAK: >=3 most recent executions in executions.db are failed/error
     (joined against the store's executions.db where present).
  2. Files ONE deduped kanban card per dead job on the jarvis-os board, assignee
     devops, using an idempotency key so re-runs never duplicate.
  3. Emits a compact daily digest to stdout, which the no_agent cron delivers to a
     verified channel (discord:#critical-alerts). SILENT when clean (watchdog
     pattern: empty stdout = nothing delivered).

Read-only with respect to cron stores: it never edits jobs.json or executions.db.
It only CREATES kanban cards (deduped) and prints a digest.
"""
import json, os, sqlite3, glob, subprocess, sys
from datetime import datetime, timezone

HERMES = "/home/frank/.hermes"
PROFILES = os.path.join(HERMES, "profiles")
BOARD = "jarvis-os"
ASSIGNEE = "devops"
STREAK_THRESHOLD = 3
HERMES_BIN = os.environ.get("HERMES_BIN", "/home/frank/.local/bin/hermes")

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def resolve_script(pdir, script):
    if not script:
        return True  # no script = agent job, skip
    if os.path.isabs(script):
        return os.path.isfile(script)
    if os.path.isfile(os.path.join(PROFILES, pdir, "scripts", script)):
        return True
    return bool(glob.glob(os.path.join(PROFILES, pdir, "scripts", "**", script), recursive=True))

def error_streak(pdir, jid):
    """Return (streak, last_error, last_at) or None."""
    db = os.path.join(PROFILES, pdir, "cron", "executions.db")
    if not os.path.isfile(db):
        return None
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        cur = con.cursor()
        rows = cur.execute(
            "SELECT status, error, claimed_at FROM executions WHERE job_id=? "
            "ORDER BY claimed_at DESC LIMIT 10", (jid,)).fetchall()
        con.close()
    except Exception:
        return None
    streak = 0
    last_err = None
    last_at = None
    for st, err, at in rows:
        s = (st or "").lower()
        if s in ("failed", "error"):
            streak += 1
            if last_err is None:
                last_err = (err or "")[:140]
                last_at = at
        elif s == "completed":
            break
        else:
            break
    if streak >= STREAK_THRESHOLD:
        return (streak, last_err, last_at)
    return None

def load_jobs(pdir):
    jp = os.path.join(PROFILES, pdir, "cron", "jobs.json")
    if not os.path.isfile(jp):
        return []
    try:
        d = json.load(open(jp))
    except Exception:
        return []
    jobs = d.get("jobs", d if isinstance(d, list) else [])
    return [j for j in jobs if isinstance(j, dict)]

def main():
    findings = []  # list of dicts
    for pdir in sorted(os.listdir(PROFILES)):
        if pdir.startswith("."):
            continue
        for j in load_jobs(pdir):
            jid = j.get("id")
            if not jid:
                continue
            name = j.get("name") or jid[:8]
            enabled = j.get("enabled", False)
            script = j.get("script") or ""
            paused = j.get("paused_reason") or j.get("paused") or ""
            cls = None
            detail = ""
            if isinstance(paused, str) and "dead-pin" in paused.lower():
                cls = "DEAD-PIN"
                detail = paused[:140]
            elif enabled and script and not resolve_script(pdir, script):
                cls = "SCRIPT-MISS"
                detail = f"script='{script}'"
            if cls:
                findings.append({
                    "cls": cls, "profile": pdir, "job": name, "id": jid,
                    "detail": detail, "enabled": enabled,
                })
                continue
            if enabled:
                es = error_streak(pdir, jid)
                if es:
                    streak, last_err, last_at = es
                    findings.append({
                        "cls": "ERROR-STREAK", "profile": pdir, "job": name,
                        "id": jid, "detail": f"streak={streak} last='{last_err}'",
                        "enabled": True, "last_at": last_at,
                    })

    # --- File deduped kanban cards on jarvis-os ---
    DRY = os.environ.get("NECRO_DRY_RUN") == "1"
    filed = []
    created = 0
    for f in findings:
        key = f"cron-necromancer:{f['profile']}:{f['id']}:{f['cls']}"
        title = f"[cron-necromancer] {f['cls']} {f['profile']}/{f['job']}"
        body = (
            f"Deterministic cron sweep {now_iso()} (t_8645b1fc).\n\n"
            f"class: {f['cls']}\nprofile: {f['profile']}\njob: {f['job']} "
            f"(id {f['id']})\nenabled: {f['enabled']}\ndetail: {f['detail']}\n\n"
            f"Owner: devops. Fix the script/pin or consciously retire the job, "
            f"then the sweep stops re-filing (dedup key {key})."
        )
        if DRY:
            filed.append((f["cls"], f["profile"], f["job"], "DRY", key))
            created += 1
            continue
        cmd = [HERMES_BIN, "kanban", "--board", BOARD, "create",
               "--assignee", ASSIGNEE, "--priority", "0",
               "--idempotency-key", key, "--body", body, title]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            out = (r.stdout or "").strip()
            # parse task id from JSON output (--json not passed; fall back to
            # scanning 'Created <id>' when output is plain text)
            tid = None
            if r.returncode == 0:
                try:
                    j = json.loads(out)
                    tid = j.get("id") or j.get("task_id")
                except Exception:
                    tid = None
                if not tid:
                    import re
                    m = re.search(r"Created (\w+)|created (\w+)", out)
                    if m:
                        tid = m.group(1) or m.group(2)
            filed.append((f["cls"], f["profile"], f["job"], r.returncode, tid))
            if r.returncode == 0 and tid:
                created += 1
        except Exception as e:
            filed.append((f["cls"], f["profile"], f["job"], -1, str(e)[:80]))

    # --- Digest to stdout (delivered to verified channel) ---
    if not findings:
        return  # silent watchdog

    lines = []
    lines.append(f"CRON-NECROMANCER SWEEP — {now_iso()}")
    lines.append(f"{len(findings)} dead job(s) across {BOARD} board")
    lines.append("---")
    for f in sorted(findings, key=lambda x: (x['cls'], x['profile'])):
        lines.append(f"[{f['cls']}] {f['profile']}/{f['job']} — {f['detail']}")
    lines.append("---")
    lines.append(f"kanban cards filed: {created} (deduped per idempotency key)")
    print("\n".join(lines))

if __name__ == "__main__":
    main()
