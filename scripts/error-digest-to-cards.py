#!/usr/bin/env python3
"""Turn persistent fleet errors into ONE card per error class — and close them again.

WHY THE OBVIOUS VERSION IS WRONG HERE
-------------------------------------
A naive "error -> card" cron is a known trap in this fleet. Measured 2026-08-27:
17,037 cards created against 7,183 done in 90 days (net +9,854); the auto-
decomposer alone writes ~421/week, almost exactly the net growth; 753 cards are
blocked right now and 340 of those have block_kind NULL so nobody can even
classify them; 38/38 cards once sat 'running' with zero live processes because
NOTHING CLOSES CARDS. Adding another producer in front of a queue nobody drains
makes a bigger pile, not a better fleet — the "detector population grows
O(incidents) while mechanism population stays flat" pattern.

THE THREE CONSTRAINTS THAT MAKE IT SELF-LIMITING
------------------------------------------------
1. ONE CARD PER ERROR CLASS, not per occurrence. Deduplicated by signature and
   only opened after the error PERSISTS across MIN_STREAK consecutive checks, so
   a blip never becomes a card.
2. THE DETECTOR CLOSES ITS OWN CARD when the error clears. If the thing that
   opens cards cannot close them, you have built a ratchet. This is the single
   most important rule here.
3. A HARD CAP on open auto-cards. Hitting the cap IS the signal — it means
   triage has stalled — so it pages instead of filing more.

Cards are created ASSIGNED to the owning board's PM for triage (P8-R2, t_de9b87e0
— the earlier "created UNASSIGNED, six of seven gateways ESTOP'd" rationale no
longer holds; see assignee_for()). This still must never auto-dispatch WORK to
agents beyond the PM triaging it — a PM assignment is a triage inbox, not a
crash-wall.

Idempotency: card keys are `autoerr-<signature>`, so a double run is a no-op.

FALSE-GREEN GUARD (t_cebe04a4 / t_14bb1770, 2026-08-28): auto-close must NOT treat a
process-local counter reset as resolution. `DatabaseWritesDLQIngress` and
`DatabaseWriteQueueTerminalFkLoss` are keyed on `increase()` of process-local
monotonic counters (`sycodetrading_bullmq_queue_dlq_total`,
`sycodetrading_bullmq_queue_fk_terminal_total`) that reset to 0 when
`sycodetrading-server` restarts. On 2026-08-28 a 21:37Z server restart reset both
counters, the alerts briefly stopped firing, and this detector auto-closed the
critical card while the incident was still active (re-fired within ~1h). Two guards:
  1. CLOSE_GRACE_MIN: an alert card is only auto-closed after the alert has been
     continuously ABSENT for >= this many minutes (a brief gap — scrape hiccup,
     flap, or a restart's flat counter window — must not close a card).
  2. RESTART_SUPPRESS_MIN: if the sycodetrading-server restarted within this many
      minutes AND the alert was last seen at/after that restart, suppress auto-close
      entirely — the alert's disappearance is a counter-reset false-clear, not a
      genuine resolution.

DOWNTIME GUARD (t_fe627dc5, 2026-08-29): a full server outage is a SECOND, distinct
false-green vector. The restart guard above needs /ready to be reachable to read
proof.bootAt — when the whole sycodetrading-server is down, the probe fails and the
detector falls back to the plain close-grace path, so a metrics-scrape gap (alert
absent because its SOURCE is down, NOT because it resolved) still auto-closes a live
card. Two-part guard for alerts whose metrics source is the sycodetrading-server
(prometheus-alert records routed to the sycode-trading board):
  1. While /ready is UNREACHABLE-or-unhealthy, absence is UNKNOWN — suppress
     auto-close entirely (a server-down window must not count as resolution).
  2. After the server comes back, require it to have been continuously up >=
     CLOSE_GRACE_MIN before auto-close may run — the >=90m absence may have been
     entirely during the outage, so it cannot prove the alert stayed gone while
     the source was actually reporting.

NO-SUCH-TASK CLEANUP (t_9069f2aa): when the close path tries to complete a card that
does NOT exist on any board (`hermes kanban show` -> "no such task", rc != 0), that is
a stale-record cleanup, not a detector failure — drop the record instead of persisting
close_failed forever on a card that can never be completed.

Run `error-digest-to-cards.py --self-test` for a deterministic regression check.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

# Route by DOMAIN, not by keyword. Prometheus alerts carry job= / instance=
# labels naming the emitting service, which is far more reliable than guessing
# from the alertname — it correctly classifies KeyPipelineFreshnessStale as
# trading, which a keyword rule on the name gets wrong.
DEFAULT_BOARD = os.environ.get("ERROR_DIGEST_BOARD", "jarvis-os")
BOARD_ROUTES = [
    ("sycodetrading", "sycode-trading"),
    ("sycode-trading", "sycode-trading"),
]


def route_board(labels: dict) -> str:
    """Pick the board whose team owns this alert. Falls back to the fleet board."""
    hay = " ".join(str(labels.get(k, "")) for k in ("job", "instance", "service", "pipeline")).lower()
    for needle, board in BOARD_ROUTES:
        if needle in hay:
            return board
    return DEFAULT_BOARD


# P8-R2 (t_de9b87e0): cards used to land UNASSIGNED "for triage" — the original
# rationale was that six of seven gateways were ESTOP'd, so auto-dispatching
# into that was a crash-wall risk. That ESTOP condition is gone (verified live
# 2026-08-30: no ESTOP sentinel on any profile) and unassigned auto-error cards
# are exactly the ghost-card class the P8 gate measures (blank non-terminal
# assignee != 0). Route to the board's PM, same pattern as fleet-alert-card.sh
# (t_89678308) — a PM triaging an auto-error card is the intended behavior, not
# a risk. Unknown boards fall back to jarvis-os-pm.
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
STATE = Path(os.environ.get("ERROR_DIGEST_STATE",
                            "/home/frank/.hermes/state/auto-error-cards.json"))
SPOOL_ARCHIVE = Path("/home/frank/.hermes/profiles/jarvis/state/alertmanager-spool/archive")
MIN_STREAK = int(os.environ.get("ERROR_DIGEST_MIN_STREAK", "3"))   # ~1.5h at 30m
MAX_OPEN = int(os.environ.get("ERROR_DIGEST_MAX_OPEN", "12"))
# False-green guard (t_cebe04a4): auto-close requires the alert to be continuously
# absent for CLOSE_GRACE_MIN, and is suppressed entirely for RESTART_SUPPRESS_MIN
# after a detected sycodetrading-server restart (process-local counters reset).
CLOSE_GRACE_MIN = int(os.environ.get("ERROR_DIGEST_CLOSE_GRACE_MINUTES", "90"))
RESTART_SUPPRESS_MIN = int(os.environ.get("ERROR_DIGEST_RESTART_SUPPRESS_MINUTES", "60"))
READY_URL = os.environ.get("ERROR_DIGEST_READY_URL", "http://127.0.0.1:3001/ready")
SIG_RE = re.compile(r"[^A-Za-z0-9_.-]+")
# Cards in a terminal state can no longer be completed. When the detector maps a
# signature to a card that is ALREADY terminal (e.g. falsely auto-closed while the
# error is still firing), the mapping is stale and must be reconciled (t_e9849737).
# NOTE: ``blocked`` is deliberately NOT terminal — complete_task transitions
# ``blocked -> done``, so a blocked card is a valid completion source. Only the
# already-completed/archived states make complete fail with "terminal state".
TERMINAL_STATUSES = {"done", "cancelled", "archived"}


def sig(text: str) -> str:
    return SIG_RE.sub("-", text.strip())[:60].strip("-").lower()


# ``hermes kanban show`` prints ``no such task`` (rc != 0) for a card_id that does
# NOT exist on the board. Distinguishing this definitive no-such-task signal from a
# transient hermes failure is what lets the close path clean up a record whose card
# was deleted / never existed, instead of persisting close_failed forever on a card
# that can never be completed (t_9069f2aa).
MISSING_TASK_MARKERS = ("no such task", "task not found", "task does not exist")


def is_no_such_task(output: str) -> bool:
    """True when a `hermes kanban show` output reports the task does NOT exist."""
    lo = (output or "").lower()
    return any(m in lo for m in MISSING_TASK_MARKERS)


def card_status(card_id: str, board: str) -> tuple[str | None, bool]:
    """Best-effort live status of a kanban card.

    Returns (status, known_missing):
      status        — the lowercased status token, or None when it cannot be read.
      known_missing — True ONLY when the board definitively reported the card does
                      not exist (``no such task``, rc != 0 with that marker). This is
                      a state-cleanup signal, NOT a transient failure.
    ``hermes kanban show`` prints ``  status:    <value>`` for a known card and
    ``no such task`` (rc != 0) for an unknown id. (None, False) — status unreadable
    with no no-such-task marker — is treated conservatively by callers: it could be
    a transient hermes failure, so do NOT assume stale and do NOT treat as
    confirmed-open either.
    """
    rc, out = hermes("kanban", "--board", board, "show", card_id)
    if rc != 0:
        return None, is_no_such_task(out)
    m = re.search(r"^\s*status:\s*(\S+)", out, re.MULTILINE)
    return (m.group(1).strip().lower() if m else None), False


def classify_complete_failure(status: str | None, known_missing: bool = False) -> str:
    """Decide what a failed ``kanban complete`` means for the state record.

    - 'closed':    the complete call succeeded (handled by the caller, rc==0).
    - 'cleanup':   the complete failed because the card no longer exists on the
                   board (``known_missing`` — "no such task") OR is already in a
                   terminal state (done/cancelled/archived). Neither is a detector
                   failure: the card was deleted / never existed / closed elsewhere,
                   so the state record is stale. Treat as a state-cleanup no-op:
                   drop the record instead of persisting ``close_failed``
                   (t_e9849737, t_9069f2aa).
    - 'failed':    the complete failed for another reason (status unreadable with
                   no no-such-task marker, or a genuinely non-terminal status) — a
                   genuine close error that must surface as ``close_failed`` for
                   triage. ``None`` with ``known_missing=False`` stays conservative.
    """
    if known_missing:
        return "cleanup"
    if status in TERMINAL_STATUSES:
        return "cleanup"
    return "failed"


def hermes(*args: str, timeout: int = 90) -> tuple[int, str]:
    try:
        p = subprocess.run(["hermes", *args], capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:  # noqa: BLE001
        return 1, f"{type(e).__name__}: {e}"


def current_errors() -> dict[str, dict]:
    """Signature -> {kind, title, detail}. Sources are cheap and already trusted."""
    out: dict[str, dict] = {}

    # 1. Firing alerts, read LIVE from Alertmanager. Deliberately not parsed from
    #    the spool digest: that is a derived artifact whose newest file may be a
    #    quiet one, so the detector would go blind exactly when the fleet was
    #    briefly calm. Ask the source of truth.
    try:
        import urllib.request
        url = ("http://127.0.0.1:9093/api/v2/alerts"
               "?active=true&silenced=false&inhibited=false")
        with urllib.request.urlopen(url, timeout=10) as resp:
            alerts = json.loads(resp.read().decode())
    except Exception:
        alerts = []  # AM unreachable: report nothing rather than invent absence
    for alert in alerts if isinstance(alerts, list) else []:
        labels = alert.get("labels") or {}
        name = labels.get("alertname")
        if not name:
            continue
        sev = labels.get("severity", "")
        summary = ((alert.get("annotations") or {}).get("summary")
                   or (alert.get("annotations") or {}).get("description") or "")
        out[f"alert-{sig(name)}"] = {
            "kind": "prometheus-alert",
            "title": f"{name}" + (f" [{sev}]" if sev else ""),
            "detail": f"severity={sev or 'n/a'} startsAt={alert.get('startsAt','?')} "
                      f"job={labels.get('job','?')} summary={summary[:300]}",
            "board": route_board(labels),
        }

    # 2. Hermes cron jobs whose recent runs all failed. Exit code is the only
    #    liveness signal for no_agent jobs — stdout is never parsed.
    _seen_exec = set()
    for _store in glob.glob("/home/frank/.hermes/profiles/*/cron/executions.db"):
        if os.path.realpath(_store) in _seen_exec:
            continue  # symlink alias — dedupe
        _seen_exec.add(os.path.realpath(_store))
        store = _store
        profile = store.split("/")[-3]
        try:
            con = sqlite3.connect(f"file:{store}?mode=ro", uri=True, timeout=3.0)
            cols = {r[1] for r in con.execute("pragma table_info(executions)")}
            if not {"job_id", "status"} <= cols:
                con.close(); continue
            rows = con.execute(
                "select job_id, status, count(*) from executions"
                " where started_at >= datetime('now','-6 hours') group by 1,2"
            ).fetchall()
            con.close()
        except Exception:
            continue
        agg: dict[str, dict[str, int]] = {}
        for job, status, n in rows:
            agg.setdefault(job, {})[str(status)] = n
        for job, counts in agg.items():
            bad = sum(v for k, v in counts.items() if k.lower() not in {"completed", "success", "ok"})
            good = sum(v for k, v in counts.items() if k.lower() in {"completed", "success", "ok"})
            if bad >= 3 and good == 0:
                out[f"cron-{sig(profile)}-{sig(str(job))}"] = {
                    "kind": "cron-failure", "board": DEFAULT_BOARD,
                    "title": f"cron job '{job}' failing on profile '{profile}'",
                    "detail": f"{bad} failed runs, 0 successes in the last 6h ({counts})",
                }
    return out


def ready_probe() -> tuple[bool, float | None]:
    """Probe the sycodetrading-server /ready endpoint.

    Returns (reachable, boot_at_epoch_seconds):
      reachable — True ONLY when /ready answered HTTP 200 with a parseable JSON
                  body (the server is up AND serving health). False when the
                  endpoint is unreachable, non-200 (not ready yet), or the body
                  is unparseable — any state where an alert's absence cannot be
                  trusted as resolution (t_fe627dc5 downtime variant).
      boot_at   — the server's proof.bootAt epoch-seconds when present, else None.

    Callers must treat reachable=False as "metrics source may be down: hold
    auto-close" for server-derived alerts, and must never let a best-effort
    probe crash the detector.
    """
    try:
        import urllib.request
        with urllib.request.urlopen(READY_URL, timeout=5) as resp:
            if resp.status != 200:
                return False, None
            data = json.loads(resp.read().decode())
    except Exception:
        return False, None
    boot = data.get("bootAt")
    if boot is None:
        proof = data.get("proof") or {}
        boot = proof.get("bootAt")
    if boot is None:
        return True, None
    if isinstance(boot, (int, float)):
        return True, (boot / 1000.0 if boot > 1e12 else float(boot))
    s = str(boot)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    import datetime as _dt
    return True, _dt.datetime.fromisoformat(s).timestamp()


def should_auto_close(rec: dict, now: float, restart_boot_at: float | None = None,
                      ready_reachable: bool | None = None,
                      close_grace_min: int = CLOSE_GRACE_MIN,
                      restart_suppress_min: int = RESTART_SUPPRESS_MIN) -> tuple[bool, str | None]:
    """Decide whether an alert card may be auto-closed, guarding the false-green
    where a process-local monotonic counter reset at server restart looks like an
    incident resolution (t_cebe04a4 / t_14bb1770) and the DOWNTIME variant where
    the whole server is down so the alert's SOURCE is gone (t_fe627dc5).

    ready_reachable — True when the /ready probe succeeded (server up + healthy);
    False when it failed (server down / not ready / unparseable); None when not
    probed. For prometheus-alert records routed to the sycode-trading board (their
    metrics source is the sycodetrading-server), anything other than True holds
    the close: absence during a server-down window is UNKNOWN, not resolution.
    Also, once the server is back, it must have been continuously up for >=
    close_grace_min before a server-derived alert may close — otherwise the
    >=90m absence could be entirely downtime, not genuine resolution.

    Returns (should_close, reason). reason is a human-readable HOLD reason when
    should_close is False, else None.
    """
    last_seen = rec.get("last_seen", now)
    is_server_alert = (rec.get("kind") == "prometheus-alert"
                       and rec.get("board") == "sycode-trading")

    # 0. DOWNTIME GUARD (t_fe627dc5) — server-derived alerts only. While /ready is
    #    unreachable-or-unhealthy, the alert has stopped appearing because its
    #    SOURCE is down, NOT because the incident resolved. Absence is UNKNOWN,
    #    so auto-close is suppressed entirely (a metrics-scrape gap must not be
    #    read as resolution). This is the guard that was missing on 2026-08-29:
    #    the server was down ~07:30->09:17Z, /ready unreachable, and the detector
    #    fell back to the plain 90m grace path, falsely closing t_07cbad6c.
    if is_server_alert and ready_reachable is not True:
        return False, ("held: /ready unreachable or unhealthy; metrics source absent — "
                       "alert absence is UNKNOWN (downtime), not resolution")

    # 1. Restart suppression is the STRONGER guard. A sycodetrading-server restart
    #    resets the process-local counters these alerts are keyed on, so an alert
    #    that was firing at/after a recent restart "clears" purely because the
    #    counter reset to 0 — that is NOT a resolution. Suppress auto-close for any
    #    such card for restart_suppress_min after the boot, regardless of how long
    #    the alert has been gone.
    if restart_boot_at is not None and last_seen >= restart_boot_at:
        restart_min = (now - restart_boot_at) / 60.0
        if restart_min <= restart_suppress_min:
            return False, (f"held: server restarted {restart_min:.0f}m ago; "
                           "process-local counter reset false-clear risk")

    # 2. Confirmation grace window: treat as resolved only after the alert has
    #    been continuously absent for >= close_grace_min. A brief gap (scrape
    #    hiccup, flap, or a restart's flat counter window) must not close a card.
    gone_min = (now - last_seen) / 60.0
    if gone_min < close_grace_min:
        return False, f"held {gone_min:.0f}m < close-grace {close_grace_min}m"

    # 3. UPTIME VERIFICATION (t_fe627dc5, option b) — server-derived alerts only.
    #    The server that just came back from downtime cannot yet PROVE the alert
    #    stayed absent while it was reachable: the >=90m absence may have been
    #    entirely during the outage. Require the server to have been continuously
    #    up for >= close_grace_min (>=90m of verifiable absence) before closing,
    #    so the downtime window never counts as resolution.
    if is_server_alert and restart_boot_at is not None:
        up_min = (now - restart_boot_at) / 60.0
        if up_min < close_grace_min:
            return False, (f"held: server up only {up_min:.0f}m < close-grace "
                           f"{close_grace_min}m; downtime window cannot count as resolution")

    return True, None


def self_test() -> int:
    """Deterministic regression for the false-green guard. In-memory only — safe."""
    now = 1_800_000_000.0
    restart = now - 30 * 60  # server restarted 30m ago

    # T1: alert seen 0.5h ago, NO restart — must be held by the grace window
    #     (this is exactly the 2026-08-28 false-close: 0.5h gap closed a live card).
    rec = {"last_seen": now - 30 * 60}
    close, reason = should_auto_close(rec, now, None)
    assert not close, "T1: 0.5h absence must be HELD (grace window)"
    assert "close-grace" in (reason or ""), "T1 reason"

    # T2: alert seen AFTER a recent restart — must be suppressed by restart guard.
    rec = {"last_seen": now - 10 * 60}  # seen 10m ago (>= restart)
    close, reason = should_auto_close(rec, now, restart)
    assert not close, "T2: post-restart absence must NOT auto-close"
    assert "restarted" in (reason or ""), "T2 reason"

    # T3: alert last seen just BEFORE a recent restart, within the suppress window
    #     — must still be held (it was active around the restart; do not close).
    rec = {"last_seen": restart - 1}  # seen just before the restart
    close, reason = should_auto_close(rec, now, restart)
    assert not close, "T3: active-at-restart alert must be HELD"

    # T4: genuine resolution — alert absent 3h, no restart, past grace -> CLOSE.
    rec = {"last_seen": now - 3 * 3600}
    close, reason = should_auto_close(rec, now, None)
    assert close, "T4: genuine resolution must auto-close"
    assert reason is None, "T4 reason"

    # T5: genuine resolution AFTER the restart-suppress window elapses -> CLOSE.
    rec = {"last_seen": now - 3 * 3600}
    close, reason = should_auto_close(rec, now, now - 3 * 3600)  # restart 3h ago
    assert close, "T5: resolution past restart-suppress window must auto-close"
    assert reason is None, "T5 reason"

    # T6: unresolved (alert still present) is handled by caller, but verify a
    #     just-gone alert with no restart is held, not closed.
    rec = {"last_seen": now - 1}
    close, reason = should_auto_close(rec, now, None)
    assert not close, "T6: just-gone alert must be held"

    # T7 (t_e9849737): a complete that fails because the card is already in a
    #     terminal state must be treated as a state-cleanup no-op, NOT a failure.
    assert classify_complete_failure("done") == "cleanup", "T7: done -> cleanup"
    assert classify_complete_failure("cancelled") == "cleanup", "T7b: cancelled -> cleanup"
    assert classify_complete_failure("archived") == "cleanup", "T7c: archived -> cleanup"
    assert classify_complete_failure("ready") == "failed", "T7d: ready -> failed"
    assert classify_complete_failure("running") == "failed", "T7e: running -> failed"
    assert classify_complete_failure(None) == "failed", "T7f: unknown -> failed (conservative)"
    #     and a stale terminal card_id is a reconciliation target on the OPEN path.
    assert "done" in TERMINAL_STATUSES and "ready" not in TERMINAL_STATUSES, "T7g: terminal set"

    # T8 (t_9069f2aa): a complete that fails because the card does NOT exist on any
    #     board ("no such task") must be treated as a state-cleanup no-op, NOT a
    #     failure — otherwise close_failed persists forever on a card that can never
    #     be completed (the alert-decisionloglatencyp99 -> t_f301eb93 residue).
    assert classify_complete_failure(None, known_missing=True) == "cleanup", \
        "T8: no-such-task -> cleanup"
    assert classify_complete_failure(None, known_missing=False) == "failed", \
        "T8b: unreadable (no no-such-task marker) -> failed (conservative)"
    assert classify_complete_failure("ready", known_missing=False) == "failed", \
        "T8c: ready -> failed"

    # T9 (t_9069f2aa): the no-such-task marker detection must fire on the exact
    #     strings `hermes kanban show` emits for an unknown id and stay quiet on a
    #     normal status read and on a transient (non-no-such-task) failure.
    assert is_no_such_task("no such task: t_f301eb93\n"), "T9a: 'no such task' marker"
    assert is_no_such_task("ERROR: task not found: t_f301eb93\n"), "T9b: 'task not found' marker"
    assert not is_no_such_task("  status:    done\n"), "T9c: normal output is not missing"
    assert not is_no_such_task("timeout talking to board"), "T9d: transient failure not missing"

    # --- DOWNTIME GUARD (t_fe627dc5, 2026-08-29) --------------------------------
    # A server-derived alert is a prometheus-alert routed to the sycode-trading
    # board. Its absence while /ready is unreachable is UNKNOWN (source down),
    # and right after a boot the server cannot yet PROVE the absence — the
    # >=90m gap may have been entirely downtime. Both must hold, not close.
    server_alert = {"kind": "prometheus-alert", "board": "sycode-trading"}

    # T10: THE 2026-08-29 FALSE GREEN — server down, /ready unreachable, alert
    #     absent 3h (past the 90m grace) -> MUST be HELD. Previously the detector
    #     fell back to the plain grace path and closed t_07cbad6c 1m before the
    #     server came back (09:16:36Z vs boot 09:17:41Z).
    rec = dict(server_alert, last_seen=now - 3 * 3600)
    close, reason = should_auto_close(rec, now, None, ready_reachable=False)
    assert not close, "T10: server-down absence must NOT auto-close (downtime)"
    assert "UNKNOWN" in (reason or ""), "T10 reason"

    # T11: genuine resolution — server up 4h (past grace), /ready reachable, alert
    #     absent 3h -> CLOSE. The downtime guard must not trap real resolutions.
    rec = dict(server_alert, last_seen=now - 3 * 3600)
    close, reason = should_auto_close(rec, now, now - 4 * 3600, ready_reachable=True)
    assert close, "T11: genuine resolution must auto-close (server up past grace)"
    assert reason is None, "T11 reason"

    # T12: server JUST came back (up 30m), /ready reachable, alert absent 3h ->
    #     HELD by uptime verification: the 3h gap may be entirely downtime, so it
    #     cannot count as resolution (option b: >=90m of verifiable absence).
    rec = dict(server_alert, last_seen=now - 3 * 3600)
    close, reason = should_auto_close(rec, now, now - 30 * 60, ready_reachable=True)
    assert not close, "T12: post-downtime absence must stay HELD until server up >= grace"
    assert "up only" in (reason or ""), "T12 reason"

    # T13: guard must NOT trap non-server records — a cron failure can genuinely
    #     resolve while the sycode server is down.
    rec = {"kind": "cron-failure", "board": "jarvis-os", "last_seen": now - 3 * 3600}
    close, reason = should_auto_close(rec, now, None, ready_reachable=False)
    assert close, "T13: cron-failure absent 3h closes even when /ready is down"
    assert reason is None, "T13 reason"

    # T14: guard scoped to the sycode-trading board — a fleet-board prometheus
    #     alert whose source is NOT the sycodetrading-server must still close.
    rec = {"kind": "prometheus-alert", "board": "jarvis-os", "last_seen": now - 3 * 3600}
    close, reason = should_auto_close(rec, now, None, ready_reachable=False)
    assert close, "T14: non-sycode prometheus alert closes even when /ready is down"
    assert reason is None, "T14 reason"

    # T15: ready_reachable=None (not probed) defaults conservative for server
    #     alerts — absence cannot be verified, so hold.
    rec = dict(server_alert, last_seen=now - 3 * 3600)
    close, reason = should_auto_close(rec, now, None, ready_reachable=None)
    assert not close, "T15: unprobed /ready must NOT auto-close server alert"
    assert "UNKNOWN" in (reason or ""), "T15 reason"

    # T16: restart guard still works for server alerts (both guards coexist) —
    #     alert last seen after a recent restart, /ready up -> restart suppression.
    rec = dict(server_alert, last_seen=now - 10 * 60)
    close, reason = should_auto_close(rec, now, now - 30 * 60, ready_reachable=True)
    assert not close, "T16: post-restart server alert must be HELD by restart guard"
    assert "restarted" in (reason or ""), "T16 reason"
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--self-test", action="store_true",
                    help="Run deterministic false-green-guard regression tests (in-memory, safe).")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    STATE.parent.mkdir(parents=True, exist_ok=True)
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    errors = current_errors()
    now = time.time()
    opened, closed, capped, held, held_close, stale_cleared, cleaned = [], [], [], [], [], [], []
    # Best-effort /ready probe: gives (reachable, boot_at). reachable=False means
    # the server is down / not ready — server-derived alerts must then HOLD
    # (downtime guard, t_fe627dc5); boot_at drives the restart suppression
    # (t_cebe04a4) and the post-downtime uptime verification.
    ready_reachable, restart_boot_at = ready_probe()

    # Cap per board: a noisy trading day must not consume the fleet board's budget.
    per_board = {}
    for v in state.values():
        if v.get("card_id"):
            per_board[v.get("board", DEFAULT_BOARD)] = per_board.get(v.get("board", DEFAULT_BOARD), 0) + 1
    open_count = sum(per_board.values())

    # --- errors that are still present -------------------------------------
    for signature, info in errors.items():
        rec = state.setdefault(signature, {"streak": 0, "card_id": None,
                                           "first_seen": now, "kind": info["kind"]})
        rec["streak"] = int(rec.get("streak", 0)) + 1
        rec["last_seen"] = now
        rec["title"] = info["title"]
        # STALE-CARD RECONCILIATION (t_e9849737): if the tracked card is already in
        # a terminal state while the error is STILL firing, the mapping is stale —
        # e.g. the card was falsely auto-closed (pre-fix) or closed by hand. Clear
        # the stale card_id so a replacement card tracks the live error. Otherwise
        # the open path would skip forever on the dead card_id.
        tracked = rec.get("card_id")
        if tracked:
            st, known_missing = card_status(tracked, info.get("board", DEFAULT_BOARD))
            if known_missing or st in TERMINAL_STATUSES:
                stale_cleared.append(f"{signature}: cleared stale card {tracked} "
                                     f"({'no such task' if known_missing else st})")
                rec["card_id"] = None
                rec["retrack"] = int(rec.get("retrack", 0)) + 1
                tracked = None
                # The stale card was counted toward the per-board cap at the top of
                # the run; drop it so its own re-track (below) is not self-capped.
                _tgt = info.get("board", DEFAULT_BOARD)
                per_board[_tgt] = max(0, per_board.get(_tgt, 0) - 1)
        if rec.get("card_id") or rec["streak"] < MIN_STREAK:
            if not rec.get("card_id"):
                held.append(f"{signature} (streak {rec['streak']}/{MIN_STREAK})")
            continue
        tgt = info.get("board", DEFAULT_BOARD)
        if per_board.get(tgt, 0) >= MAX_OPEN:
            capped.append(f"{signature} (board {tgt} at cap)")
            continue
        hours = (now - rec.get("first_seen", now)) / 3600
        body = (
            f"Opened automatically after this error persisted across {rec['streak']} "
            f"consecutive checks (~{hours:.1f}h).\n\n"
            f"SOURCE: {info['kind']}\nDETAIL: {info['detail']}\n\n"
            f"This card was assigned to {assignee_for(tgt)} for triage.\n"
            "It will be CLOSED AUTOMATICALLY by the detector when the error stops "
            "appearing — if you fix the cause, you do not need to close it by hand."
        )
        if args.dry_run:
            opened.append(f"[dry] [{info.get('board', DEFAULT_BOARD)}] {signature}: {info['title']}")
            continue
        board = info.get("board", DEFAULT_BOARD)
        rec["board"] = board
        # IDEMPOTENCY-KEY COLLISION AVOIDANCE (t_e9849737): the plain
        # ``autoerr-{signature}`` key is bound to the OLD (now-terminal) card, so
        # re-creating with it would return that dead card instead of a fresh one.
        # A re-track after clearing a stale card uses a generation-suffixed key so
        # a genuinely new card is created to track the live error.
        key = f"autoerr-{signature}"
        if rec.get("retrack"):
            key = f"autoerr-{signature}-rt{rec['retrack']}"
        rc, out = hermes("kanban", "--board", board, "create",
                         f"[auto-error] {info['title']}", "--body", body,
                         "--assignee", assignee_for(board),
                         "--idempotency-key", key)
        m = re.search(r"\b(t_[0-9a-f]{8})\b", out)
        if rc == 0 and m:
            rec["card_id"] = m.group(1)
            opened.append(f"{m.group(1)} [{board}] {info['title']}")
            per_board[board] = per_board.get(board, 0) + 1

    # --- errors that have CLEARED: close their card ------------------------
    for signature, rec in list(state.items()):
        if signature in errors:
            continue
        rec["streak"] = 0
        card = rec.get("card_id")
        if not card:
            state.pop(signature, None)
            continue
        gone_h = (now - rec.get("last_seen", now)) / 3600
        close_ok, hold_reason = should_auto_close(rec, now, restart_boot_at, ready_reachable)
        if not close_ok:
            held_close.append(f"{card} {rec.get('title', signature)} ({hold_reason})")
            continue
        if args.dry_run:
            closed.append(f"[dry] {card} {rec.get('title', signature)}")
            continue
        rc, _ = hermes("kanban", "--board", rec.get("board", DEFAULT_BOARD),
                       "complete", card,
                       "--summary", f"Auto-closed: '{rec.get('title', signature)}' "
                                    f"stopped appearing {gone_h:.1f}h ago. Closed by the "
                                    f"same detector that opened it.")
        if rc == 0:
            closed.append(f"{card} {rec.get('title', signature)}")
            state.pop(signature, None)
        else:
            # STALE-CARD CLEANUP (t_e9849737, t_9069f2aa): if complete failed because
            # the card is already in a terminal state (done/cancelled/archived — closed
            # elsewhere or falsely auto-closed pre-fix) OR because the card does NOT
            # exist on any board (``no such task`` — deleted/never existed), this is a
            # state-cleanup no-op, not a detector failure. Drop the stale record
            # instead of persisting ``close_failed`` (which leaks state and mislabels
            # the detector forever on a card that can never be completed). A transient
            # hermes failure (None status with NO no-such-task marker) stays
            # conservative and still records close_failed.
            st, known_missing = card_status(card, rec.get("board", DEFAULT_BOARD))
            if classify_complete_failure(st, known_missing) == "cleanup":
                cleaned.append(f"{card} {rec.get('title', signature)} "
                               f"({'no such task' if known_missing else f'already {st}'})")
                state.pop(signature, None)
            else:
                rec["close_failed"] = True

    if not args.dry_run:
        STATE.write_text(json.dumps(state, indent=1, sort_keys=True))

    lines = []
    if opened:
        lines += ["🆕 auto-error cards opened:"] + [f"   {x}" for x in opened]
    if closed:
        lines += ["✅ auto-error cards closed (error cleared):"] + [f"   {x}" for x in closed]
    if held_close:
        lines += ["⏸️ auto-close HELD (false-green guard: restart / grace window):"] + \
                 [f"   {x}" for x in held_close]
    if stale_cleared:
        lines += ["♻️ stale card_id reconciled (terminal card, error still firing):"] + \
                 [f"   {x}" for x in stale_cleared]
    if cleaned:
        lines += ["🧹 stale card_id cleaned (card already terminal on close):"] + \
                 [f"   {x}" for x in cleaned]
    if capped:
        lines += [f"🛑 AUTO-ERROR CAP REACHED ({MAX_OPEN} open). {len(capped)} new error "
                  f"classes NOT filed: {', '.join(capped[:6])}",
                  "   The cap is the signal: triage has stalled. Clear open auto-error "
                  "cards before more are created."]
    # stdout is the delivered message under cron --no-agent; empty = silent.
    if lines:
        print("\n".join(lines))
    print(json.dumps({"status": "ok", "errors_seen": len(errors), "opened": len(opened),
                      "closed": len(closed), "capped": len(capped), "held": held,
                      "held_close": held_close, "stale_cleared": stale_cleared,
                      "cleaned": cleaned, "ready_reachable": ready_reachable,
                      "restart_boot_at": restart_boot_at,
                      "open_total": open_count}, indent=1), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
