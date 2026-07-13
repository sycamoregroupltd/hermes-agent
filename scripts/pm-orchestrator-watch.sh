#!/usr/bin/env bash
set -euo pipefail
HERMES=${HERMES:-/home/frank/.local/bin/hermes}
BASE=/home/frank/.hermes/kanban/boards
OUT=/home/frank/uaa-rules/PM-ORCHESTRATOR-STATUS.md
STATE=/home/frank/.hermes/state/pm-orchestrator-watch-state.json
CRON_STORE=/home/frank/.hermes/cron/jobs.json
WATCH_JOB_ID=${WATCH_JOB_ID:-e4b4b732339d}
STALE_AFTER_SECONDS=${STALE_AFTER_SECONDS:-4500}
TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
mkdir -p "$(dirname "$STATE")"
python3 - "$BASE" "$OUT" "$HERMES" "$TS" "$STATE" "$CRON_STORE" "$WATCH_JOB_ID" "$STALE_AFTER_SECONDS" <<'PY'
import datetime as dt
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys

base, out, hermes, ts, state_path, cron_store, watch_job_id, stale_after_raw = sys.argv[1:]
stale_after = int(stale_after_raw)
now = dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)

profiles = {"default"}
profdir = "/home/frank/.hermes/profiles"
if os.path.isdir(profdir):
    profiles.update(x for x in os.listdir(profdir) if os.path.isdir(os.path.join(profdir, x)))

actions = []
failures = []
dispatch_log = []
warnings = []
rows = []


def parse_iso(value):
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(dt.timezone.utc)
    except Exception:
        return None


def run(cmd, timeout=60, board=None, kind=None):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception as exc:
        failures.append({
            "board": board,
            "kind": kind or "command",
            "cmd": " ".join(shlex.quote(x) for x in cmd),
            "error": repr(exc),
        })
        return None
    if p.returncode != 0:
        failures.append({
            "board": board,
            "kind": kind or "command",
            "cmd": " ".join(shlex.quote(x) for x in cmd),
            "rc": p.returncode,
            "err": (p.stderr or p.stdout)[-500:],
        })
    return p


def previous_artifact_age():
    if not os.path.exists(out):
        return None
    try:
        text = open(out, "r", encoding="utf-8").read(2000)
    except Exception:
        return None
    match = re.search(r"^Updated:\s*(\S+)", text, re.M)
    if not match:
        return None
    previous = parse_iso(match.group(1))
    if not previous:
        return None
    return max(0, int((now - previous).total_seconds()))

previous_age = previous_artifact_age()
if previous_age is None:
    warnings.append("previous PM artifact had no parseable Updated timestamp")
elif previous_age > stale_after:
    warnings.append(f"PM artifact was stale before refresh: age={previous_age // 60}m threshold={stale_after // 60}m")

watch_job = None
if os.path.exists(cron_store):
    try:
        raw = json.load(open(cron_store, "r", encoding="utf-8"))
        jobs = raw.get("jobs", raw if isinstance(raw, list) else [])
        for job in jobs:
            if job.get("id") == watch_job_id:
                watch_job = job
                break
    except Exception as exc:
        warnings.append(f"could not read cron store {cron_store}: {exc!r}")
else:
    warnings.append(f"cron store missing: {cron_store}")

cron_health = {"id": watch_job_id, "found": bool(watch_job)}
if watch_job:
    cron_health.update({
        "name": watch_job.get("name"),
        "enabled": bool(watch_job.get("enabled", True)) and not watch_job.get("paused_at"),
        "schedule": watch_job.get("schedule_display") or (watch_job.get("schedule") or {}).get("display"),
        "no_agent": bool(watch_job.get("no_agent")),
        "script": watch_job.get("script"),
        "last_run_at": watch_job.get("last_run_at"),
        "last_status": watch_job.get("last_status"),
        "last_error": watch_job.get("last_error"),
        "last_delivery_error": watch_job.get("last_delivery_error"),
        "next_run_at": watch_job.get("next_run_at"),
        "store": cron_store,
    })
    last_run = parse_iso(watch_job.get("last_run_at"))
    next_run = parse_iso(watch_job.get("next_run_at"))
    if not cron_health["enabled"]:
        warnings.append(f"watchdog cron {watch_job_id} is disabled or paused")
    if not cron_health["no_agent"]:
        warnings.append(f"watchdog cron {watch_job_id} is not no-agent")
    if watch_job.get("script") != "pm-orchestrator-watch.sh":
        warnings.append(f"watchdog cron {watch_job_id} script is {watch_job.get('script')!r}")
    if watch_job.get("last_status") not in (None, "ok"):
        warnings.append(f"watchdog cron {watch_job_id} last_status={watch_job.get('last_status')}")
    if watch_job.get("last_error"):
        warnings.append(f"watchdog cron {watch_job_id} last_error present")
    if watch_job.get("last_delivery_error"):
        warnings.append(f"watchdog cron {watch_job_id} delivery error present")
    if last_run and (now - last_run).total_seconds() > stale_after:
        warnings.append(f"watchdog cron {watch_job_id} last_run stale: age={int((now - last_run).total_seconds()) // 60}m threshold={stale_after // 60}m")
    if next_run and next_run < now - dt.timedelta(minutes=10):
        warnings.append(f"watchdog cron {watch_job_id} next_run is overdue: {watch_job.get('next_run_at')}")
