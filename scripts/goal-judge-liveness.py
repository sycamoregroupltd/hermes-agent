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

Consumed by: stack-health-audit.sh (system crontab, */10) — see the
GOAL-JUDGE section there. Red-drill: pass a scratch HERMES_HOME whose config
pins a nonexistent model; expect exit 1.
"""
import os
import sys
import logging


def main() -> int:
    home = sys.argv[1] if len(sys.argv) > 1 else "/home/frank/.hermes"
    os.environ["HERMES_HOME"] = home
    # Quiet the plugin-discovery chatter; keep warnings.
    logging.basicConfig(level=logging.WARNING)

    sys.path.insert(0, "/home/frank/.hermes/hermes-agent")
    try:
        from agent.auxiliary_client import call_llm
    except Exception as exc:  # broken install is a FAIL, not a skip
        print(f"GOAL-JUDGE-FAIL import: {type(exc).__name__}: {str(exc)[:160]}")
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
        print(f"GOAL-JUDGE-FAIL {type(exc).__name__}: {str(exc)[:200]}")
        return 1
    if not content:
        print("GOAL-JUDGE-FAIL empty judge reply (parse-failure class)")
        return 1
    served = getattr(resp, "model", "?")
    print(f"GOAL-JUDGE-OK model={served} reply={content[:60]!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
