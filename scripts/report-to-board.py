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
  RTB_SCRIPT     canonical script to run (required)
  RTB_KEY        stable job identity for the idempotency key (required)
  RTB_TITLE      card title (defaults to RTB_KEY)
  RTB_BOARD      target board (default jarvis-os)
  RTB_STATE_FILE optional path to the wrapped job's OWN state file, for
                  falling-edge jobs (t_06b884a5). See still_active() below.

FALLING-EDGE FALSE-CLEAR (t_06b884a5, live incident 2026-09-05T16:25:01Z):
  A falling-edge job (e.g. nous_balance_watchdog.py) alerts once when a
  condition crosses a threshold, then goes SILENT while the condition
  persists (dedup re-remind, default 24h). Empty stdout from such a job is
  ambiguous — it means EITHER "genuinely healthy" OR "still bad, just
  deduped" — but the close path below historically treated ALL empty stdout
  as "condition cleared" and completed+archived the card, including blocked
  cards. That auto-closed a card while the wrapped job's own state file still
  recorded the bad condition (usable=0.0 < threshold=5.0), silently dropping
  a live spend-gate.
  Fix: an opt-in RTB_STATE_FILE hook. A job's shim points RTB_STATE_FILE at
  the same state path the wrapped script itself owns. That script's contract
  (already true for nous_balance_watchdog.py, and the general falling-edge
  shape used by position_age_watchdog.py / dgx_data_freshness_probe.py) is:
  state file PRESENT == still tracking an active/bad condition (including a
  JWT/transient-read hiccup that intentionally preserves prior state); state
  file ABSENT == the job called clear_state() on genuine recovery. When
  RTB_STATE_FILE is set, empty stdout + a present state file means STILL
  ACTIVE, not a clear: RTB comments instead of completing/archiving. Jobs
  that don't set RTB_STATE_FILE keep the original close-on-silence behavior
  unchanged.