else:
    warnings.append(f"watchdog cron {watch_job_id} not found in {cron_store}")

if os.path.isdir(base):
    for slug in sorted(os.listdir(base)):
        db = os.path.join(base, slug, "kanban.db")
        if not os.path.exists(db):
            continue
        try:
            con = sqlite3.connect(db)
            con.row_factory = sqlite3.Row
            cols = [r["name"] for r in con.execute("pragma table_info(tasks)")]
            status = {r["status"]: r["n"] for r in con.execute("select status,count(*) n from tasks group by status")}
            pm = None
            if "assignee" in cols:
                pms = [r[0] for r in con.execute("select distinct assignee from tasks where assignee like '%-pm' order by assignee") if r[0]]
                if pms:
                    pm = pms[0]
            if not pm and f"{slug}-pm" in profiles:
                pm = f"{slug}-pm"

            done7d = 0
            if "completed_at" in cols:
                done7d = con.execute("select count(*) from tasks where status='done' and completed_at > CAST(strftime('%s','now','-7 days') AS INTEGER)").fetchone()[0]

            orphan_rows = []
            if "assignee" in cols:
                orphan_rows = [dict(r) for r in con.execute("select id,title,status from tasks where status in ('ready','todo') and (assignee is null or assignee='') order by created_at limit 30")]
            for orphan in orphan_rows:
                if not pm:
                    warnings.append(f"{slug}/{orphan['id']} is unassigned {orphan['status']} but no PM owner was inferred")
                    continue
                comment = run([hermes, "kanban", "--board", slug, "comment", orphan["id"], f"delegated: PM watchdog routed inert {orphan['status']} work to {pm}; dispatcher cannot claim unassigned cards."], board=slug, kind="comment")
                assign = run([hermes, "kanban", "--board", slug, "assign", orphan["id"], pm], board=slug, kind="assign")
                if comment and assign and comment.returncode == 0 and assign.returncode == 0:
                    actions.append(f"{slug}/{orphan['id']}: assigned orphan {orphan['status']} to {pm}")

            ready_assigned = 0
            if "assignee" in cols:
                ready_assigned = con.execute("select count(*) from tasks where status='ready' and assignee is not null and assignee<>''").fetchone()[0]
            if ready_assigned:
                dispatched = run([hermes, "kanban", "--board", slug, "dispatch"], timeout=120, board=slug, kind="dispatch")
                entry = {"board": slug, "ready_assigned_before": ready_assigned, "rc": None, "summary": "not run"}
                if dispatched:
                    interesting = []
                    for line in (dispatched.stdout or "").splitlines():
                        if line.startswith(("Spawned:", "Deferred", "Skipped", "Claimed", "No ")):
                            interesting.append(line.strip())
                    entry.update({"rc": dispatched.returncode, "summary": "; ".join(interesting)[:500] or (dispatched.stdout or dispatched.stderr)[-500:]})
                    if dispatched.returncode == 0 and interesting:
                        actions.append(f"{slug}: dispatch {entry['summary']}")
                dispatch_log.append(entry)

            con.close()
            con = sqlite3.connect(db)
            con.row_factory = sqlite3.Row
            status = {r["status"]: r["n"] for r in con.execute("select status,count(*) n from tasks group by status")}
            orphan_count = con.execute("select count(*) from tasks where status in ('ready','todo') and (assignee is null or assignee='')").fetchone()[0] if "assignee" in cols else 0
            rows.append({
                "board": slug,
                "pm": pm or "-",
                "ready": int(status.get("ready", 0)),
                "todo": int(status.get("todo", 0)),
                "running": int(status.get("running", 0)),
                "blocked": int(status.get("blocked", 0)),
                "done7d": int(done7d),
                "orphan_ready_todo": int(orphan_count),
                "all_status_counts": dict(sorted(status.items())),
            })
            con.close()
        except Exception as exc:
            failures.append({"board": slug, "error": repr(exc)})
