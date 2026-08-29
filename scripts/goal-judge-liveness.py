#!/usr/bin/env python3
"""goal-judge-liveness.py — synthetic goal-judge probe (card t_9df82b30, 2026-08-02).

WHY: every goal-mode kanban completion runs through the auxiliary goal judge
(hermes_cli/goals.py judge_goal -> call_llm(task="goal_judge")). When the
judge's provider/model dies (model delisted, provider balance empty, 429
treadmill), judge_goal returns a transport-failure "continue" verdict and the
kanban_complete gate REJECTS every completion fleet-wide — a silent
completion-backlog builder. 2026-08-02 22:00Z outage: nous balance empty made
every PAID model 404 ("requires available credits" => NotFoundError), so
profiles whose judge auto-resolved to the paid default were rejected.

WHAT: makes ONE tiny synthetic judge call through the exact production path
(agent.auxiliary_client.call_llm task="goal_judge", config-resolved provider/
model) and reports PASS/FAIL. Run it with the hermes venv python:

    /home/frank/.hermes/hermes-agent/.venv/bin/python \
        /home/frank/.hermes/scripts/goal-judge-liveness.py [profile-home]

Exit 0 = judge reachable and returned content.
Exit 1 = judge transport/parse failure (goal-mode completions are at risk).

BLAST RADIUS (t_f2360b4e, 2026-08-29, nervous-system-engineer seat)
------------------------------------------------------------------
A FAIL used to be reported by the caller as "goal-mode kanban completions
fleet-wide are being REJECTED".  That is an ASSERTION, not a measurement, and
on 2026-08-28 it was false for 6h and counting: the Nous session belonging to
the ROOT/default home (/home/frank/.hermes) was revoked at 17:10:19Z
("Nous Portal detected refresh-token reuse and revoked this session",
code=invalid_grant, relogin_required=true) while every one of the 74 PROFILE
homes kept a working judge.  Nothing dispatches work in the root home — it had
0 sessions in 7 days, 0 agent-mode cron jobs, and every gateway process runs
`--profile <name>` — so the fleet was never blocked.

So on failure this probe now appends a measured SCOPE clause to the single
final line (the caller keeps only the last line).  The scope is derived from
OUTCOMES, not from credential metadata — presence is not validity, see
nous_token_presence.sh's 2026-08-14 rewrite — by counting rows that real
inference calls wrote into each profile's state.db.  It makes NO network call,
NO LLM call, and NO credential/token refresh, so it cannot itself provoke the
refresh-token-reuse revocation it is reporting on.

The exit code contract is UNCHANGED: a failing probe is still rc=1 / RED.  Only
the wording gained a measurement.

Consumed by: stack-health-audit.sh (system crontab, */10) — see the
GOAL-JUDGE section there. Red-drill: pass a scratch HERMES_HOME whose config
pins a nonexistent model; expect exit 1.
"""
import glob
import os
import sqlite3
import sys
import time
import logging

FLEET_ROOT = "/home/frank/.hermes"
# Env overrides exist for RED-PATH DRILLS ONLY (point the glob at a scratch dir
# to prove the "no lane is serving" branch without touching a live lane).
PROFILES_GLOB = os.environ.get(
    "GOAL_JUDGE_PROFILES_GLOB", "/home/frank/.hermes/profiles/*/state.db"
)
# >30min so a short quiet patch cannot manufacture a false zero
# (same window nous_token_presence.sh settled on).
LANE_WINDOW_MIN = int(os.environ.get("GOAL_JUDGE_LANE_WINDOW_MIN", "45"))
DORMANT_DAYS = int(os.environ.get("GOAL_JUDGE_DORMANT_DAYS", "7"))


def _session_count(db_path: str, since_epoch: float) -> int:
    """Sessions started since `since_epoch` in one home's state.db. Fail-open (-1)."""
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            row = con.execute(
                "SELECT COUNT(*) FROM sessions WHERE started_at >= ?", (since_epoch,)
            ).fetchone()
            return int(row[0]) if row else 0
        finally:
            con.close()
    except Exception:
        return -1


