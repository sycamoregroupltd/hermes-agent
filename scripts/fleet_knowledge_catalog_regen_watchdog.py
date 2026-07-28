#!/usr/bin/env python3
# pyright: reportMissingImports=false
"""Governed watchdog for the fleet-knowledge-catalog-regen cron (t_30c10d87 / t_e445eddb).

Root cause of the recurrence (t_e445eddb, live re-verified 2026-07-17): the
catalog-regen job was created in the live jarvis store, ticked, then dropped by
a downstream `save_jobs()` rewrite, and the long-lived gateway (PID 5060,
started 2026-07-16 13:42 BST) kept an in-memory snapshot whose ids diverged
from disk (`mark_job_run: job_id ... not found` warnings — all such ids were
still present on disk post-rewrite, so it was stale in-memory, not disk loss).
The earlier watchdog heal used `hermes cron create`, which mints a fresh
`uuid.uuid4().hex[:12]` every call (hermes-agent/cron/jobs.py:1113), so each
heal produced a NEW job id and the "reviewed id" never stayed stable.

This watchdog heals idempotently by a PINNED id (TARGET_ID): if the job with
that id is absent it re-inserts the exact verified job shape directly into
jobs.json (no id churn, no dependency on the gateway's in-memory snapshot), and
first prunes any stray same-name job with a different id to avoid duplicates.
Silent when present/enabled; emits a repair alert to discord:#fleet-reports only
when it had to reinsert (watchdog pattern).

Edit this canonical file, not the profile-local shim.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path

HERMES_AGENT = Path("/home/frank/.hermes/hermes-agent")
if str(HERMES_AGENT) not in sys.path:
    sys.path.insert(0, str(HERMES_AGENT))

from cron.jobs import _jobs_lock, _save_jobs_unlocked, load_jobs, use_cron_store  # noqa: E402

JARVIS_HOME = Path(os.environ.get("HERMES_CATALOG_REGEN_WATCHDOG_HOME", "/home/frank/.hermes/profiles/jarvis"))
STORE = JARVIS_HOME / "cron" / "jobs.json"
TARGET_NAME = "fleet-knowledge-catalog-regen"
# Stable id (t_e445eddb root-cause fix): `hermes cron create` mints a fresh
# uuid every call (hermes-agent/cron/jobs.py:1113), so healing by `create`
# burned a new job id on each disappearance and the "reviewed id" never stayed
# stable. We re-insert the exact verified job shape under a PINNED id instead,
# so a heal is idempotent and leaves no id churn. If a stray job with the same
# name but a different id exists, we remove it first (once) to avoid duplicates.
TARGET_ID = "3ddf2469949e"
SCRIPT = "fleet_knowledge_catalog_regen.py"
SCHEDULE = "23 4 * * *"
DELIVER = "discord:#fleet-reports"
LOG = Path(os.environ.get("HERMES_CATALOG_REGEN_WATCHDOG_LOG", "/home/frank/.hermes/var/fleet-knowledge-catalog-regen-watchdog.log"))
PY = sys.executable or "python3"


def _load_store() -> list[dict]:
    with use_cron_store(JARVIS_HOME):
        return load_jobs()


def _save_store(jobs: list[dict]) -> None:
    # Caller must hold cron.jobs._jobs_lock(). Use the canonical atomic writer
    # instead of a bare write_text whole-store rewrite; this watchdog exists to
    # prevent cron-store clobber, so it must not introduce the same lost-update
    # class itself.
    _save_jobs_unlocked(jobs)


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def job_present() -> bool:
    for j in _load_store():
        if j.get("id") == TARGET_ID and j.get("name") == TARGET_NAME and j.get("enabled"):
            return True
    return False


def _job_shape(now: str, template: dict | None = None) -> dict:
    """Verified shape of the catalog-regen job (matches t_30c10d87 install).

    When ``template`` is a stray same-name job being pruned, carry over its
    live schedule/runtime fields so the heal preserves ``next_run_at`` /
    ``last_run_at`` / ``completed`` and does not double-fire or lose history.
    """
    base = {
        "id": TARGET_ID,
        "name": TARGET_NAME,
        "prompt": "",
        "skills": [],
        "skill": None,
        "model": None,
        "provider": None,
        "provider_snapshot": None,
        "model_snapshot": None,
        "base_url": None,
        "script": SCRIPT,
        "no_agent": True,
        "context_from": None,
        "schedule": {"kind": "cron", "expr": SCHEDULE, "display": SCHEDULE},
        "schedule_display": SCHEDULE,
        "repeat": {"times": None, "completed": 0},
        "enabled": True,
        "state": "scheduled",
        "paused_at": None,
        "paused_reason": None,
        "created_at": now,
        "next_run_at": None,
        "last_run_at": None,
        "last_status": None,
        "last_error": None,
        "last_delivery_error": None,
        "deliver": DELIVER,
        "origin": None,
        "enabled_toolsets": None,
        "workdir": None,
        "fire_claim": None,
    }
    if template:
        for k in ("next_run_at", "last_run_at", "last_status", "last_error",
                  "last_delivery_error", "created_at"):
            if k in template and template[k] is not None:
                base[k] = template[k]
        if isinstance(template.get("repeat"), dict):
            base["repeat"] = {"times": None,
                              "completed": template["repeat"].get("completed", 0) or 0}
        if template.get("schedule"):
            base["schedule"] = template["schedule"]
            base["schedule_display"] = template.get("schedule_display") or SCHEDULE
    return base


def recreate() -> tuple[bool, str]:
    """Re-insert the job under its PINNED id. Returns (success_bool, detail).

    Heals idempotently without id churn and without depending on a long-lived
    gateway's in-memory snapshot of jobs.json. A stray same-name job with a
    different id is pruned (its schedule/runtime fields are carried over).
    """
    now = now_iso()
    try:
        with use_cron_store(JARVIS_HOME), _jobs_lock():
            jobs = load_jobs()
            stray = next((j for j in jobs
                          if j.get("name") == TARGET_NAME and j.get("id") != TARGET_ID), None)
            before = len(jobs)
            jobs = [j for j in jobs if not (j.get("name") == TARGET_NAME and j.get("id") != TARGET_ID)]
            pruned = before - len(jobs)
            if any(j.get("id") == TARGET_ID for j in jobs):
                if pruned:
                    _save_store(jobs)
                return True, f"already present (id {TARGET_ID}); pruned {pruned} stray"
            jobs.append(_job_shape(now, template=stray))
            _save_store(jobs)
        return True, f"reinserted id {TARGET_ID} under {STORE} (pruned {pruned} stray)"
    except Exception as exc:  # surface, never silent
        return False, f"{type(exc).__name__}: {exc}"


def main() -> int:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now(dt.timezone.utc).isoformat()
    if job_present():
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(f"{ts} ok=True present=True (no action)\n")
        return 0
    ok, detail = recreate()
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(f"{ts} ok={ok} present=False recreated={ok} detail={detail[:200]}\n")
    if ok:
        print(
            f"[fleet-knowledge-catalog-regen-watchdog] REPAIR @ {ts} | "
            f"recreated missing job '{TARGET_NAME}' in live jarvis store "
            f"(possible cron-store clobber — see t_e445eddb) | log={LOG}"
        )
        return 1  # non-zero so the failure-alert path delivers the repair notice
    print(
        f"[fleet-knowledge-catalog-regen-watchdog] FAIL @ {ts} | "
        f"job '{TARGET_NAME}' MISSING and recreate FAILED: {detail} | log={LOG}"
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