else:
    failures.append({"error": f"board base missing: {base}"})

snapshot = {
    "rows": rows,
    "warnings": sorted(warnings),
    "failures": failures,
    "cron_health": cron_health,
}
previous_snapshot = None
if os.path.exists(state_path):
    try:
        previous_snapshot = json.load(open(state_path, "r", encoding="utf-8"))
    except Exception:
        previous_snapshot = None
material_change = previous_snapshot != snapshot

lines = []
lines.append("# PM Orchestrator Status\n")
lines.append(f"Updated: {ts}\n")
lines.append("Freshness SLO: refreshed at least every 75 minutes for the 30-minute no-agent watchdog.\n")
lines.append("Sources: /home/frank/.hermes/kanban/boards/*/kanban.db, /home/frank/.hermes/profiles/*, /home/frank/.hermes/cron/jobs.json, `hermes kanban --board <slug> dispatch`.\n")
lines.append("\n## Board Activity\n")
lines.append("| Board | PM owner | Ready | Todo | Running | Blocked | Done 7d | Orphan ready/todo |\n")
lines.append("|---|---:|---:|---:|---:|---:|---:|---:|\n")
for row in rows:
    lines.append(f"| {row['board']} | {row['pm']} | {row['ready']} | {row['todo']} | {row['running']} | {row['blocked']} | {row['done7d']} | {row['orphan_ready_todo']} |\n")

lines.append("\n## Full Status Counts\n")
for row in rows:
    lines.append(f"- {row['board']}: {json.dumps(row['all_status_counts'], sort_keys=True)}\n")

lines.append("\n## Dispatch Action Log\n")
if actions:
    for action in actions:
        lines.append(f"- action: {action}\n")
else:
    lines.append("- action: none\n")
if dispatch_log:
    for entry in dispatch_log:
        lines.append(f"- dispatch: {entry['board']} ready_assigned_before={entry['ready_assigned_before']} rc={entry['rc']} summary={entry['summary']}\n")
else:
    lines.append("- dispatch: none needed; no assigned ready cards found before dispatch\n")

lines.append("\n## Failure Log\n")
if failures:
    for failure in failures:
        lines.append(f"- {json.dumps(failure, sort_keys=True)}\n")
else:
    lines.append("- none\n")

lines.append("\n## Stale Timestamp / Cron Warnings\n")
if warnings:
    for warning in warnings:
        lines.append(f"- WARNING: {warning}\n")
else:
    lines.append("- none\n")

lines.append("\n## Watchdog Cron Path\n")
lines.append(f"- job_id: {cron_health.get('id')}\n")
lines.append(f"- found: {cron_health.get('found')}\n")
for key in ("name", "enabled", "schedule", "no_agent", "script", "last_run_at", "last_status", "last_error", "last_delivery_error", "next_run_at", "store"):
    if key in cron_health:
        lines.append(f"- {key}: {cron_health.get(key)}\n")
lines.append("- quiet_contract: no-agent script prints only when actions, failures, warnings, or board/cron snapshot changes are detected; otherwise stdout is empty.\n")

lines.append("\n## Material Snapshot\n")
lines.append("```json\n")
lines.append(json.dumps(snapshot, indent=2, sort_keys=True))
lines.append("\n```\n")

open(out, "w", encoding="utf-8").write("".join(lines))
open(state_path, "w", encoding="utf-8").write(json.dumps(snapshot, indent=2, sort_keys=True) + "\n")

if actions or failures or warnings or material_change:
    print(f"PM_ORCHESTRATOR_MATERIAL_CHANGE actions={len(actions)} failures={len(failures)} warnings={len(warnings)} status={out}")
    for action in actions[:20]:
        print("ACTION", action)
    for warning in warnings[:20]:
        print("WARNING", warning)
    for failure in failures[:10]:
        print("FAIL", json.dumps(failure, sort_keys=True))
PY
