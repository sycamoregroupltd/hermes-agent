#!/usr/bin/env python3
"""Genuine live forced-release evidence for t_560fc38a (review round 2).

Runs the SHIPPED sweep_stale_inflight against a faithful COPY of the real
jarvis cron store in a temp HERMES_HOME, injecting a stale in-flight claim
for the real board-pm-triage-sycode-ai row (id 9a755435138f, 60m interval).

Why a copy: the running gateway does NOT have this fix yet (source-only
change, gates forbid deploy/restart), so a real forced release cannot be
triggered in the live process without mutating the live store.  This run
executes the exact shipped code path with the exact persisted row shape and
writes the JSONL artifact to disk — reproducible, verifiable, non-destructive.
"""
import json
import logging
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

REAL_HOME = Path("/home/frank/.hermes/profiles/jarvis")
TMP = Path(tempfile.mkdtemp(prefix="t560fc38a-evidence-"))

# 0. Snapshot the live store hash BEFORE anything runs.
import hashlib
_live_sha_before = hashlib.sha256((REAL_HOME / "cron" / "jobs.json").read_bytes()).hexdigest()

# 1. Faithful copy of the real store (jobs.json only; sweep needs load_jobs + mark_job_run).
(TMP / "cron").mkdir(parents=True, exist_ok=True)
shutil.copy2(REAL_HOME / "cron" / "jobs.json", TMP / "cron" / "jobs.json")

# 2. Point cron storage at the copy.
os.environ["HERMES_HOME"] = str(TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cron.scheduler as sched  # noqa: E402
from cron import jobs as cron_jobs  # noqa: E402

# Capture real logger output to a file (this is the actual logger the sweep uses).
log_path = TMP / "evidence-agent.log"
handler = logging.FileHandler(log_path, encoding="utf-8")
handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
sched.logger.addHandler(handler)
sched.logger.setLevel(logging.WARNING)

# 3. Load the real row for board-pm-triage-sycode-ai and inject a stale claim.
jobs = cron_jobs.load_jobs()
row = next(j for j in jobs if j.get("id") == "9a755435138f")
print("row id      :", row["id"])
print("row name    :", row.get("name"))
print("row schedule:", json.dumps(row["schedule"]))
print("interval    :", sched._job_interval_minutes(row), "minutes")

sched._running_job_ids.add(row["id"])
sched._running_since[row["id"]] = time.time() - 4 * 60 * 60  # 4h ago (240m > 120m allowance)

# 4. Run the shipped sweep.
released = sched.sweep_stale_inflight([row])
print("released    :", released)

handler.flush()
sched.logger.removeHandler(handler)
handler.close()

# 5. Verify artifacts.
log_text = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
jsonl_path = TMP / "cron" / "inflight_forced_releases.jsonl"
jsonl_text = jsonl_path.read_text(encoding="utf-8") if jsonl_path.exists() else ""
stats = sched.get_inflight_guard_stats()

print("\n--- real log line (from evidence-agent.log) ---")
for line in log_text.splitlines():
    if "cron.inflight.forced_release" in line:
        print(line)
print("\n--- JSONL artifact on disk:", jsonl_path, "---")
print(jsonl_text.strip())
print("--- guard stats ---")
print(json.dumps({k: v for k, v in stats.items() if k != "recent_forced_releases"}, indent=1))

# 6. Verify mark_job_run surfaced last_error on the COPY (not the live store).
saved = cron_jobs.load_jobs()
saved_row = next(j for j in saved if j.get("id") == "9a755435138f")
print("\n--- copy row after sweep ---")
print("last_status:", saved_row.get("last_status"))
print("last_error :", (saved_row.get("last_error") or "")[:120])

# 7. Prove the LIVE store was untouched.
live_after = hashlib.sha256((REAL_HOME / "cron" / "jobs.json").read_bytes()).hexdigest()
print("\nlive store untouched:", _live_sha_before == live_after,
      "(sha256", _live_sha_before[:12], ")")
print("tmp home:", TMP)
