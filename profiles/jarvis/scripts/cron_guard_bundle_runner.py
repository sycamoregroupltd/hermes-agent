#!/usr/bin/env python3
"""cron_guard_bundle_runner.py — shared no-agent watchdog bundle runner.

CONDENSE 1/4 (kanban t_db689c47). Collapses 38 guard/watchdog/probe/liveness
cron jobs in the jarvis store into 4 cadence bundles (5m/15m/hourly/daily),
each driven by a thin .sh wrapper that execs this runner with a bundle name.
The manifest also contains one separately justified additive DB-size guard
(`kanban-db-size-guard`), not one of the 38 paused source jobs; its inclusion
is documented in the condensation evidence note and uses the same failure path.

WATCHDOG SEMANTICS
------------------
* Each absorbed check keeps its ORIGINAL cadence (interval-based gating via a
  per-check last-run state file), independent of the bundle's fire minute. So
  coverage is preserved exactly even though the scheduler only fires 4 jobs.
* A check is run only when `now - last_run >= its interval`.
* On a check exit code of 0 (healthy) its stdout/stderr is SUPPRESSED — a no-agent
  job must be silent on success.
* On a check exit code != 0 (failure) its output is COLLECTED into an aggregate
  report. At the end, if any check failed, the report is printed and this runner
  exits 1 (non-zero exit = cron alert). If all due checks passed, empty stdout +
  exit 0 = fully silent.
* A check that times out (or crashes) is recorded as a failure (never silent).

This preserves the alert-on-nonzero semantics of the absorbed validators
(e.g. dgx-fleet-chain-validator, data-freshness-probe) verbatim.

State file: <profile_cron>/state/guard_bundle_last_run.json  (per-check last-run)

RESILIENCE FIX (2026-08-29, t_74f47880 CRON-HEALTH→ACTION)
----------------------------------------------------------
The original design had three structural flaws that produced a runaway-spawn
storm (365+ concurrent runner processes, load 200+):

1. STATE ONLY SAVED AT THE END. The scheduler/report-to-board kills a script
   job after 600s. The runner ran checks serially with per-check timeouts up to
   600s; any slow/hung check pushed the bundle past the job's 600s cap, the job
   was killed MID-RUN, the state file was NEVER written, so the next tick
   re-ran EVERY check. With one hung check that means an unbounded retry loop.
   -> FIX: state is saved INCREMENTALLY after each check completes. A killed
   run preserves all completed checks; the next tick only re-runs what was
   unfinished. The retry loop converges instead of compounding.

2. NO SINGLE-INSTANCE GUARD. Every scheduler fire spawned a fresh runner even
   when a previous instance was still alive, so blocked runs stacked.
   -> FIX: an flock-based single-instance guard (one runner per bundle). A
   concurrent fire that cannot acquire the lock exits immediately (exit 0,
   silent) instead of stacking a new blocked process.

3. NO WALL-CLOCK BUDGET. Per-check timeouts (up to 600s) ran unbounded in the
   serial loop; total bundle time could exceed the job's 600s kill cap.
   -> FIX: each bundle has a wall-clock budget (5m:120s, 15m:240s,
   hourly:360s, daily:3300s). A check's timeout is clamped to the remaining
   budget so no single check can eat the whole run, and the loop stops
   launching new checks once the budget is exhausted (already-saved state
   means unfinished checks simply run on the next tick).

Combined these make the bundle converge: worst case a wedged check consumes
its own clamped timeout on one tick, its window is recorded, and the next tick
moves on. The pile-up cannot recur.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

try:
    import fcntl
except ImportError:  # non-POSIX fallback (shouldn't happen on DGX)
    fcntl = None

# ---- location resolution (mirrors cron scheduler) -------------------------
# The scheduler runs this bundle with HERMES_HOME = the jarvis profile home,
# and cwd = the scripts dir. Use the same resolution for absorbed scripts.
HERMES_HOME = Path(os.environ.get("HERMES_HOME", "/home/frank/.hermes/profiles/jarvis"))
SCRIPTS_DIR = HERMES_HOME / "scripts"
CRON_DIR = HERMES_HOME / "cron"
STATE_FILE = CRON_DIR / "state" / "guard_bundle_last_run.json"

# ---- per-bundle wall-clock budgets (seconds) ------------------------------
# The live guard-bundle path has previously observed a 600s kill boundary
# (t_8cdc9260). Keep every bundle well below that boundary; report-to-board
# retains wrapper slack. Unfinished checks run next tick via the state file.
BUDGETS: dict[str, int] = {
    "5m": 120,
    "15m": 240,
    "hourly": 360,
    "daily": 450,
}

# default per-check timeout (s). Long audits get an explicit override.
DEFAULT_TIMEOUT = 600


# ---- check manifest -------------------------------------------------------
# name -> (script, interval_seconds). interval preserves the original cadence.
# Intervals derived from each job's schedule in the jarvis cron store (verified
# 2026-08-29): every Nm -> N*60; */N -> 3600/N... see below.
def _min(v):
    return v * 60


def _manifest_boards() -> str:
    """Comma list of boards fleet loops may touch (state=active).

    NEVER use `--boards all` here: it globs every directory under
    kanban/boards/, which includes test scratch dirs (dedupecheck, testproj,
    e2e-*), dormant boards (legacy-yss, ai-restaurant) and — worst —
    orchestrator-sync, which the manifest marks state=denied: "must NEVER be
    dispatched or GC'd by fleet loops". Introduced and caught 2026-08-30.
    """
    try:
        import json as _json
        p = "/home/frank/.hermes/kanban/boards-manifest.json"
        with open(p) as fh:
            d = _json.load(fh)["boards"]
        act = [k for k, v in d.items()
               if v.get("state") == "active" and v.get("dispatch")]
        if act:
            return ",".join(sorted(act))
    except Exception:
        pass
    return "jarvis-os,sycode-trading,upero"


CHECKS: dict[str, dict] = {
    # ---- tick-5m bundle: fast-moving guards/liveness (original <=10m) ----
    "kanban-dedupe-guard":               {"script": "kanban_dedupe_guard.py",              "interval": _min(5), "args": ["--boards", _manifest_boards()]},
    "dgx-fleet-chain-alert-drain":       {"script": "fleet_chain_alert_drain.sh",          "interval": _min(5)},
    "hl-desk-watchman-guard":            {"script": "hl-desk-watchman-guard.sh",           "interval": _min(5)},
    "dgx-unified-health-probe":          {"script": "rtb-dgx-unified-health-probe.py",     "interval": _min(10)},
    "hl-candle-recorder-guard":          {"script": "hl-candle-recorder-guard.sh",         "interval": _min(10)},
    "position-age-watchdog":             {"script": "position_age_watchdog.py",            "interval": _min(10)},
    "elon-skill-write-guard":            {"script": "elon-skill-write-guard.sh",           "interval": _min(5)},  # was 2m; floored to bundle 5m
    # ---- tick-15m bundle: 15m-30m checks ----------------------------------
    "cron-store-disabled-state-watchdog":{"script": "cron_store_disabled_state_watchdog.py","interval": _min(15)},
    "orchestrator-heartbeat-watchdog":   {"script": "rtb-orchestrator-heartbeat-watchdog.py","interval": _min(15)},
    "nous-balance-watchdog":             {"script": "rtb-nous-balance-watchdog.py",        "interval": _min(15)},
    "spine-liveness-watch":              {"script": "spine-audit-cron.sh",                 "interval": _min(15)},
    "ci-runner-liveness":                {"script": "ci-runner-liveness-report.sh",        "interval": _min(20)},
    "kanban-cap-invariant":              {"script": "kanban-cap-invariant.py",             "interval": _min(15)},
    "kanban-audit-chain-monitor":        {"script": "kanban-audit-chain-monitor.sh",       "interval": _min(15)},
    "cron-ticker-invariant-guard":       {"script": "cron_ticker_invariant_guard.py",      "interval": _min(30)},
    "fleet-knowledge-catalog-regen-watchdog":{"script": "fleet_knowledge_catalog_regen_watchdog.py","interval": _min(30)},
    "data-freshness-probe":              {"script": "dgx_data_freshness_probe.py",         "interval": _min(30)},
    "strategy-audit-drift-watch":        {"script": "strategy-audit-drift-watch.sh",       "interval": _min(30)},
    "deadpid-fleet-alert-guard":         {"script": "deadpid-fleet-alert-guard.py",        "interval": _min(30)},
    "dgx-service-gate-escalation":       {"script": "service_gate_escalation_watchdog.py", "interval": _min(30)},
    "cron-health-canary":                {"script": "cron_health_canary_wrapper.sh",       "interval": _min(30)},
    # ---- tick-hourly bundle: hourly + 6h/8h audits ------------------------
    "untracked-cron-script-guard":       {"script": "cron_untracked_script_guard.py",      "interval": 3600},
    "kanban-idempotency-board-guard":    {"script": "kanban_idempotency_board_guard.py",   "interval": 6 * 3600},
    "dgx-disk-space-watchdog":           {"script": "rtb-dgx-disk-space-watchdog.py",      "interval": 3600},
    "primary-provider-liveness":         {"script": "rtb-primary-provider-liveness.py",    "interval": 3600},
    "dgx-agent-context-audit":           {"script": "agent_context_audit.py",              "interval": 6 * 3600},
    "dgx-fleet-safety-config-audit":     {"script": "fleet_safety_config_audit.py",        "interval": 6 * 3600},
    # ---- tick-daily bundle: daily + weekly ---------------------------------
    "ephemeral-workspace-config-guard":  {"script": "ephemeral_workspace_config_guard.py", "interval": 86400},
    "skill-cli-drift-guard":             {"script": "skill-cli-drift-guard.py",            "interval": 7 * 86400},
    "sycode-canonical-leak-guard-v2-weekly":{"script": "sycode_leak_guard_v2_watchdog.py", "interval": 7 * 86400},
    "nous-proxy-daily-auth-probe":       {"script": "dgx_nous_proxy_watchdog.py",          "interval": 86400},
    "channel-liveness-oob":              {"script": "channel_liveness_oob_probe.py",       "interval": 86400},
    "backup-freshness-monitor":          {"script": "rtb-backup-freshness-monitor.py",     "interval": 86400},
    "profile-toolset-obligation-audit":  {"script": "profile_toolset_obligation_audit.py", "interval": 86400},
    "weekly-security-audit":             {"script": "security_audit_route_high_cves.py",   "interval": 7 * 86400},
    "dgx-fleet-chain-validator":         {"script": "fleet_chain_validator.sh",            "interval": 86400, "timeout": 3100},
    "memory-knowledge-health-watchdog":  {"script": "memory_knowledge_health.py",          "interval": 86400},
    "standing-no-black-holes-detector":  {"script": "no_black_holes_detector.py",          "interval": 7 * 86400},
    # ADDITIVE (not one of the 38 absorbed rows): kanban-db-size-guard was
    # introduced by recurrence-prevention card t_519df3c0 after the original
    # mapping was created. Source is this profile-local script; it runs on a
    # 7-day interval inside the daily bundle, is report-only (file-size stats,
    # no DB mutation), and uses the same local -> report-to-board.py ->
    # jarvis-os -> jarvis-os-pm consumer path. It remains separately identified
    # in the evidence note and is not counted in the 38-row pause/reduction.
    "kanban-db-size-guard":              {"script": "kanban_db_size_guard.py",             "interval": 7 * 86400},
}

BUNDLES: dict[str, list[str]] = {
    "5m": [
        "kanban-dedupe-guard", "dgx-fleet-chain-alert-drain", "hl-desk-watchman-guard",
        "dgx-unified-health-probe", "hl-candle-recorder-guard", "position-age-watchdog",
        "elon-skill-write-guard",
    ],
    "15m": [
        "cron-store-disabled-state-watchdog", "orchestrator-heartbeat-watchdog",
        "nous-balance-watchdog", "spine-liveness-watch", "ci-runner-liveness",
        "kanban-cap-invariant", "kanban-audit-chain-monitor",
        "cron-ticker-invariant-guard", "fleet-knowledge-catalog-regen-watchdog",
        "data-freshness-probe", "strategy-audit-drift-watch", "deadpid-fleet-alert-guard",
        "dgx-service-gate-escalation", "cron-health-canary",
    ],
    "hourly": [
        "untracked-cron-script-guard", "kanban-idempotency-board-guard",
        "dgx-disk-space-watchdog", "primary-provider-liveness",
        "dgx-agent-context-audit", "dgx-fleet-safety-config-audit",
    ],
    "daily": [
        "ephemeral-workspace-config-guard", "skill-cli-drift-guard",
        "sycode-canonical-leak-guard-v2-weekly", "nous-proxy-daily-auth-probe",
        "channel-liveness-oob", "backup-freshness-monitor",
        "profile-toolset-obligation-audit", "weekly-security-audit",
        "dgx-fleet-chain-validator", "memory-knowledge-health-watchdog",
        "standing-no-black-holes-detector", "kanban-db-size-guard",
    ],
}


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
        tmp.replace(STATE_FILE)
    except Exception as e:
        print(f"guard-bundle WARN: could not write state file: {e}")


def run_check(name: str, spec: dict, timeout: int) -> tuple[int, str]:
    """Run one absorbed check. Returns (exit_code, captured_output)."""
    script = spec["script"]
    path = (SCRIPTS_DIR / script).resolve()
    if not path.is_file():
        return 2, f"script missing: {path}"
    argv = ["bash", str(path)] if path.suffix in (".sh", ".bash") else [sys.executable, str(path)]
    # Per-check extra CLI args. Without this every absorbed check ran bare, so
    # kanban_dedupe_guard silently scanned only its default board
    # (sycode-trading) and never saw the jarvis-os duplicate storm (fixed
    # 2026-08-30).
    argv += list(spec.get("args", []))
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout,
            cwd=str(SCRIPTS_DIR), env=os.environ.copy(), start_new_session=True,
        )
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        if proc.returncode != 0:
            parts = [f"[{name}] exited {proc.returncode}"]
            if out:
                parts.append(out)
            if err:
                parts.append(f"stderr: {err}")
            return proc.returncode, "\n".join(parts)
        return 0, ""  # healthy -> suppress
    except subprocess.TimeoutExpired:
        return 124, f"[{name}] timed out after {timeout}s"
    except Exception as e:
        return 2, f"[{name}] execution error: {e}"


def acquire_single_instance(bundle: str) -> bool:
    """Non-blocking flock guard: one runner per bundle at a time.

    Returns True if this process holds the lock (it should run), False if
    another instance is already running (the caller should exit silently).
    The lock is released automatically when this process exits or is killed.
    """
    if fcntl is None:
        return True  # no fcntl -> degrade to no guard rather than break the job
    try:
        lock_dir = STATE_FILE.parent
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_path = lock_dir / f"guard_bundle_{bundle}.lock"
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Keep the fd open for the lifetime of the process (do not close it
            # here — closing would release the lock). Store on a module attr.
            _held_locks.append(fd)
            return True
        except (BlockingIOError, OSError):
            os.close(fd)
            return False
    except Exception:
        return True  # lock failure should not break the bundle; degrade safely


_held_locks: list[int] = []


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in BUNDLES:
        print("usage: cron_guard_bundle_runner.py <5m|15m|hourly|daily>")
        return 2
    bundle = sys.argv[1]
    budget = BUDGETS.get(bundle, DEFAULT_TIMEOUT)

    # Single-instance guard: if another runner for this bundle is alive, skip.
    if not acquire_single_instance(bundle):
        # Concurrent fire — another instance is already running this bundle.
        # Exit silently (exit 0): the running instance owns this tick. We must
        # NOT report failure here (the other instance will).
        return 0

    now = int(time.time())
    started = time.monotonic()
    state = load_state()
    failures: list[str] = []
    due_ran = 0
    budget_exhausted = False

    for name in BUNDLES[bundle]:
        # Wall-clock budget: stop launching new checks once exhausted. Completed
        # checks already had their state saved incrementally, so unfinished ones
        # simply run on the next tick. No loss, no pile-up.
        elapsed = time.monotonic() - started
        if elapsed >= budget:
            budget_exhausted = True
            break

        spec = CHECKS[name]
        interval = int(spec["interval"])
        last = int(state.get(name, 0) or 0)
        if now - last < interval:
            continue  # not due yet
        due_ran += 1
        # Clamp this check's timeout to the remaining budget so a single hung
        # check can never eat the whole run (min 30s so legitimate long audits
        # still get a fair window on a fresh tick).
        remaining = budget - elapsed
        check_timeout = max(30, min(int(spec.get("timeout", DEFAULT_TIMEOUT)), int(remaining)))
        rc, out = run_check(name, spec, check_timeout)
        # Save state INCREMENTALLY after each check (not just at the end) so a
        # killed run preserves completed checks and never re-runs everything.
        state[name] = now
        save_state(state)
        if rc != 0:
            failures.append(out)

    if budget_exhausted:
        # Only when there was actually more due work than the budget allowed.
        # Not a failure — a deferral. The state file keeps unfinished checks due
        # for the next tick. Keep this out of `failures` so we stay silent.
        pass

    if failures:
        print(f"GUARD BUNDLE [{bundle}] — {len(failures)} failed check(s) at "
              f"{time.strftime('%Y-%m-%dT%H:%M:%S')}:\n")
        print("\n\n".join(failures))
        return 1

    # All due checks passed: emit NOTHING (empty stdout = silent watchdog).
    return 0


if __name__ == "__main__":
    sys.exit(main())
