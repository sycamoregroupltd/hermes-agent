#!/usr/bin/env python3
"""Recommendations → Implementation loop engine.

Reconciles the Recommendations Ledger against research guides (section 10) + analysis notes
+ kanban state: appends NEW recommendations (dedup), fires triage tasks for proposed recs,
and ticks recs verified ✅ when their task is done AND objective-gate/evidence passes.
Dry-run by default; --live to fire tasks + update the ledger. R3 items need a human tick.
Reuses the curator/cron LLM pattern. Obsidian git auto-commit records ledger ticks.
"""
import subprocess, datetime, sys

LEDGER = "/home/frank/obsidian-fleet-vault/Inbox-Loops/recommendations-ledger.md"
GUIDES = "/home/frank/obsidian-fleet-vault/Research/Guides"
ANALYSIS = "/home/frank/obsidian-fleet-vault/Research"
now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M")
live = "--live" in sys.argv
mode = "LIVE" if live else "DRY-RUN"

prompt = (
    f"Recommendations→Implementation loop pass ({mode}).\n"
    f"1) Read the ledger {LEDGER}. Scan {GUIDES}/*.md section '10. Follow-up' and "
    f"{ANALYSIS}/*implementation-analysis*.md for recommendations.\n"
    "2) DEDUP: add ONLY recommendations not already in the ledger (new row, status=proposed).\n"
    "3) For ledger recs status=proposed with no kanban task: "
    + ("FIRE a kanban triage task (`hermes kanban --board jarvis-os create \"<rec>\" --triage --created-by rec-loop`) and set status=triaged + record the task id.\n"
       if live else "REPORT which triage tasks WOULD be fired.\n")
    + "4) For recs status in {triaged,implementing} whose kanban task is done AND verified "
      "(objective gate `dgx_guide_evaluator.py` / tests / evidence): "
    + ("set status=verified, tick ✅, and record evidence + code commit SHA.\n"
       if live else "REPORT which WOULD be ticked verified.\n")
    + "GUARDRAIL: R3 recommendations (money / live-trading / credentials / prod / irreversible) "
      "must get a HUMAN tick — never auto-verify them.\n"
    f"Update {LEDGER} (the vault git auto-commit records the tick). Log to /tmp/dgx_rec_loop_{now[:8]}.txt."
)

subprocess.run(
    ["hermes", "-p", "jarvis", "-m", "gpt-5.5", "--provider", "openai-codex", "-z", prompt],
    timeout=900,
)

