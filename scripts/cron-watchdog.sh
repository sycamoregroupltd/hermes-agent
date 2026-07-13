#!/usr/bin/env bash
# cron-watchdog: flag enabled jobs overdue >30m that have never run, in ANY store
set -u
alerts=$(python3 - <<'PY'
import json, glob, datetime
now = datetime.datetime.now(datetime.timezone.utc).timestamp()
stores = ["/home/frank/.hermes/cron/jobs.json"] + glob.glob("/home/frank/.hermes/profiles/*/cron/jobs.json")
for f in stores:
    try:
        data = json.load(open(f))
    except FileNotFoundError:
        continue
    except Exception as e:
        print(f"{f}: unreadable ({e})")
        continue
    for j in (data if isinstance(data, list) else data.get("jobs", [])):
        if not j.get("enabled", True):
            continue
        nra = j.get("next_run_at")
        if not nra:
            continue
        try:
            t = datetime.datetime.fromisoformat(nra.replace("Z", "+00:00")).timestamp()
        except ValueError:
            print(f"{f}: '{j.get('name')}' has unparseable next_run_at: {nra}")
            continue
        if t < now - 1800 and j.get("last_run_at") is None:
            print(f"{f}: '{j.get('name')}' overdue >30m, never ran")
PY
)
if [ -n "$alerts" ]; then
    echo "$alerts" | tee -a /home/frank/.hermes/logs/cron-watchdog.log
    exit 1
fi
echo "[SILENT] all cron stores healthy"