def scope_clause(home: str) -> str:
    """Measured blast-radius clause for a FAILED probe. Never raises."""
    try:
        now = time.time()
        probed_db = os.path.join(home, "state.db")
        probed_recent = _session_count(probed_db, now - DORMANT_DAYS * 86400)

        lanes_live = 0
        lanes_total = 0
        lane_sessions = 0
        for db in glob.glob(PROFILES_GLOB):
            if os.path.realpath(os.path.dirname(db)) == os.path.realpath(home):
                continue  # don't count the home we just probed as its own witness
            lanes_total += 1
            n = _session_count(db, now - LANE_WINDOW_MIN * 60)
            if n > 0:
                lanes_live += 1
                lane_sessions += n

        if probed_recent == 0:
            dormancy = (
                f"probed home {home} is DORMANT (0 sessions in {DORMANT_DAYS}d) "
                f"— no dispatched worker uses it"
            )
        elif probed_recent > 0:
            dormancy = f"probed home {home} is ACTIVE ({probed_recent} sessions/{DORMANT_DAYS}d)"
        else:
            dormancy = f"probed home {home} activity UNKNOWN (state.db unreadable)"

        if lane_sessions > 0:
            lanes = (
                f"{lanes_live}/{lanes_total} profile lanes served {lane_sessions} "
                f"session(s) in the last {LANE_WINDOW_MIN}m"
            )
            verdict = (
                "goal-mode completions are NOT fleet-blocked"
                if probed_recent == 0
                else "other lanes still serving — blast radius is the probed home"
            )
        else:
            lanes = f"0/{lanes_total} profile lanes served anything in the last {LANE_WINDOW_MIN}m"
            verdict = "FLEET-WIDE risk — no lane is serving, treat as a real judge outage"

        return f" | SCOPE: {dormancy}; {lanes} — {verdict}."
    except Exception as exc:  # scope is a nicety; never let it break the probe
        return f" | SCOPE: unavailable ({type(exc).__name__})."


def main() -> int:
    home = sys.argv[1] if len(sys.argv) > 1 else "/home/frank/.hermes"
    os.environ["HERMES_HOME"] = home
    # Quiet the plugin-discovery chatter; keep warnings.
    logging.basicConfig(level=logging.WARNING)

    sys.path.insert(0, "/home/frank/.hermes/hermes-agent")
    try:
        from agent.auxiliary_client import call_llm
    except Exception as exc:  # broken install is a FAIL, not a skip
        print(
            f"GOAL-JUDGE-FAIL import: {type(exc).__name__}: {str(exc)[:160]}"
            + scope_clause(home)
        )
        return 1

    messages = [
        {
            "role": "system",
            "content": (
                "You are a strict judge. Reply with exactly this JSON and "
                'nothing else: {"verdict": "done"}'
            ),
        },
        {
            "role": "user",
            "content": (
                "Goal: reply OK. Agent response: OK. Is the goal met? "
                'Reply {"verdict": "done"} or {"verdict": "continue"}.'
            ),
        },
    ]
    try:
        resp = call_llm(
            task="goal_judge",
            messages=messages,
            temperature=0,
            max_tokens=30,
            timeout=45,
        )
        content = (resp.choices[0].message.content or "").strip()
    except Exception as exc:
        print(
            f"GOAL-JUDGE-FAIL {type(exc).__name__}: {str(exc)[:200]}"
            + scope_clause(home)
        )
        return 1
    if not content:
        print("GOAL-JUDGE-FAIL empty judge reply (parse-failure class)" + scope_clause(home))
        return 1
    served = getattr(resp, "model", "?")
    print(f"GOAL-JUDGE-OK model={served} reply={content[:60]!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
