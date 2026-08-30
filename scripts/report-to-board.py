#!/usr/bin/env python3
"""Generic: deliver a cron job's report to the BOARD instead of a messaging platform.

WHY: Frank, 2026-08-27 — "all reports need to go back through the pipe and not on a
messaging platform thats not monitered". The voice bridge reads the kanban boards on
EVERY call, so a card is heard; a discord/telegram message he never opens is not.

SHAPE — the error-digest pattern generalised, and the constraints are the point:
  ONE CARD PER JOB, not per run. Keyed `rtb-<RTB_KEY>`, so re-runs update rather
  than accumulate. 42 jobs each filing per-run cards would flood the board, and the
  board IS the pipe — flooding it breaks the thing this exists to feed.
  SELF-CLOSING. When the job goes silent (its condition cleared), the card is
  completed automatically. A detector that opens cards but cannot close them is a
  ratchet — that is the defect this fleet already has 230 CRON-HEALTH cards of.
  SILENT WHEN CLEAN. Empty stdout produces nothing, exactly as `--no-agent` intends.
  EXIT CODE PRESERVED. For a no_agent job the exit code is the only liveness signal
  cron records, so it is passed through untouched.

CONFIG (env, set per job by its shim):
  RTB_SCRIPT  canonical script to run (required)
  RTB_KEY     stable job identity for the idempotency key (required)
  RTB_TITLE   card title (defaults to RTB_KEY)
  RTB_BOARD   target board (default jarvis-os)
"""
from __future__ import annotations
import hashlib, json, os, re, subprocess, sys, time
from pathlib import Path

STATE = Path("/home/frank/.hermes/state/report-to-board.json")

# P8-R2 (t_de9b87e0): [report] cards used to land unassigned, invisible to
# dispatch — same ghost-card class as fleet-alert-card.sh (t_89678308). Route
# to the target board's PM so every report card has a live triage owner.
BOARD_PM = {
    "jarvis-os": "jarvis-os-pm",
    "sycode-trading": "sycode-trading-pm",
    "upero": "upero-pm",
    "sycode-ai": "upero-pm",
    "yorkstone-supplies": "yorkstone-supplies-pm",
    "ai-restaurant": "jarvis-os-pm",
    "ecohome": "ecohome-pm",
}


def assignee_for(board: str) -> str:
    return BOARD_PM.get(board, "jarvis-os-pm")

# Subprocess timeout for the wrapped script, configurable per job via env.
# Default 600s preserves legacy behavior; guard-bundle tick shims set this
# higher (RTB_TIMEOUT=1200) so a legitimately long bundle is never killed
# mid-run (which orphaned grandchildren and piled up blocked runners, t_74f47880).
RTB_TIMEOUT = int(os.environ.get("RTB_TIMEOUT", "600"))


def hermes(*a, timeout=90):
    try:
        p = subprocess.run(["hermes", *a], capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:
        return 1, f"{type(e).__name__}: {e}"


def main() -> int:
    script = os.environ.get("RTB_SCRIPT", "").strip()
    key = os.environ.get("RTB_KEY", "").strip()
    if not script or not key:
        print(json.dumps({"status": "error", "error": "RTB_SCRIPT and RTB_KEY required"}),
              file=sys.stderr)
        return 2
    board = os.environ.get("RTB_BOARD", "jarvis-os").strip()
    title = os.environ.get("RTB_TITLE", key).strip()

    runner = ["bash", script] if script.endswith((".sh", ".bash")) else [sys.executable, script]
    try:
        r = subprocess.run(runner, capture_output=True, text=True, timeout=600)
        out, rc = (r.stdout or "").strip(), r.returncode
    except Exception as e:
        print(json.dumps({"status": "error", "error": str(e)}), file=sys.stderr)
        return 1

    STATE.parent.mkdir(parents=True, exist_ok=True)
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    rec = state.get(key, {})
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # --- clean: close any open card, say nothing ---------------------------
    if not out:
        if rec.get("card_id"):
            crc, _ = hermes("kanban", "--board", rec.get("board", board), "complete",
                            rec["card_id"], "--summary",
                            f"Auto-closed {stamp}: '{title}' reported nothing this run, so the "
                            f"condition has cleared. Closed by the same job that opened it.")
            if crc == 0:
                state.pop(key, None)
                STATE.write_text(json.dumps(state, indent=1, sort_keys=True))
                print(json.dumps({"status": "ok", "closed": rec["card_id"]}), file=sys.stderr)
        return rc

    # --- reporting: one card, refreshed only when the content changes ------
    digest = hashlib.sha256(out.encode()).hexdigest()[:16]
    if rec.get("card_id") and rec.get("digest") == digest:
        print(json.dumps({"status": "ok", "unchanged": rec["card_id"]}), file=sys.stderr)
        return rc

    body = (f"{out[:6000]}\n\n---\nReported {stamp} by cron job '{key}' (exit {rc}).\n"
            f"This card IS the delivery — the voice line reads it on every call.\n"
            f"It closes automatically when '{key}' next reports nothing.")
    if rec.get("card_id"):
        hermes("kanban", "--board", rec.get("board", board), "complete", rec["card_id"],
               "--summary", f"Superseded {stamp} by a newer report from '{key}'.")
    crc, cout = hermes("kanban", "--board", board, "create", f"[report] {title}",
                       "--body", body, "--assignee", assignee_for(board),
                       "--idempotency-key", f"rtb-{key}-{digest}")
    m = re.search(r"\b(t_[0-9a-f]{8})\b", cout)
    if crc == 0 and m:
        state[key] = {"card_id": m.group(1), "digest": digest, "board": board, "at": stamp}
        STATE.write_text(json.dumps(state, indent=1, sort_keys=True))
        print(json.dumps({"status": "ok", "card": m.group(1), "board": board}), file=sys.stderr)
    else:
        print(json.dumps({"status": "error", "out": cout[:200]}), file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
