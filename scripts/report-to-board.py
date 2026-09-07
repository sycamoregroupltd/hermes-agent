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
  RTB_OBSERVATION_PROTOCOL=guard-bundle-v1 enables exact positive-observation
                  markers and same-key serialization for the guard bundle only
  RTB_STATE_FILE optional wrapped-job state path; present means still active
"""
from __future__ import annotations
import hashlib, json, os, re, subprocess, sys, time
from functools import wraps
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - the live target is POSIX
    fcntl = None

STATE = Path(os.environ.get(
    "RTB_STATE", "/home/frank/.hermes/state/report-to-board.json"
))
OBSERVATION_PROTOCOL = "guard-bundle-v1"
OBSERVATION_MARKERS = {
    "GUARD_BUNDLE_OBSERVATION_V1 CLEAN": "clean",
    "GUARD_BUNDLE_OBSERVATION_V1 NO_DUE_CHECKS": "no_due",
    "GUARD_BUNDLE_OBSERVATION_V1 DEFERRED": "deferred",
}


def _protocol_enabled() -> bool:
    return os.environ.get("RTB_OBSERVATION_PROTOCOL", "") == OBSERVATION_PROTOCOL


def _acquire_observation_lock(board: str, key: str) -> int | bool | None:
    """Fence one opted-in producer through report application."""
    if fcntl is None:
        return None
    digest = hashlib.sha256(f"{board}\0{key}".encode()).hexdigest()[:16]
    path = STATE.parent / f".report-to-board-{digest}.lock"
    STATE.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return False
    return fd


def _release_observation_lock(fd: int | bool | None) -> None:
    if isinstance(fd, int):
        os.close(fd)


def _observation_lock_guard(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not _protocol_enabled():
            return fn(*args, **kwargs)
        board = os.environ.get("RTB_BOARD", "jarvis-os").strip()
        key = os.environ.get("RTB_KEY", "").strip()
        if not key:
            return fn(*args, **kwargs)
        try:
            fd = _acquire_observation_lock(board, key)
        except Exception as exc:
            print(json.dumps({"status": "error", "error": f"observation lock setup failed: {exc}"}),
                  file=sys.stderr)
            return 1
        if fd is False:
            return 0
        try:
            return fn(*args, **kwargs)
        finally:
            _release_observation_lock(fd)
    return wrapped

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


def still_active(state_file: str) -> bool:
    """A configured falling-edge producer remains active until it unlinks its state."""
    return Path(state_file).exists()


def _normalize_final_newline(raw: str) -> str:
    return raw[:-1] if raw.endswith("\n") else raw


@_observation_lock_guard
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
    protocol = _protocol_enabled()

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
        raw_out, rc = (r.stdout or ""), r.returncode
        out = raw_out.strip()
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
        raw_out, rc = out, 124
    except Exception as e:
        print(json.dumps({"status": "error", "error": str(e)}), file=sys.stderr)
        return 1

    if protocol:
        marker = OBSERVATION_MARKERS.get(_normalize_final_newline(raw_out))
        if rc == 0 and marker in {"no_due", "deferred"}:
            return 0
        if rc == 0 and marker == "clean":
            out = ""
        elif rc == 0:
            out = ("GUARD BUNDLE OBSERVATION CONTRACT ERROR: expected one exact "
                   "v1 marker, received:\n" + (raw_out[:6000] or "<empty>"))
            rc = 1
        elif not out:
            out = f"## PRODUCER EXIT {rc}\nProducer returned nonzero with no output."

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
        if state_file and still_active(state_file):
            if rec.get("card_id"):
                card_id = rec["card_id"]
                card_board = rec.get("board", board)
                status = card_status(card_id, card_board)
                if status not in {None, "done", "completed", "cancelled", "archived"}:
                    hermes("kanban", "--board", card_board, "comment", "--author",
                           "report-to-board", card_id,
                           f"STILL ACTIVE {stamp}: '{title}' reported nothing this run "
                           "but its own state file still shows the condition live. "
                           f"NOT auto-closing — see {state_file}.")
            return rc
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
                                f"Auto-closed {stamp}: '{title}' completed due checks passed and "
                                "pending rechecks cleared. Closed by the same job that opened it.")
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
                       (f"RESOLVED {stamp}: '{title}' completed due checks passed and pending "
                        "rechecks cleared."
                        if protocol else
                        f"RESOLVED {stamp}: '{title}' reported nothing this run; "
                        "the underlying condition has cleared."))
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
