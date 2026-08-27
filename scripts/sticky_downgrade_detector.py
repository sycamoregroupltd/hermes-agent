#!/usr/bin/env python3
"""Detect silent sticky model downgrades in Hermes agent sessions.

A capacity-503 on the pinned primary activates ``fallback_providers``.  The
fallback is only *turn*-scoped (``restore_primary_runtime``), so a long agentic
turn finishes on the fallback, and any background fork started from that turn
inherits the demoted model as its OWN primary (``_current_main_runtime()``
returns the LIVE model).  All of this is logged at INFO, so nothing ever errors
and the run passes as green while executing off-pin.

This detector reads agent.log, reconstructs per-session model usage, and alerts
when off-pin tokens or off-pin cost cross a threshold.

Liveness contract: this script's EXIT CODE is the only signal a no_agent cron
job records.  Parse/probe failures therefore exit non-zero — a run that could
not measure must never look like a clean run.  Exit 0 = measured, under
threshold.  Exit 2 = downgrade over threshold (alert path).  Exit 1 = the
detector itself failed.

Usage:
  sticky_downgrade_detector.py                # incremental (cron): new lines only
  sticky_downgrade_detector.py --all          # scan whole log (backfill/audit)
  sticky_downgrade_detector.py --since-hours 24
  sticky_downgrade_detector.py --dry-run      # never send, print report
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta

HOME = os.path.expanduser("~")
LOG_FILE = os.environ.get("SDD_AGENT_LOG", f"{HOME}/.hermes/logs/agent.log")
# The fleet runs under PROFILES, each with its own agent.log. Watching only the
# default-profile log makes this detector a false-green machine: on 2026-08-05
# the root log had been idle since 14:04 while 68 profile logs were live.
PROFILE_GLOB = os.environ.get("SDD_PROFILE_GLOB", f"{HOME}/.hermes/profiles/*/logs/agent.log")
STATE_FILE = os.environ.get("SDD_STATE", f"{HOME}/.hermes/state/sticky-downgrade-detector.json")
OUT_LOG = os.environ.get("SDD_LOG", f"{HOME}/.hermes/logs/sticky-downgrade-detector.log")
# System crontab PATH lacks ~/.local/bin, so a bare "hermes" raised FileNotFoundError and every
# BREACH alert logged ALERT-EXC/ALERT-UNDELIVERED (observed 2026-08-24 21:00Z). Resolve once, fail loud.
HERMES_BIN = (
    os.environ.get("SDD_HERMES_BIN")
    or shutil.which("hermes")
    or next((c for c in (f"{HOME}/.local/bin/hermes", "/usr/local/bin/hermes") if os.path.exists(c)), "hermes")
)
PRICE_OVERRIDE = os.environ.get("SDD_PRICES", f"{HOME}/.hermes/state/model-prices.json")

ALERT_TARGET = os.environ.get("SDD_ALERT_TARGET", "whatsapp:Frank")
ALERT_FALLBACKS = os.environ.get(
    "SDD_ALERT_FALLBACKS", "discord:#critical-alerts telegram:506972405"
).split()
REALERT_SECS = int(os.environ.get("SDD_REALERT_SECS", "21600"))

# Alert when a window's off-pin traffic exceeds EITHER threshold.
#
# Calibrated against the measured 2026-08-05 fleet baseline: ~1.35e9 off-pin
# tokens/day (~14M per 15-min run) at a 1.8x aggregate, essentially all of it
# deepseek->qwen at 2.4x. Thresholds tighter than that would page every single
# run, which is a silenced alert with extra steps. These are set to catch what
# is genuinely worse than the known-bad steady state:
#   - any session on an EXPENSIVE tier (gpt-5.6-luna 10x/30x, nemotron 48x/144x)
#   - a token spike ~3.5x above the current per-run average
# Tighten SDD_COST_MULTIPLE back toward 2.0 once the steady state is fixed,
# otherwise this detector silently normalises the very thing it was built for.
OFFPIN_TOKEN_THRESHOLD = int(os.environ.get("SDD_TOKEN_THRESHOLD", "50000000"))
OFFPIN_COST_MULTIPLE = float(os.environ.get("SDD_COST_MULTIPLE", "3.0"))

# $/token (prompt, completion, cache_read). Source: Nous catalog /v1/models,
# read 2026-08-05. Override by writing the same shape to SDD_PRICES.
# An UNPRICED model is reported as unknown and forces an alert — never 0.
PRICES = {
    "deepseek/deepseek-v4-flash-0731":   (0.01e-6, 0.02e-6, 0.0),
    "deepseek/deepseek-v4-flash":        (0.01e-6, 0.02e-6, 0.0),
    "deepseek/deepseek-v4-pro":          (0.28e-6, 0.42e-6, 0.028e-6),
    "qwen/qwen3.7-flash":                (0.024e-6, 0.104e-6, 0.0048e-6),
    "openai/gpt-5.6-luna":               (0.10e-6, 0.60e-6, 0.01e-6),
    "nvidia/nemotron-3-ultra-550b-a55b": (0.48e-6, 2.88e-6, 0.16e-6),
    "moonshotai/kimi-k3":                (0.06e-6, 0.25e-6, 0.006e-6),
    "tencent/hy3:free":                  (0.0, 0.0, 0.0),
    "stepfun/step-3.7-flash:free":       (0.0, 0.0, 0.0),
    # Cheap-chain tiers installed 2026-08-05 across all 69 configs. Every one
    # is <= the primary's blended cost, so a demotion can no longer cost more
    # than staying on-pin.
    "inclusionai/ling-3.0-flash:free":   (0.0, 0.0, 0.0),
    "poolside/laguna-s-2.1:free":        (0.0, 0.0, 0.0),
    "poolside/laguna-xs-2.1:free":       (0.0, 0.0, 0.0),
    "inclusionai/ling-2.6-flash":        (0.008e-6, 0.024e-6, 0.0016e-6),
}

# Catalog ALIASES that resolve to the same served model. Without this the
# detector fabricates downgrades: a session moving between "~deepseek/
# deepseek-v4-flash-latest" and "deepseek/deepseek-v4-flash-0731" never changed
# served model at all (probed 2026-08-05 — the alias returns 0731 on Novita).
# Keys and values are compared AFTER this mapping.
ALIASES = {
    "~deepseek/deepseek-v4-flash-latest": "deepseek/deepseek-v4-flash-0731",
    "deepseek/deepseek-v4-flash": "deepseek/deepseek-v4-flash-0731",
}


def canon(model: str) -> str:
    return ALIASES.get(model, model)


CALL_RE = re.compile(
    r"^(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d).*?\[(?P<sid>\d{8}_\d{6}_[0-9a-f]+)\].*?"
    r"API call #(?P<n>\d+): model=(?P<model>\S+) provider=(?P<prov>\S+) "
    r"in=(?P<tin>\d+) out=(?P<tout>\d+) total=(?P<tot>\d+)"
    r"(?: cache=(?P<cached>\d+)/(?P<cachetot>\d+))?"
)
FB_RE = re.compile(
    r"^(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d).*?\[(?P<sid>\d{8}_\d{6}_[0-9a-f]+)\].*?"
    r"Fallback activated: (?P<frm>\S+) → (?P<to>\S+)"
)


def log(msg: str) -> None:
    os.makedirs(os.path.dirname(OUT_LOG), exist_ok=True)
    with open(OUT_LOG, "a") as fh:
        fh.write(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}\n")


def load_state() -> dict:
    try:
        with open(STATE_FILE) as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_state(st: dict) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(st, fh, indent=2)
    os.replace(tmp, STATE_FILE)


def load_prices() -> dict:
    prices = dict(PRICES)
    try:
        with open(PRICE_OVERRIDE) as fh:
            for k, v in json.load(fh).items():
                prices[k] = tuple(v)
        log(f"price override applied from {PRICE_OVERRIDE}")
    except FileNotFoundError:
        pass
    except Exception as exc:
        log(f"WARN price override unreadable ({exc}) — using built-in table")
    return prices


def discover_logs(args):
    """Every agent.log the fleet writes: default profile + one per profile."""
    if args.log_file:
        return [("(explicit)", args.log_file)]
    import glob
    found = []
    if os.path.exists(LOG_FILE):
        found.append(("default", LOG_FILE))
    for path in sorted(glob.glob(PROFILE_GLOB)):
        # .../profiles/<name>/logs/agent.log -> <name>
        found.append((os.path.basename(os.path.dirname(os.path.dirname(path))), path))
    return found


def read_new_lines(args, st: dict, path: str):
    """Return (lines, new_state_for_path). Handles rotation via inode+size."""
    stat = os.stat(path)
    inode, size = stat.st_ino, stat.st_size
    prev = (st.get("files") or {}).get(path, {})
    offset = 0
    if not args.all and args.since_hours is None:
        if prev.get("inode") == inode and isinstance(prev.get("offset"), int):
            offset = min(prev["offset"], size)
        elif prev.get("inode") is not None:
            log(f"{path}: rotated (inode {prev.get('inode')} -> {inode}) — rescanning from 0")
    with open(path, "rb") as fh:
        fh.seek(offset)
        raw = fh.read()
    lines = raw.decode("utf-8", errors="replace").split("\n")
    if args.since_hours is not None:
        cutoff = (datetime.now() - timedelta(hours=args.since_hours)).strftime("%Y-%m-%d %H:%M:%S")
        lines = [ln for ln in lines if ln[:19] >= cutoff]
    return lines, {"inode": inode, "offset": size}


def analyse(lines, prices):
    sessions = defaultdict(list)
    activations = []
    for ln in lines:
        m = CALL_RE.match(ln)
        if m:
            sessions[m.group("sid")].append(
                dict(
                    n=int(m.group("n")), ts=m.group("ts"), model=canon(m.group("model")),
                    tin=int(m.group("tin")), tout=int(m.group("tout")),
                    cached=int(m.group("cached") or 0),
                )
            )
            continue
        f = FB_RE.match(ln)
        if f:
            activations.append((f.group("ts"), f.group("sid"), f.group("frm"), f.group("to")))
    return sessions, activations


def cost(model, tin, tout, cached, prices):
    p = prices.get(model)
    if p is None:
        return None
    fresh = max(tin - cached, 0)
    return fresh * p[0] + cached * p[2] + tout * p[1]


def build_report(sessions, activations, prices):
    """Baseline = the model a session's FIRST call used (its effective pin)."""
    rows, unpriced = [], set()
    for (profile, sid), calls in sessions.items():
        calls.sort(key=lambda c: c["n"])
        baseline = calls[0]["model"]
        off = [c for c in calls if c["model"] != baseline]
        if not off:
            continue
        actual = counterfactual = 0.0
        for c in calls:
            a = cost(c["model"], c["tin"], c["tout"], c["cached"], prices)
            b = cost(baseline, c["tin"], c["tout"], c["cached"], prices)
            if a is None:
                unpriced.add(c["model"])
                continue
            if b is None:
                unpriced.add(baseline)
                continue
            actual += a
            counterfactual += b
        rows.append(
            dict(
                sid=sid, profile=profile, started=calls[0]["ts"], baseline=baseline,
                ended_on=calls[-1]["model"],
                off_models=sorted({c["model"] for c in off}),
                calls=len(calls), off_calls=len(off),
                off_tokens=sum(c["tin"] + c["tout"] for c in off),
                actual=actual, counterfactual=counterfactual,
                multiple=(actual / counterfactual) if counterfactual > 0 else None,
            )
        )
    rows.sort(key=lambda r: r["started"])
    return rows, unpriced