"""
from __future__ import annotations
import hashlib, json, os, re, subprocess, sys, time
from pathlib import Path

STATE = Path(os.environ.get(
    "RTB_STATE", "/home/frank/.hermes/state/report-to-board.json"
))

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
# Default 600s preserves legacy behavior; guard-bundle tick shims set
# per-cadence bounded values so each bundle completes below the live scheduler
# kill boundary while preserving the wrapped check's alert semantics.
RTB_TIMEOUT = int(os.environ.get("RTB_TIMEOUT", "600"))


def hermes(*a, timeout=90):
    try:
        p = subprocess.run(["hermes", *a], capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:
        return 1, f"{type(e).__name__}: {e}"


def card_status(card_id: str, board: str) -> str | None:
    """Read the live card status; unknown is fail-closed."""
    rc, out = hermes("kanban", "--board", board, "show", card_id)
    if rc != 0:
        return None
    match = re.search(r"^\s*status:\s*(\S+)", out, re.M)
    return match.group(1) if match else None


def persist_state(state: dict) -> None:
    STATE.write_text(json.dumps(state, indent=1, sort_keys=True))


def still_active(state_file: str) -> bool:
    """True when a falling-edge job's OWN state file says the condition is
    still live, so empty stdout must NOT be read as "cleared" (t_06b884a5).

    Contract shared by every first_seen.json-style falling-edge script in
    this fleet (nous_balance_watchdog.py, position_age_watchdog.py,
    dgx_data_freshness_probe.py): the script writes this file while the bad
    condition persists — including on a transient read hiccup, where it
    deliberately leaves prior state untouched rather than clearing it — and
    calls clear_state() (unlink) only on genuine recovery. So file PRESENT
    means still-bad-just-deduped; file ABSENT means genuinely recovered.
    An unreadable/corrupt file fails closed (treated as still-active) rather
    than risking a second false-clear on top of a read error.
    """
    p = Path(state_file)
    return p.exists()


def main() -> int:
    script = os.environ.get("RTB_SCRIPT", "").strip()
    key = os.environ.get("RTB_KEY", "").strip()
    if not script or not key:
        print(json.dumps({"status": "error", "error": "RTB_SCRIPT and RTB_KEY required"}),
              file=sys.stderr)
        return 2
    board = os.environ.get("RTB_BOARD", "jarvis-os").strip()
    title = os.environ.get("RTB_TITLE", key).strip()
    state_file = os.environ.get("RTB_STATE_FILE", "").strip()

    runner = ["bash", script] if script.endswith((".sh", ".bash")) else [sys.executable, script]
    try:
        # BUG FIX (2026-08-31, t_8cdc9260): RTB_TIMEOUT was computed above but
        # never actually applied here — this call hardcoded timeout=600
        # regardless of env, so any RTB_SCRIPT needing >600s (e.g. the
        # guard-bundle-daily bundle, budget 3300s) was killed mid-run every
        # time. Wire the already-resolved RTB_TIMEOUT through. Default stays
        # 600s (unchanged) for every job that does not set RTB_TIMEOUT.
        r = subprocess.run(runner, capture_output=True, text=True, timeout=RTB_TIMEOUT)
        out, rc = (r.stdout or "").strip(), r.returncode
    except Exception as e:
        print(json.dumps({"status": "error", "error": str(e)}), file=sys.stderr)
        return 1

    STATE.parent.mkdir(parents=True, exist_ok=True)
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    rec = state.get(key, {})
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # --- clean: close and archive untouched report cards -------------------
    if not out:
        # t_06b884a5: a falling-edge job's empty stdout is ambiguous — it
        # means EITHER genuine recovery OR still-bad-but-deduped (default
        # 24h re-remind). When the job's own state file says the condition
        # is still live, do NOT treat this run as a clear: skip straight to
        # the "owner comment" path below so a blocked/owned card is never
        # silently completed+archived while the underlying condition is
        # unresolved. Jobs without RTB_STATE_FILE are unaffected.
        job_still_active = bool(state_file) and still_active(state_file)
        if rec.get("card_id"):
            card_id = rec["card_id"]
            card_board = rec.get("board", board)
            status = card_status(card_id, card_board)
            if job_still_active:
                if status not in {None, "done", "completed", "cancelled", "archived"}:
                    hermes("kanban", "--board", card_board, "comment", "--author",
                           "report-to-board", card_id,
                           f"STILL ACTIVE {stamp}: '{title}' reported nothing this run "
                           "(falling-edge dedup window), but its own state file still "
                           "shows the condition live. NOT auto-closing — see "
                           f"{state_file}.")
                return rc
            if status in {"done", "completed", "cancelled"}:
                arc, _ = hermes("kanban", "--board", card_board, "archive", card_id)
                if arc == 0:
                    state.pop(key, None)
                    persist_state(state)
            elif status == "archived":
                state.pop(key, None)
                persist_state(state)
            # OPS: RTB-owned cards (keyed in report-to-board.json) may land in
            # blocked while the condition is live; include blocked in the
            # close-set so a clean RESOLVED run can complete+archive. Scoped to
            # this RTB key path only — do not mass-close unrelated blocked cards.
            # NOTE: job_still_active (checked above) already intercepts and
            # returns before this branch when the wrapped job's own state file
            # says the condition persists, so a blocked card backed by a live
            # falling-edge state file never reaches this close path (t_06b884a5).
            elif status in {"ready", "todo", "triage", "scheduled", "blocked"}:
                crc, _ = hermes("kanban", "--board", card_board, "complete", card_id,
                                "--summary",
                                f"Auto-closed {stamp}: '{title}' reported nothing this run, so the "
                                f"condition has cleared. Closed by the same job that opened it.")
                arc, _ = ((1, "") if crc != 0 else
                          hermes("kanban", "--board", card_board, "archive", card_id))
                if crc == 0 and arc == 0:
                    state.pop(key, None)
                    persist_state(state)
                    print(json.dumps({"status": "ok", "closed": card_id}), file=sys.stderr)
            elif status is not None:
                # A worker/reviewer owns the card. Do not stomp its lifecycle;
                # attach the recovery evidence and let that owner close it.
                hermes("kanban", "--board", card_board, "comment", "--author",
                       "report-to-board", card_id,
                       f"RESOLVED {stamp}: '{title}' reported nothing this run; "
                       "the underlying condition has cleared.")
        return rc

    # --- reporting: one durable card per active incident -------------------
    digest = hashlib.sha256(out.encode()).hexdigest()[:16]
    if rec.get("card_id"):
        card_id = rec["card_id"]
        card_board = rec.get("board", board)
        status = card_status(card_id, card_board)
        if status not in {None, "done", "completed", "cancelled", "archived"}:
            if rec.get("digest") == digest:
                print(json.dumps({"status": "ok", "unchanged": card_id}), file=sys.stderr)
                return rc
            update = (f"REPORT REFRESH {stamp} (exit {rc}):\n\n{out[:6000]}")
            crc, cout = hermes("kanban", "--board", card_board, "comment",
                               "--author", "report-to-board", card_id, update)
            if crc == 0:
                state[key] = {"card_id": card_id, "digest": digest,
                              "board": card_board, "at": stamp}
                persist_state(state)
                print(json.dumps({"status": "ok", "updated": card_id}), file=sys.stderr)
            else:
                print(json.dumps({"status": "error", "out": cout[:200]}), file=sys.stderr)
            return rc
        if status in {"done", "completed", "cancelled"}:
            # Retire the terminal incident so the stable idempotency key can be
            # reused if the condition genuinely recurs.
            hermes("kanban", "--board", card_board, "archive", card_id)
        state.pop(key, None)
        persist_state(state)

    body = (f"{out[:6000]}\n\n---\nReported {stamp} by cron job '{key}' (exit {rc}).\n"
            f"This card IS the delivery — the voice line reads it on every call.\n"
            f"It closes automatically when '{key}' next reports nothing.")
    crc, cout = hermes("kanban", "--board", board, "create", f"[report] {title}",
                       "--body", body, "--assignee", assignee_for(board),
                       "--idempotency-key", f"rtb-{key}")
    m = re.search(r"\b(t_[0-9a-f]{8})\b", cout)
    if crc == 0 and m:
        state[key] = {"card_id": m.group(1), "digest": digest, "board": board, "at": stamp}
        persist_state(state)
        print(json.dumps({"status": "ok", "card": m.group(1), "board": board}), file=sys.stderr)
    else:
        print(json.dumps({"status": "error", "out": cout[:200]}), file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
