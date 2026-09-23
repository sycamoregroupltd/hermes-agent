#!/usr/bin/env python3
# Auto-generated shim: routes 'primary-provider-liveness' output to the BOARD instead of discord:#critical-alerts,telegram.
# Canonical logic untouched at /home/frank/.hermes/scripts/nous_token_presence.sh; delivery changed only.
import json, os, subprocess, sys, time

os.environ.setdefault("RTB_KEY", "primary-provider-liveness")
os.environ.setdefault("RTB_TITLE", "primary-provider-liveness")
os.environ.setdefault("RTB_BOARD", "jarvis-os")
# A3 Isolation-safe 2026-09-11: root ~/.hermes/config.yaml first provider: match is
# auxiliary.*.provider=anthropic (false primary). Real jarvis model.provider + fleet
# traffic is openai-codex. Pin watch primary so primary-provider-liveness stops false-RED
# when billing_provider was null->unknown.
os.environ.setdefault("NOUS_WATCH_PRIMARY", "openai-codex")

# ---------------------------------------------------------------------------
# STILL-LIVE SYNTHESIS (t_b3bee631, 2026-09-23) -- additive, env-gated, reversible
#
# DEFECT THIS FIXES (measured, not asserted):
#   report-to-board's contract is "empty stdout from the wrapped script == the
#   condition has CLEARED -> complete+archive the card". nous_token_presence.sh
#   goes deliberately silent - exit 0, no stdout - for every tick inside its own
#   QUOTA_REMIND_SECONDS (default 21600s) dedup window while a benign quota
#   episode is STILL LIVE. Its only live consumer is now this RTB shim, on an
#   hourly cadence, so 5 of every 6 runs are silence-while-live: RTB read them as
#   recovery, closed the card with "the condition has cleared", and a fresh
#   duplicate card was filed at the next 6h reminder. Measured on jarvis-os:
#   5-6 duplicate cards/day for ONE unchanged episode, each auto-closed ~1h after
#   filing with a FALSE "cleared" summary (t_2482ef2e, t_9f8c7830, t_142a22ec,
#   t_6dc97179, t_7075d116, ...). The card IS the voice-line delivery, so the
#   false close also removed the alert from the board for 5 of every 6 hours.
#
# FIX SCOPE: delivery side only, exactly the role of this shim. The canonical
#   monitor logic is NOT modified and is still executed verbatim; its output and
#   exit code are passed through untouched whenever it produces any output. Only
#   the "silent + rc0 + the producer's OWN episode state file says an episode is
#   live" case is re-labelled, using fields that are stable for the whole
#   episode (first_seen, reset_at) so RTB's digest comparison sees one unchanged
#   condition: card stays open, no comment churn, and it closes truthfully when
#   the canonical script's clean path removes the state file.
#
# REVERSIBILITY: delete this block / restore the two RTB_SCRIPT lines and the
#   shim behaves byte-identically to the pre-fix version. No store, no state,
#   no board mutation happens here.
# ---------------------------------------------------------------------------
CANONICAL = os.environ.get(
    "PRIMARY_LIVENESS_CANONICAL", "/home/frank/.hermes/scripts/nous_token_presence.sh")
QUOTA_STATE = os.environ.get(
    "PRIMARY_LIVENESS_QUOTA_STATE",
    "/home/frank/.hermes/profiles/jarvis/cron/state/primary_provider_liveness_quota.json")
PRODUCER_MODE = os.environ.get("PRIMARY_LIVENESS_PRODUCER_MODE") == "1"


def _iso(ts):
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(ts)))
    except Exception:
        return "unknown"


def _producer() -> int:
    """Run the canonical monitor verbatim; only re-label its silent-while-live case."""
    r = subprocess.run(["bash", CANONICAL], capture_output=True, text=True)
    out = (r.stdout or "").strip()
    if out:
        # Any canonical output (hard alert, quota reminder, expiry probe) passes
        # through UNCHANGED, with the canonical exit code. Never rewritten.
        print(out)
        return r.returncode
    if r.returncode != 0:
        # Failed with no output: keep it visible (report-to-board returns the
        # wrapped rc when no board action is possible -> the bundle flags it).
        err = (r.stderr or "").strip()
        if err:
            print(err, file=sys.stderr)
        return r.returncode
    # Canonical is silent and healthy-looking. Distinguish the two silences using
    # the producer's OWN episode state file (written only while an episode lives;
    # deleted by the canonical clean path).
    state = {}
    try:
        with open(QUOTA_STATE) as f:
            state = json.load(f)
    except Exception:
        state = {}
    first_seen, reset_at = state.get("first_seen"), state.get("reset_at")
    if first_seen:
        primary = os.environ.get("NOUS_WATCH_PRIMARY") or "openai-codex"
        # STABLE TEXT: episode fields only -- no live counts, no clock. One digest
        # for the whole episode, so RTB takes its "unchanged" path, not a refresh.
        print(
            f"PRIMARY PROVIDER RATE-LIMITED (benign, self-recovering -- STILL LIVE, "
            f"inside the monitor's own reminder dedup window): '{primary}' has been "
            f"quota-exhausted since {_iso(first_seen)}; expected reset "
            f"{_iso(reset_at) if reset_at else 'unknown'}. The fallback chain is "
            f"serving; no action needed unless this persists past the reset or the "
            f"fallback stops serving.")
        return 1
    return 0


if PRODUCER_MODE:
    sys.exit(_producer())

# shim mode: bind the producer (ourselves, in producer mode: canonical logic
# untouched, still executed verbatim) and exec the board delivery path.
os.environ.setdefault("PRIMARY_LIVENESS_PRODUCER_MODE", "1")
# Demote the canonical script's own 6h re-alert cadence in the BOARD path only.
# That reminder is a paging-cadence device for a human channel (its original
# delivery was discord+telegram): it re-prints the same benign alert with LIVE
# session counts, so under report-to-board it changes the digest on every
# reminder tick and would post a REPORT REFRESH comment each hour on an episode
# whose facts have not changed. On the board the open card IS the persistent
# delivery (the voice line reads it every call), so the re-alert is redundant
# here. Setting the window beyond any episode keeps the produced text stable for
# the whole episode. This does NOT touch the canonical reset-overdue escalation
# (`if reset_overdue:` is an independent branch evaluated BEFORE the reminder
# elif, nous_token_presence.sh:224-240) nor the healthy/quiet clear path (:252).
os.environ.setdefault("PRIMARY_LIVENESS_QUOTA_REMIND_SECONDS", "31536000")
os.environ.setdefault("RTB_SCRIPT", os.path.abspath(__file__))
os.execv(sys.executable, [sys.executable,
    "/home/frank/.hermes/scripts/report-to-board.py", *sys.argv[1:]])