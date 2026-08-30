#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
"""Script-only cron delivery-health check.

Scans Hermes cron stores for enabled jobs whose execution succeeded
(last_status == "ok") but whose delivery layer recorded an error. This
replaces an LLM-backed health check so delivery-health monitoring does not
consume provider credits.

Output contract for no_agent cron:
- stdout is empty when no delivery errors are present (silent success)
- stdout contains a compact report only when delivery errors are found
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path("/home/frank/.hermes")
_seen_stores: set[str] = set()
STORES = [ROOT / "cron" / "jobs.json"]
for _p in sorted((ROOT / "profiles").glob("*/cron/jobs.json")):
    _real = str(_p.resolve())
    if _real in _seen_stores:
        continue
    _seen_stores.add(_real)
    STORES.append(_p)


def iter_jobs(path: Path):
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text())
    except Exception as exc:  # fail visible: corrupt store should page the operator
        yield {
            "store": str(path),
            "name": "<store-read-error>",
            "id": "-",
            "delivery_error": f"{type(exc).__name__}: {exc}",
        }
        return
    jobs = data.get("jobs", data if isinstance(data, list) else [])
    for job in jobs:
        if not isinstance(job, dict):
            continue
        delivery_error = job.get("last_delivery_error")
        if (
            job.get("enabled") is True
            and job.get("last_status") == "ok"
            and delivery_error
        ):
            yield {
                "store": str(path),
                "name": job.get("name") or "<unnamed>",
                "id": job.get("id") or "-",
                "delivery_error": str(delivery_error),
            }


def main() -> int:
    issues: list[dict[str, Any]] = []
    for store in STORES:
        issues.extend(iter_jobs(store) or [])
    if not issues:
        return 0
    print(
        "CRON_DELIVERY_HEALTH_FAIL enabled jobs with successful execution but delivery errors:"
    )
    for issue in issues:
        msg = issue["delivery_error"].replace("\n", " ")[:240]
        print(
            f"- {issue['name']} ({issue['id']}): store={issue['store']} delivery_error={msg}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