def send_alert(key, subject, body, st, dry_run):
    if dry_run:
        print(f"[DRY-RUN] would alert key={key}\n  {subject}\n{body}")
        return True
    last = (st.get("alerts") or {}).get(key)
    if last and (time.time() - last) < REALERT_SECS:
        log(f"SUPPRESSED key={key} (re-alert window)")
        return True
    delivered = False
    for target in [ALERT_TARGET] + ALERT_FALLBACKS:
        pretty = subject if target == ALERT_TARGET else f"🔁 FAILOVER: {subject}"
        try:
            rc = subprocess.run(
                [HERMES_BIN, "send", "-q", "-t", target, "-s", pretty, body],
                capture_output=True, timeout=60,
            ).returncode
        except Exception as exc:
            log(f"ALERT-EXC target={target} key={key} {exc}")
            continue
        if rc == 0:
            delivered = True
            log(f"ALERT-SENT target={target} key={key} subject={subject}")
            break
        log(f"ALERT-FAILED target={target} rc={rc} key={key}")
    # Arm the throttle ONLY on confirmed delivery — an alert that reached nobody
    # must not buy 6h of silence (same rule as system-crontab-watchdog.sh).
    if delivered:
        st.setdefault("alerts", {})[key] = time.time()
    else:
        log(f"ALERT-UNDELIVERED key={key} — throttle NOT armed, retrying next run")
    return delivered


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="scan the whole log")
    ap.add_argument("--since-hours", type=float, default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--log-file", default=None)
    args = ap.parse_args()

    global LOG_FILE
    if args.log_file:
        LOG_FILE = args.log_file

    st = load_state()
    prices = load_prices()
    logs = discover_logs(args)
    if not logs:
        log(f"FATAL no agent logs found (root={LOG_FILE} glob={PROFILE_GLOB})")
        print("FATAL: no agent logs found", file=sys.stderr)
        return 1

    sessions, activations, scanned, new_files, failed = {}, [], 0, {}, []
    for profile, path in logs:
        try:
            lines, new_state = read_new_lines(args, st, path)
        except Exception as exc:
            # A log we cannot read is UNKNOWN, never "clean" — record and fail.
            failed.append(f"{profile}:{path} ({exc})")
            continue
        scanned += len(lines)
        new_files[path] = new_state
        s, a = analyse(lines, prices)
        for sid, calls in s.items():
            sessions[(profile, sid)] = calls
        activations.extend((ts, f"{profile}/{sid}", frm, to) for ts, sid, frm, to in a)

    rows, unpriced = build_report(sessions, activations, prices)
    print(f"logs watched: {len(logs)} (default + {len(logs) - 1} profile)")
    if failed:
        print(f"UNREADABLE logs: {len(failed)} -> {'; '.join(failed[:3])}")
    off_tokens = sum(r["off_tokens"] for r in rows)
    actual = sum(r["actual"] for r in rows)
    counterfactual = sum(r["counterfactual"] for r in rows)
    multiple = (actual / counterfactual) if counterfactual > 0 else 1.0

    print(f"scanned {scanned} new log lines; {len(sessions)} sessions with API calls")
    print(f"fallback activations: {len(activations)}")
    print(f"downgraded sessions : {len(rows)}")
    if rows:
        print(f"\n{'started':20s} {'pin -> off-pin model(s)':56s} {'off':>4s} {'off-tok':>10s} "
              f"{'actual$':>9s} {'pin$':>9s} {'x':>6s}")
        for r in rows:
            arrow = f"[{r['profile']}] {r['baseline']} -> {','.join(r['off_models'])}"
            mult = "n/a" if r["multiple"] is None else f"{r['multiple']:.1f}x"
            print(f"{r['started']:20s} {arrow:56s} {r['off_calls']:4d} {r['off_tokens']:10,d} "
                  f"{r['actual']:9.4f} {r['counterfactual']:9.4f} {mult:>6s}")
        print(f"\nTOTAL off-pin tokens {off_tokens:,}  actual ${actual:.4f}  "
              f"if-pinned ${counterfactual:.4f}  ({multiple:.1f}x aggregate)")
    for a in activations[-10:]:
        print(f"  activation {a[0]} {a[1]} {a[2]} -> {a[3]}")
    if len(activations) > 10:
        print(f"  ... {len(activations) - 10} earlier activations not shown")

    # Breach is evaluated PER SESSION, never only on the aggregate: a cheap or
    # free downgrade (hy3:free at 0.2x) otherwise averages an expensive one
    # (nemotron at 12.7x) back under the threshold and reports clean. Proven
    # against real history 2026-08-05 — the aggregate read 0.9x while a 12.7x
    # session sat inside it.
    breached = []
    hot = [r for r in rows if r["multiple"] is not None and r["multiple"] > OFFPIN_COST_MULTIPLE]
    if hot:
        worst_x = max(r["multiple"] for r in hot)
        breached.append(
            f"{len(hot)} session(s) over {OFFPIN_COST_MULTIPLE}x cost (worst {worst_x:.1f}x)"
        )
    if off_tokens > OFFPIN_TOKEN_THRESHOLD:
        breached.append(f"off-pin tokens {off_tokens:,} > {OFFPIN_TOKEN_THRESHOLD:,}")
    if unpriced:
        breached.append(f"UNPRICED model(s) seen: {', '.join(sorted(unpriced))}")
    if failed:
        breached.append(f"{len(failed)} unreadable agent log(s) — coverage incomplete")

    rc = 0
    if breached:
        worst = max(rows, key=lambda r: (r["multiple"] or 0, r["off_tokens"])) if rows else None
        worst_desc = f"{worst['multiple']:.1f}x" if worst and worst["multiple"] else f"{multiple:.1f}x"
        subject = f"⚠️ Hermes sticky downgrade: {len(rows)} session(s) off-pin (worst {worst_desc})"
        body_lines = ["Reason: " + "; ".join(breached), ""]
        for r in sorted(rows, key=lambda r: -(r["multiple"] or 0))[:8]:
            mult = "n/a" if r["multiple"] is None else f"{r['multiple']:.1f}x"
            body_lines.append(
                f"[{r['profile']}] {r['started']} {r['sid']}: {r['baseline']} -> "
                f"{','.join(r['off_models'])} "
                f"({r['off_calls']}/{r['calls']} calls off-pin, {r['off_tokens']:,} tok, {mult})"
            )
        if worst:
            body_lines += ["", f"Worst: {worst['sid']} -> {','.join(worst['off_models'])}"]
        body_lines += ["", f"Detector: {os.path.abspath(__file__)}", f"Log: {OUT_LOG}"]
        body = "\n".join(body_lines)
        # Key on the set of affected sessions so a NEW downgrade re-alerts
        # immediately instead of being swallowed by the previous throttle.
        key = "sticky_downgrade:" + ",".join(sorted(f"{r['profile']}/{r['sid']}" for r in rows))[:120]
        send_alert(key, subject, body, st, args.dry_run)
        log(f"BREACH sessions={len(rows)} off_tokens={off_tokens} multiple={multiple:.2f} "
            f"reasons={breached}")
        rc = 2
    else:
        log(f"OK scanned={scanned} sessions={len(sessions)} downgraded={len(rows)} "
            f"off_tokens={off_tokens} multiple={multiple:.2f}")

    # Only advance the watermark on a successful scan, and never in a mode that
    # deliberately re-reads history (--all / --since-hours) or a dry run.
    if not args.dry_run and not args.all and args.since_hours is None:
        st.setdefault("files", {}).update(new_files)
        save_state(st)
    if failed:
        return max(rc, 2)
    return rc


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # never let a parse bug read as a clean run
        log(f"FATAL detector raised: {exc!r}")
        print(f"FATAL: {exc!r}", file=sys.stderr)
        sys.exit(1)
