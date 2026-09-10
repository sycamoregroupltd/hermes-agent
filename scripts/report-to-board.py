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
import ast, hashlib, json, os, re, subprocess, sys, time
from pathlib import Path

STATE = Path(os.environ.get(
    "RTB_STATE", "/home/frank/.hermes/state/report-to-board.json"
))

# t_4ed34e09: identical-condition re-fire suppression.
#
# ROOT CAUSE this fixes: report-to-board's "one card per active incident"
# design (see module docstring) only remembers a condition via the OPEN
# card's rec (state[key]). The instant a human/worker archives that card
# (e.g. classifying a still-live-but-already-diagnosed condition as
# "reviewed, self-healing"), state.pop(key) runs and ALL memory of the
# condition is lost. The very next tick — still the same unchanged
# condition — has no rec to compare against, falls through to the
# create-new-card branch, and files a brand-new card. Repeat every cron
# cadence => dozens of near-duplicate cards for one unchanged root cause
# (observed: dgx-unified-health-probe, ~13-15min cadence, 20+ duplicates
# in a few hours).
#
# FIX: a second, tombstone-style state entry keyed f"{RTB_KEY}:fp" that is
# NEVER popped when a card closes/archives — only ever refreshed when this
# script actually takes a create/comment action. It holds a hash of the
# STABLE part of the report (see dedup_fingerprint below) plus the
# timestamp of the last real action. If the next run's fingerprint matches
# and the configured quiet window has not elapsed, filing is suppressed
# entirely (no card, no comment, no hermes call) regardless of whether a
# card is currently open, closed, or was archived by a human in between.
# A CHANGED fingerprint (new dead_key, different infra check, etc) always
# fires immediately — this only debounces byte-for-byte-same conditions,
# never new information (requirement: fail-visible semantics preserved).
#
# Opt-in only (default 0 = feature fully off): every job's behavior is
# byte-identical to pre-fix unless its shim explicitly sets
# RTB_QUIET_WINDOW_SEC. Only dgx-unified-health-probe's shim sets it.
RTB_QUIET_WINDOW_SEC = int(os.environ.get("RTB_QUIET_WINDOW_SEC", "0"))

# Stable structural lines from dgx_unified_health_probe.py's BLOCK alert body
# (scripts/dgx_unified_health_probe.py ~L1479-1523). Used only to build a
# condition fingerprint that ignores volatile content (timestamps, ready-
# backlog ages, individual crash/task ids) so an UNCHANGED root cause hashes
# identically across runs instead of drifting on every embedded
# now.isoformat() / age-in-days value.
_MECH_LINE_RE = re.compile(
    r"^\s*-\s*overall=(?P<overall>\S+)\s+dead=(?P<dead>\d+)\s+"
    r"warn=(?P<warn>\S+)\s+keys=(?P<keys>\[.*?\])\s*$", re.M)
_INFRA_NAME_RE = re.compile(r"^\s*-\s*([A-Za-z0-9_.\-]+):", re.M)

# os-reviewer round-1 fix (t_4ed34e09): the crash/forced-release BLOCK-cause
# signal used to collapse to a bare boolean (crash=1 / forced=1) -- a
# DIFFERENT crashed task, or a crash-count escalation, with dead_keys/infra
# names unchanged, hashed identically and would be silently suppressed for
# the whole quiet window. These regexes pull the actual count AND the
# stable identity of each hit (board/task_id for crashes; job name/id for
# forced releases) straight out of dgx_unified_health_probe.py's own line
# format (scripts/dgx_unified_health_probe.py ~L959-961, ~L1514-1516) so a
# genuinely different crash/forced-release set always changes the
# fingerprint. Deliberately excludes the volatile trailing fields on each
# line (outcome/status for crashes, `at`/`age_s` for forced releases) so an
# unchanged set of tasks/jobs still fingerprints identically run-to-run.
_CRASH_SECTION_RE = re.compile(
    r"^## Kanban ACTIVE crashes.*?:\s*(?P<count>\d+)\s*<-- BLOCK cause\n"
    r"(?P<items>(?:  - .+\n?)*)", re.M)
_CRASH_ITEM_RE = re.compile(r"^\s*-\s*(\S+):", re.M)
_FORCED_SECTION_RE = re.compile(
    r"^## Cron forced releases.*?:\s*(?P<count>\d+)\s*<-- BLOCK cause\n"
    r"(?P<items>(?:  - .+\n?)*)", re.M)
_FORCED_ITEM_RE = re.compile(r"^\s*-\s*([^:\n]+):", re.M)


