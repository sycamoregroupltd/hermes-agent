#!/usr/bin/env python3
"""Deliver the fleet daily digest to the BOARD, not to a messaging platform.

WHY: Frank, 2026-08-27 — "i dont even read my telegram - all reports need to go
back through the pipe and not on a messaging platform thats not monitered".
The voice bridge's _gather_live_state() reads ~/.hermes/kanban/boards on EVERY
call and injects the result into the session, so a card is heard on the next
phone call. A telegram message is not. This digest was built specifically for
Frank's phone and was routed to the one channel he never opens.

PRESERVING THE LIVENESS CONTRACT: the original script says "If this message stops
arriving, that silence is the alarm." A missing card is harder to notice than a
missing message, so the card TITLE carries its own date and the body states the
age explicitly — a stale card is visibly stale on the board and in the spoken
snapshot. Exactly one digest card is kept: today's opens, yesterday's is closed.

Wraps the canonical script rather than editing it (canonical-copy rule t_7fec9a7c).
"""
from __future__ import annotations
import json, os, re, subprocess, sys, time
from pathlib import Path

CANON = "/home/frank/.hermes/scripts/fleet-daily-digest.sh"
BOARD = os.environ.get("FLEET_DIGEST_BOARD", "jarvis-os")
STATE = Path("/home/frank/.hermes/state/fleet-daily-digest-card.json")

# ADOPT item 5: durable per-job state lives in the cron notepad (native KV)
# for this job (7b14a5eae3df, jarvis profile). The loose JSON file is kept as
# a read-only rollback mirror; the notepad is the source of truth.
from notepad_state import NotepadStore  # type: ignore
_NOTEPAD = NotepadStore(
    "7b14a5eae3df", "/home/frank/.hermes/profiles/jarvis"
)


def _load_state() -> dict:
    """Load job state from the notepad (primary), falling back to the JSON
    mirror so the first notepad-enabled run doesn't lose prior state."""
    raw = _NOTEPAD.get("digest:card")
    if raw is not None:
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            pass
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    return state


def _save_state(state: dict) -> None:
    """Persist job state to the notepad (source of truth). Also mirrors to the
    legacy JSON file for rollback inspection only."""
    _NOTEPAD.set("digest:card", json.dumps(state, separators=(",", ":")))
    try:
        STATE.write_text(json.dumps(state, indent=1))
    except OSError:
        pass



def hermes(*args, timeout=90):
    try:
        p = subprocess.run(["hermes", *args], capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:
        return 1, f"{type(e).__name__}: {e}"


def main() -> int:
    try:
        r = subprocess.run(["bash", CANON], capture_output=True, text=True, timeout=300)
        digest = (r.stdout or "").strip()
    except Exception as e:
        print(json.dumps({"status": "error", "error": str(e)}), file=sys.stderr)
        return 1
    if not digest:
        print(json.dumps({"status": "error", "error": "canonical digest produced no output"}),
              file=sys.stderr)
        return 1

    today = time.strftime("%Y-%m-%d")
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    STATE.parent.mkdir(parents=True, exist_ok=True)
    state = _load_state()

    if state.get("date") == today and state.get("card_id"):
        print(json.dumps({"status": "ok", "note": "today's digest card already exists",
                          "card": state["card_id"]}), file=sys.stderr)
        return 0

    body = (
        f"{digest}\n\n"
        f"Generated {stamp}. This card IS the delivery — it is read by the voice line's "
        f"fleet snapshot on every call.\n"
        f"LIVENESS: exactly one digest card should exist and it should carry TODAY's date. "
        f"If the newest one is older than today, the digest job has stopped and that "
        f"staleness is the alarm."
    )
    rc, out = hermes("kanban", "--board", BOARD, "create",
                     f"[fleet-daily] {today} — fleet digest",
                     "--body", body,
                     "--idempotency-key", f"fleet-daily-{today}")
    m = re.search(r"\b(t_[0-9a-f]{8})\b", out)
    if rc != 0 or not m:
        print(json.dumps({"status": "error", "error": "card create failed", "out": out[:300]}),
              file=sys.stderr)
        return 1
    new_card = m.group(1)

    closed = None
    prev = state.get("card_id")
    if prev and prev != new_card:
        crc, _ = hermes("kanban", "--board", BOARD, "complete", prev,
                        "--summary", f"Superseded by {today}'s fleet digest ({new_card}).")
        closed = prev if crc == 0 else None

    _save_state({"date": today, "card_id": new_card, "closed_previous": closed})
    print(json.dumps({"status": "ok", "card": new_card, "closed_previous": closed,
                      "board": BOARD}), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