def dedup_fingerprint(key: str, out: str) -> str:
    """Return the string whose hash is compared run-over-run for de-dup.

    Default (every job that has NOT opted into RTB_QUIET_WINDOW_SEC): the
    raw text, unchanged from today — a single differing byte is a new
    condition, exactly as before this fix.

    dgx-unified-health-probe's BLOCK body embeds now.isoformat() and
    ever-changing ready-backlog ages on every run, so a raw-text digest
    never repeats even when the actual cause (mechanism dead_keys / infra
    check names / crash-or-forced-release BLOCK cause) hasn't changed at
    all (t_4ed34e09 root cause). For that key only, extract just the
    stable cause identifiers. Any BLOCK body that doesn't match this shape
    (e.g. PASS/WARN/DEGRADED text, or a future format change upstream)
    falls back to the raw text — fail CLOSED to "treat as new", so a
    genuinely different report is never silently swallowed by a stale
    parser.
    """
    if key != "dgx-unified-health-probe":
        return out
    parts: list[str] = []
    m = _MECH_LINE_RE.search(out)
    if m:
        try:
            keys = sorted(ast.literal_eval(m.group("keys")))
        except Exception:
            keys = [m.group("keys")]
        parts.append(f"mech={m.group('overall')}:{','.join(keys)}")
    infra_section = re.search(r"## Infra checks failed\n(.*?)(?:\n\n|\Z)", out, re.S)
    if infra_section:
        names = sorted(_INFRA_NAME_RE.findall(infra_section.group(1)))
        if names:
            parts.append("infra=" + ",".join(names))
    crash_m = _CRASH_SECTION_RE.search(out)
    if crash_m:
        ids = sorted(_CRASH_ITEM_RE.findall(crash_m.group("items")))
        # Include the reported count even when item extraction comes up
        # empty (a future format change to the per-hit lines must not
        # silently degrade back to a bare boolean -- fail toward "new").
        parts.append(f"crash={crash_m.group('count')}:{','.join(ids)}")
    forced_m = _FORCED_SECTION_RE.search(out)
    if forced_m:
        names = sorted(n.strip() for n in _FORCED_ITEM_RE.findall(forced_m.group("items")))
        parts.append(f"forced={forced_m.group('count')}:{','.join(names)}")
    if not parts:
        # Nothing recognizable extracted (non-BLOCK body, or upstream format
        # drift) — fail closed to the raw text so we never fabricate a
        # false "unchanged".
        return out
    return "|".join(parts)

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
    # Seat 2026-09-07: report cards need a tool-bearing executor; PM profiles have no
    # file/terminal/cron toolsets (roster card t_2a77f79d), so the jarvis-os default is
    # fleet-engineer and sycode-trading goes to trading-devops.
    if board == "jarvis-os":
        return "fleet-engineer"
    if board == "sycode-trading":
        return "trading-devops"
    return BOARD_PM.get(board, "fleet-engineer")

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
    echo = os.environ.get("RTB_ECHO_STDOUT", "").strip().lower() in ("1", "true", "yes")
    try:
        # BUG FIX (2026-08-31, t_8cdc9260): RTB_TIMEOUT was computed above but
        # never actually applied here — this call hardcoded timeout=600
        # regardless of env, so any RTB_SCRIPT needing >600s (e.g. the
        # guard-bundle-daily bundle, budget 3300s) was killed mid-run every
        # time. Wire the already-resolved RTB_TIMEOUT through. Default stays
        # 600s (unchanged) for every job that does not set RTB_TIMEOUT.
        r = subprocess.run(runner, capture_output=True, text=True, timeout=RTB_TIMEOUT)
        out, rc = (r.stdout or "").strip(), r.returncode
    except subprocess.TimeoutExpired as e:
        stdout = e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, (bytes, bytearray)) else (e.stdout or "")
        stderr = e.stderr.decode("utf-8", "replace") if isinstance(e.stderr, (bytes, bytearray)) else (e.stderr or "")
        partial = (stdout + (("\n" + stderr) if stderr else "")).strip()
        out = (
            (partial + "\n\n" if partial else "")
            + f"## ABORTED @ RTB_TIMEOUT {RTB_TIMEOUT}s\n"
            + "Producer did not finish before RTB_TIMEOUT. This run is a "
            + "measurement failure, not a clear verdict. Prior clear/full "
            + "report is no longer authoritative for this cycle.\n"
        ).strip()
        rc = 124
    except Exception as e:
        print(json.dumps({"status": "error", "error": str(e)}), file=sys.stderr)
        return 1

    STATE.parent.mkdir(parents=True, exist_ok=True)
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    rec = state.get(key, {})
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # Optional echo so Hermes cron output files are non-silent for jobs
    # whose primary consumer is a file artifact (fusion-calibration-report).
    # Default off preserves silent-when-clean for other RTB jobs.
    if echo and out:
        print(out)

    # --- clean: close and archive untouched report cards -------------------
    if not out:
        # t_4ed34e09: condition confirmed cleared this run — drop the
        # tombstone too so a FUTURE recurrence of the same fingerprint is
        # never wrongly suppressed as "still the old, already-handled
        # instance". Safe even if quiet-window suppression is disabled for
        # this key (tomb_key simply won't exist).
        if state.pop(f"{key}:fp", None) is not None:
            persist_state(state)
        if rec.get("card_id"):
            card_id = rec["card_id"]
            card_board = rec.get("board", board)
            status = card_status(card_id, card_board)
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

    # t_4ed34e09: fingerprint the STABLE cause (see dedup_fingerprint) and
    # compare against the tombstone that survives card close/archive. This
    # is the only thing that lets us suppress "new card because state.pop()
    # ran when a human archived the last one" — see module-level comment.
    now_epoch = int(time.time())
    fp_digest = hashlib.sha256(dedup_fingerprint(key, out).encode()).hexdigest()[:16]
    tomb_key = f"{key}:fp"
    tomb = state.get(tomb_key, {})
    same_condition = RTB_QUIET_WINDOW_SEC > 0 and tomb.get("fp_digest") == fp_digest
    quiet_active = same_condition and (now_epoch - int(tomb.get("at_epoch", 0))) < RTB_QUIET_WINDOW_SEC

    def _touch_tombstone() -> None:
        # Opt-in only: don't grow report-to-board.json with a :fp entry for
        # every job that never uses this feature.
        if RTB_QUIET_WINDOW_SEC > 0:
            state[tomb_key] = {"fp_digest": fp_digest, "at": stamp, "at_epoch": now_epoch}

    if rec.get("card_id"):
        card_id = rec["card_id"]
        card_board = rec.get("board", board)
        status = card_status(card_id, card_board)
        if status not in {None, "done", "completed", "cancelled", "archived"}:
            if rec.get("digest") == digest:
                print(json.dumps({"status": "ok", "unchanged": card_id}), file=sys.stderr)
                return rc
            if quiet_active:
                # Same underlying cause (dead_keys/infra/crash signature
                # unchanged), only volatile text (timestamps, ages) moved.
                # Record the freshest digest so a FUTURE genuine change is
                # still detected, but skip the comment — no new information
                # for a human to act on.
                state[key] = {"card_id": card_id, "digest": digest,
                              "board": card_board, "at": stamp}
                persist_state(state)
                print(json.dumps({"status": "ok", "suppressed_refresh": card_id,
                                   "quiet_window_s": RTB_QUIET_WINDOW_SEC}),
                      file=sys.stderr)
                return rc
            update = (f"REPORT REFRESH {stamp} (exit {rc}):\n\n{out[:6000]}")
            crc, cout = hermes("kanban", "--board", card_board, "comment",
                               "--author", "report-to-board", card_id, update)
            if crc == 0:
                state[key] = {"card_id": card_id, "digest": digest,
                              "board": card_board, "at": stamp}
                _touch_tombstone()
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

    # No open card (never filed, or the previous one was closed/archived by
    # a human/worker in between runs). If the condition is unchanged from
    # the tombstone and we're inside the quiet window, this is exactly the
    # re-fire this fix targets — suppress, don't create a duplicate.
    if quiet_active:
        print(json.dumps({"status": "ok", "suppressed_new_card": True,
                           "quiet_window_s": RTB_QUIET_WINDOW_SEC,
                           "last_action_at": tomb.get("at")}), file=sys.stderr)
        return rc

    body = (f"{out[:6000]}\n\n---\nReported {stamp} by cron job '{key}' (exit {rc}).\n"
            f"This card IS the delivery — the voice line reads it on every call.\n"
            f"It closes automatically when '{key}' next reports nothing.")
    crc, cout = hermes("kanban", "--board", board, "create", f"[report] {title}",
                       "--body", body, "--assignee", assignee_for(board),
                       "--idempotency-key", f"rtb-{key}")
    m = re.search(r"\b(t_[0-9a-f]{8})\b", cout)
    if crc == 0 and m:
        state[key] = {"card_id": m.group(1), "digest": digest, "board": board, "at": stamp}
        _touch_tombstone()
        persist_state(state)
        print(json.dumps({"status": "ok", "card": m.group(1), "board": board}), file=sys.stderr)
    else:
        print(json.dumps({"status": "error", "out": cout[:200]}), file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
