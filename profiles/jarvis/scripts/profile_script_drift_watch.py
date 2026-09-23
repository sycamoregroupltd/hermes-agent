#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
"""Alert when enabled profile-local cron scripts drift from canonical central scripts,
and detect duplicate mutation scripts across profiles.

Canonical-copy rule: scheduler.py resolves cron --script paths under the running
profile's $HERMES_HOME/scripts directory. Enabled profile-local jobs that are
intended to use /home/frank/.hermes/scripts must therefore be either exact byte
copies or small exec shims pointing at the central canonical script. Symlinks are
not sufficient because scheduler.py resolves and path-guards the target.

Bundle-runner indirection (t_25086e48): the guard bundle (t_db689c47) condensed
whole groups of cron jobs into four tick wrappers, so the real checks are no
longer any job's `script` field. The executed path is

    guard-bundle-tick-* -> guard_bundle_run.sh -> report-to-board.py
      -> <profile_home>/scripts/cron_guard_bundle_runner.py
      -> <profile_home>/scripts/<check>

A jobs.json-only scan is blind to every one of those check files: the drift that
mattered (a 411-line stale cron_untracked_script_guard.py executing in the jarvis
profile against a 460-line canonical, t_f0afde60) produced ZERO rows and was
found by hand. This watcher therefore also parses each profile-local bundle
runner's CHECKS manifest and drift-checks every manifest `script` against its
canonical <root>/scripts/ counterpart, resolved from the ACTUAL consumer path
(<profile_home>/scripts/, exactly as the scheduler and the runner resolve it).

This is a no-agent watchdog: clean state emits zero stdout and exits 0; any
stdout is the alert payload delivered by the owning cron job (currently Jarvis
`profile-script-drift-watch`, deliver=discord:#critical-alerts).
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from typing import Iterable

TASK_ID = "t_f1aded94"
SOURCE_MAP_TASK_ID = "t_8734e698"
KANBAN_DUPE_ROLLOUT_TASK_ID = "t_86bb3798"
DEFAULT_ROOT = Path("/home/frank/.hermes")
DEFAULT_DELIVER = "discord:critical-alerts"
SHIM_MARKER = "CANONICAL-COPY RULE"
KANBAN_DUPE_HOOK = "/home/frank/.hermes/agent-hooks/gate-kanban-dupe-create.sh"
KANBAN_DUPE_MATCHER = "kanban_create"
MAX_ALERT_ROWS = 50

# Bundle-runner indirection (t_25086e48). The guard bundle's profile-local runner
# is discovered by name under each profile's scripts/ dir — the same directory the
# scheduler and the runner itself resolve check paths against.
BUNDLE_RUNNER_NAME = "cron_guard_bundle_runner.py"
BUNDLE_COVERAGE_TASK_ID = "t_25086e48"
# A marker-less profile-local copy is still an approved canonical shim when it is
# provably nothing but an exec of the canonical file (see *_exec_adapter below).
# The SHIM_MARKER is a naming convention, not the contract; requiring it reported
# six legitimate `exec /home/frank/.hermes/scripts/<x>` adapters as fork drift
# (hl-desk-watchman-guard.sh, hl-candle-recorder-guard.sh,
# hermes-mixed-module-probe.sh, systemd_dead_path_observe.py,
# discord_target_hash_lint.py, overlay_budget_core_patch_refuse.py) — false
# positives are how a guard gets muted.
PY_ADAPTER_MAX_LINES = 60
PY_ADAPTER_DENY = (
    "subprocess",
    "write_text",
    "write_bytes",
    "os.remove",
    "os.unlink",
    "os.system",
    "eval(",
    "exec(",
)

# Pairs (profile_name, script_name) intentionally exempted from drift detection.
# Each excluded pair must document why in a kanban task referenced in the installer
# shim header (see t_f8c1e76e for the pattern).
DRIFT_EXCLUSIONS: set[tuple[str, str]] = {
    # Jarvis PIT monitor is a CANONICAL-COPY RULE exec shim pointing at
    # sycode-trading-pm/scripts/pit-monitor.sh per intentional architecture
    # (task t_f8c1e76e). The central copy is a different full script body
    # — this is a correct fork, not a drift failure.
    ("jarvis", "pit-monitor.sh"),
    # rtb-primary-provider-liveness.py: the CENTRAL copy is deliberately a
    # 23-line os.execv POINTER SHIM into the jarvis executed copy (task
    # t_6348e35c, 2026-09-23): a stale byte-identical twin was replaced by a
    # pointer so the approved fix cannot rot in an unexecuted copy. The pair is
    # intentionally divergent — installing canonical→profile would make the
    # profile shim exec itself — so the profile copy is the source of truth for
    # the LOGIC and the central path intentionally holds none.
    ("jarvis", "rtb-primary-provider-liveness.py"),
}

# Set of script names known to mutate state (writes to kanban, database,
# trading positions, service restarts, file mutations). When two enabled cron
# jobs with distinct job_ids reference the same mutation script, it risks
# duplicate writes, race conditions, or double-execution. Read-only scripts
# (monitors, probes, validators) are excluded — duplicates of those are
# wasteful but not dangerous. Add scripts here only when confirmed to write
# state that a duplicate would corrupt.
MUTATION_SCRIPTS: set[str] = {
    "verdict_router.py",                 # kanban task create/complete/block
    "nfp_safety_mode.sh",                # enables/disables trading strategies
    "msb-weekly-rebalance.sh",           # executes portfolio rebalance
    "macro-regime-change-monitor.py",    # creates kanban alert tasks
    "sycode_clean_epoch_ledger.py",      # deletes/archives DB records
    "arena-insert-liveness-cron-runner.sh",  # inserts liveness probe data to DB
    "sync-pattern-win-rate-registry.sh", # writes to win-rate registry
    "calibration_cron.sh",               # runs calibration that writes state
    "sycode_edge_emergence_scan.py",     # may create kanban investigation tasks
}

# DORMANT_SHADOW_RISK_DELIVER — delivery target for the dormant-shadow-risk
# report (paused jobs that reference mutation scripts). Distinct from the
# alert-format deliver so an operator can route it to a quieter channel.
DORMANT_SHADOW_RISK_DELIVER = "discord:fleet-reports"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_jobs(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text())
    except Exception as exc:  # fail visible via alert, not crash
        return [{"_load_error": f"{type(exc).__name__}: {exc}"}]
    if isinstance(data, dict):
        jobs = data.get("jobs", [])
    elif isinstance(data, list):
        jobs = data
    else:
        jobs = []
    return [j for j in jobs if isinstance(j, dict)]


def enabled(job: dict) -> bool:
    return bool(job.get("enabled", True)) and not bool(job.get("paused", False))


def script_path(profile_home: Path, script: str) -> Path:
    raw = Path(script).expanduser()
    if raw.is_absolute():
        return raw
    return profile_home / "scripts" / raw


def central_counterpart(root: Path, profile_home: Path, actual: Path) -> Path:
    scripts_dir = profile_home / "scripts"
    try:
        rel = actual.relative_to(scripts_dir)
    except ValueError:
        rel = Path(actual.name)
    return root / "scripts" / rel


def string_literals(text: str) -> Iterable[str]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append(node.value)
    return out


def is_python_shim(text: str, central: Path) -> bool:
    if SHIM_MARKER not in text or "os.execv" not in text:
        return False
    return str(central) in set(string_literals(text))


def is_shell_shim(text: str, central: Path) -> bool:
    if SHIM_MARKER not in text or "exec " not in text:
        return False
    # Accept quoted or unquoted central path in a simple bash exec wrapper.
    return str(central) in text


def shell_exec_adapter(text: str, central: Path) -> bool:
    """Marker-less shell copy that is provably nothing but an exec of the canonical.

    Every executable line must be `set ...`, `export ...`, or the single
    `exec <central> "$@"` line, so nothing else can run and the executed code IS
    the canonical script -> no drift is possible. Conservative by construction:
    one stray command (a curl, a write, a second exec) and this returns False.
    """
    if str(central) not in text:
        return False
    active = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not active:
        return False
    execs = [line for line in active if line.startswith(("exec ", "exec\t"))]
    if len(execs) != 1 or str(central) not in execs[0]:
        return False
    return all(
        line in execs or line.startswith(("set ", "set\t", "export "))
        for line in active
    )


def python_exec_adapter(text: str, central: Path) -> bool:
    """Marker-less python copy that is provably nothing but an exec of the canonical.

    Requires: the canonical path as a string literal, an `os.execv*` call, a
    shim-sized body, no state-mutating or subprocess primitives, and a top level
    made only of imports/assignments/defs/docs/guards. A full diverged script
    body (the t_f0afde60 failure mode: 411 vs 460 lines) can never qualify.
    """
    if str(central) not in text or "os.execv" not in text:
        return False
    if len(text.splitlines()) > PY_ADAPTER_MAX_LINES:
        return False
    if any(bad in text for bad in PY_ADAPTER_DENY):
        return False
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False
    allowed = (
        ast.Import,
        ast.ImportFrom,
        ast.Assign,
        ast.AnnAssign,
        ast.FunctionDef,
        ast.ClassDef,
        ast.If,
        ast.Expr,
        ast.Pass,
    )
    if any(not isinstance(node, allowed) for node in tree.body):
        return False
    return str(central) in set(string_literals(text))


def approved_shim(actual: Path, central: Path) -> bool:
    try:
        text = actual.read_text(errors="replace")
    except Exception:
        return False
    if actual.suffix.lower() in {".sh", ".bash"}:
        return is_shell_shim(text, central) or shell_exec_adapter(text, central)
    return is_python_shim(text, central) or python_exec_adapter(text, central)


def inspect_kanban_dupe_hook_coverage(root: Path) -> list[dict]:
    """Return alert rows for profiles missing the kanban-create dedupe hook.

    This is the profile-config counterpart to the script-copy drift watch: shell
    hooks resolve under each worker profile's HERMES_HOME, so every profile config
    must carry the same pre_tool_call matcher for kanban_create. The hook command
    itself is intentionally central under ~/.hermes/agent-hooks/ so profiles do
    not carry forkable script copies.
    """
    alerts: list[dict] = []
    profiles = root / "profiles"
    hook_path = Path(KANBAN_DUPE_HOOK)
    try:
        hook_path = root / hook_path.relative_to(DEFAULT_ROOT)
    except ValueError:
        pass
    if not hook_path.exists():
        alerts.append({
            "type": "KANBAN_DUPE_HOOK_SCRIPT_MISSING",
            "hook": KANBAN_DUPE_HOOK,
            "task": KANBAN_DUPE_ROLLOUT_TASK_ID,
        })
    elif not os.access(hook_path, os.X_OK):
        alerts.append({
            "type": "KANBAN_DUPE_HOOK_SCRIPT_NOT_EXECUTABLE",
            "hook": KANBAN_DUPE_HOOK,
            "task": KANBAN_DUPE_ROLLOUT_TASK_ID,
        })
    _seen_cfg = set()
    for _cfg in sorted(profiles.glob("*/config.yaml")):
        if os.path.realpath(_cfg) in _seen_cfg:
            continue  # symlink alias (e.g. sycode-trading -> sycode-trading-pm) — dedupe
        _seen_cfg.add(os.path.realpath(_cfg))
        config_path = _cfg
        profile = config_path.parent.name
        try:
            lines = config_path.read_text(errors="replace").splitlines()
        except Exception as exc:
            alerts.append({
                "type": "PROFILE_CONFIG_READ_ERROR",
                "profile": profile,
                "config": str(config_path),
                "error": f"{type(exc).__name__}: {exc}",
                "task": KANBAN_DUPE_ROLLOUT_TASK_ID,
            })
            continue
        hook_indexes = [idx for idx, line in enumerate(lines) if KANBAN_DUPE_HOOK in line]
        if not hook_indexes:
            alerts.append({
                "type": "KANBAN_DUPE_HOOK_MISSING",
                "profile": profile,
                "config": str(config_path),
                "hook": KANBAN_DUPE_HOOK,
                "matcher": KANBAN_DUPE_MATCHER,
                "task": KANBAN_DUPE_ROLLOUT_TASK_ID,
            })
            continue
        if not any(
            any(f"matcher: {KANBAN_DUPE_MATCHER}" in nearby for nearby in lines[idx:idx + 5])
            for idx in hook_indexes
        ):
            alerts.append({
                "type": "KANBAN_DUPE_HOOK_MATCHER_DRIFT",
                "profile": profile,
                "config": str(config_path),
                "hook": KANBAN_DUPE_HOOK,
                "expected_matcher": KANBAN_DUPE_MATCHER,
                "task": KANBAN_DUPE_ROLLOUT_TASK_ID,
            })
        if "hooks_auto_accept: true" not in lines:
            alerts.append({
                "type": "HOOKS_AUTO_ACCEPT_MISSING_OR_FALSE",
                "profile": profile,
                "config": str(config_path),
                "task": KANBAN_DUPE_ROLLOUT_TASK_ID,
            })
    return alerts


def inspect_duplicate_mutation_scripts(root: Path) -> tuple[list[dict], list[dict]]:
    """Return (duplicate_alerts, dormant_shadow_risk_alerts).

    duplicate_alerts: enabled jobs referencing a MUTATION_SCRIPTS entry with
    2+ distinct job_ids (cross-profile mirrors with the same job_id are
    intentional and not flagged as duplicates).

    dormant_shadow_risk_alerts: paused jobs that reference a mutation script.
    These represent dormant state-mutation capacity that could be accidentally
    resumed and cause double-execution with the already-active job.
    """
    duplicate_alerts: list[dict] = []
    dormant_alerts: list[dict] = []
    profiles = root / "profiles"
    if not profiles.exists():
        return duplicate_alerts, dormant_alerts

    # Collect all (script_name, job_id, profile, job_name, enabled, paused)
    script_entries: dict[str, list[dict]] = {}
    _seen_jobs = set()
    for _jp in sorted(profiles.glob("*/cron/jobs.json")):
        if os.path.realpath(_jp) in _seen_jobs:
            continue  # symlink alias — dedupe
        _seen_jobs.add(os.path.realpath(_jp))
        jobs_path = _jp
        profile = jobs_path.parents[1].name
        for job in load_jobs(jobs_path):
            if "_load_error" in job:
                continue
            script = job.get("script")
            if not script or script not in MUTATION_SCRIPTS:
                continue
            entry = {
                "profile": profile,
                "job_id": job.get("id", "?"),
                "job_name": job.get("name", "?"),
                "enabled": bool(job.get("enabled", True)),
                "paused": job.get("state") == "paused" or bool(job.get("paused_at")),
            }
            script_entries.setdefault(script, []).append(entry)

    # Active duplicate check: mutation scripts with 2+ enabled jobs
    # that have distinct job_ids (same job_id = cross-profile mirror, safe).
    for script, entries in sorted(script_entries.items()):
        active = [e for e in entries if e["enabled"] and not e["paused"]]
        distinct_ids = set(e["job_id"] for e in active)
        if len(distinct_ids) >= 2:
            duplicate_alerts.append({
                "type": "DUPLICATE_MUTATION_SCRIPT",
                "script": script,
                "count": len(active),
                "distinct_job_ids": len(distinct_ids),
                "jobs": [
                    {"profile": e["profile"], "job_id": e["job_id"], "job_name": e["job_name"]}
                    for e in sorted(active, key=lambda x: (x["profile"], x["job_name"]))
                ],
                "task": TASK_ID,
            })

    # Dormant shadow risk: paused jobs pointing to any mutation script
    for script, entries in sorted(script_entries.items()):
        paused = [e for e in entries if e["paused"]]
        if not paused:
            continue
        active = [e for e in entries if e["enabled"] and not e["paused"]]
        dormant_alerts.append({
            "type": "DORMANT_SHADOW_RISK",
            "script": script,
            "paused_count": len(paused),
            "active_count": len(active),
            "paused_jobs": [
                {"profile": e["profile"], "job_id": e["job_id"], "job_name": e["job_name"]}
                for e in sorted(paused, key=lambda x: (x["profile"], x["job_name"]))
            ],
            "active_jobs": [
                {"profile": e["profile"], "job_id": e["job_id"], "job_name": e["job_name"]}
                for e in sorted(active, key=lambda x: (x["profile"], x["job_name"]))
            ],
            "task": TASK_ID,
        })

    return duplicate_alerts, dormant_alerts


def inspect_retention_policy_duplicates(root: Path) -> list[dict]:
    """Alert when the prune-default-state-db retention policy has a second copy.

    SEAT DECISION 2026-08-03 (task t_c198fcb5): one file, one policy. The
    reviewed runtime policy lives at profiles/jarvis/scripts/prune-default-state-db.py
    with RETENTION_DAYS=45. Any second copy of this script in an executable
    script location is a live regression trap (a future copy-from-global or
    "script missing, let me copy it" repair would silently regress session
    retention 45 -> 90 days), so this watch FAILS (alerts) when:

      - more than one copy of prune-default-state-db.py exists under
        root/scripts or any profiles/*/scripts (DUPLICATE_RETENTION_POLICY), or
      - exactly one copy exists but its RETENTION_DAYS != 45
        (RETENTION_POLICY_VALUE_DRIFT).

    Scoped to the policy identity (filename + reviewed constant), NOT every
    file defining a RETENTION_DAYS constant: bak_litter_janitor.py legitimately
    defines RETENTION_DAYS=7 for a different policy domain and must not trip
    this check. Non-executable historical copies (.claude/worktrees, backups,
    __pycache__) are excluded from the scan.
    """
    alerts: list[dict] = []
    target = "prune-default-state-db.py"
    reviewed_value = 45
    candidates: list[Path] = []
    skip_dir_parts = {".git", ".claude", "worktrees", "__pycache__", "backups",
                      "state-snapshots", "node_modules", ".venv", "venv"}

    search_roots = [root / "scripts"]
    profiles = root / "profiles"
    if profiles.exists():
        seen = {os.path.realpath(root / "scripts")}
        for scripts_dir in sorted(profiles.glob("*/scripts")):
            rp = os.path.realpath(scripts_dir)
            if rp in seen:
                continue  # symlink alias — dedupe (sycode-trading -> sycode-trading-pm)
            seen.add(rp)
            search_roots.append(scripts_dir)

    for scripts_dir in search_roots:
        if not scripts_dir.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(scripts_dir):
            dirnames[:] = [d for d in dirnames if d not in skip_dir_parts]
            for fn in filenames:
                if fn == target:
                    candidates.append(Path(dirpath) / fn)

    if len(candidates) > 1:
        alerts.append({
            "type": "DUPLICATE_RETENTION_POLICY",
            "script": target,
            "count": len(candidates),
            "paths": sorted(str(p) for p in candidates),
            "expected_retention_days": reviewed_value,
            "task": TASK_ID,
            "seat_decision": "t_c198fcb5",
        })
        return alerts

    if len(candidates) == 1:
        sole = candidates[0]
        try:
            text = sole.read_text(errors="replace")
        except Exception as exc:
            alerts.append({
                "type": "RETENTION_POLICY_READ_ERROR",
                "script": target,
                "path": str(sole),
                "error": f"{type(exc).__name__}: {exc}",
                "task": TASK_ID,
            })
            return alerts
        m = re.search(r"^\s*RETENTION_DAYS\s*=\s*(\d+)", text, re.MULTILINE)
        if m is None or int(m.group(1)) != reviewed_value:
            alerts.append({
                "type": "RETENTION_POLICY_VALUE_DRIFT",
                "script": target,
                "path": str(sole),
                "found_retention_days": None if m is None else int(m.group(1)),
                "expected_retention_days": reviewed_value,
                "task": TASK_ID,
                "seat_decision": "t_c198fcb5",
            })

    return alerts


def classify_script_pair(
    profile: str, script: str, actual: Path, central: Path, ctx: dict
) -> dict | None:
    """One drift verdict for one (profile-local executed copy, canonical) pair.

    Shared by the job-`script` scan and the bundle-runner-manifest scan so both
    paths apply byte-identical semantics (PROFILE_SCRIPT_MISSING / hash error /
    exact / approved shim / SCRIPT_FORK_DRIFT) to copies that really execute.
    """
    if not central.exists():
        # Profile-local by reconciliation policy: no central source of truth.
        return None
    base = {"profile": profile, "script": str(script), **ctx}
    if not actual.exists():
        return {
            "type": "PROFILE_SCRIPT_MISSING",
            **base,
            "actual": str(actual),
            "central": str(central),
        }
    try:
        exact = sha256(actual) == sha256(central)
    except Exception as exc:
        return {
            "type": "SCRIPT_HASH_ERROR",
            **base,
            "actual": str(actual),
            "central": str(central),
            "error": f"{type(exc).__name__}: {exc}",
        }
    if exact or approved_shim(actual, central):
        return None
    return {
        "type": "SCRIPT_FORK_DRIFT",
        **base,
        "actual": str(actual),
        "central": str(central),
        "actual_sha256": sha256(actual),
        "central_sha256": sha256(central),
        "task": TASK_ID,
        "source_map_task": SOURCE_MAP_TASK_ID,
    }


def bundle_manifest_scripts(runner: Path) -> list[tuple[str, str]]:
    """Return [(check_name, script)] from a guard-bundle runner's CHECKS manifest.

    Parsed with `ast`, not a regex: manifest values legitimately contain calls
    and dict lookups (`_min(5)`, `_manifest_boards()`), and only the literal
    `script` key carries the executed file name.
    """
    try:
        tree = ast.parse(runner.read_text(errors="replace"))
    except Exception:
        return []
    out: list[tuple[str, str]] = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if not any(isinstance(t, ast.Name) and t.id == "CHECKS" for t in targets):
            continue
        if not isinstance(node.value, ast.Dict):
            continue
        for key, value in zip(node.value.keys, node.value.values):
            if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                continue
            if not isinstance(value, ast.Dict):
                continue
            for vkey, vvalue in zip(value.keys, value.values):
                if (
                    isinstance(vkey, ast.Constant)
                    and vkey.value == "script"
                    and isinstance(vvalue, ast.Constant)
                    and isinstance(vvalue.value, str)
                ):
                    out.append((key.value, vvalue.value))
    return out


def inspect_bundle_runner_checks(root: Path, covered: set[tuple[str, str]]) -> list[dict]:
    """Drift-check bundle-runner-indirected executed copies (t_25086e48).

    The guard bundle (t_db689c47) condensed whole job groups into four tick
    wrappers, so the real checks stopped being any job's `script` field:

        guard-bundle-tick-* -> guard_bundle_run.sh -> report-to-board.py
          -> <profile_home>/scripts/cron_guard_bundle_runner.py
          -> <profile_home>/scripts/<check>

    Every such check file is an executed copy with a canonical counterpart, and
    none of them was drift-checked: `cron_untracked_script_guard.py` executed a
    411-line stale jarvis copy against a 460-line canonical for 6 weeks and the
    watcher never emitted a row (t_f0afde60 — found by hand).

    Rows already produced from a job `script` field are skipped via `covered`, so
    a script reachable both ways is reported exactly once.
    """
    alerts: list[dict] = []
    seen_runners: set[str] = set()
    seen_pairs: set[tuple[str, str]] = set(covered)
    for runner in sorted((root / "profiles").glob(f"*/scripts/{BUNDLE_RUNNER_NAME}")):
        real = os.path.realpath(runner)
        if real in seen_runners:
            continue  # symlink alias — dedupe (t_a39fa15e)
        seen_runners.add(real)
        profile_home = runner.parents[1]
        profile = profile_home.name
        for check, script in bundle_manifest_scripts(runner):
            if (profile, script) in seen_pairs:
                continue
            seen_pairs.add((profile, script))
            if (profile, script) in DRIFT_EXCLUSIONS:
                continue
            actual = script_path(profile_home, script)
            central = central_counterpart(root, profile_home, actual)
            alert = classify_script_pair(
                profile,
                script,
                actual,
                central,
                {
                    "check": check,
                    "via": "guard-bundle",
                    "bundle_runner": str(runner),
                    "bundle_task": BUNDLE_COVERAGE_TASK_ID,
                },
            )
            if alert is not None:
                alerts.append(alert)
    return alerts


def inspect(root: Path) -> tuple[list[dict], list[dict]]:
    """Return (drift_and_dupe_alerts, dormant_shadow_risk_alerts).

    drift_and_dupe_alerts: script-fork-drift, kanban-dupe-hook-coverage, and
    duplicate-mutation-script alerts — actionable items needing operator attention.

    dormant_shadow_risk_alerts: paused jobs referencing mutation scripts that
    represent dormant capacity which would cause double-execution if resumed
    alongside an already-active instance of the same script.

    Intentional profile-local scripts are skipped when no central counterpart
    exists; the reconciliation map only covers rows where a central counterpart
    is the source of truth. This avoids false positives for profile-owned jobs.
    Also verifies fleet-wide coverage for the creation-time kanban duplicate
    guard hook so new or drifted profiles cannot silently bypass it.
    """
    alerts: list[dict] = []
    profiles = root / "profiles"
    central_scripts = root / "scripts"
    if not profiles.exists() or not central_scripts.exists():
        return [{"type": "ROOT_MISSING", "root": str(root)}], []
    alerts.extend(inspect_kanban_dupe_hook_coverage(root))
    # (profile, script) pairs already drift-checked from a job `script` field, so
    # the bundle-manifest pass reports each executed copy exactly once.
    covered: set[tuple[str, str]] = set()
    _seen_jobs2 = set()
    for _jp2 in sorted(profiles.glob("*/cron/jobs.json")):
        if os.path.realpath(_jp2) in _seen_jobs2:
            continue  # symlink alias — dedupe
        _seen_jobs2.add(os.path.realpath(_jp2))
        jobs_path = _jp2
        profile_home = jobs_path.parents[1]
        profile = profile_home.name
        for job in load_jobs(jobs_path):
            if "_load_error" in job:
                alerts.append({
                    "type": "CRON_JSON_ERROR",
                    "profile": profile,
                    "jobs_path": str(jobs_path),
                    "error": job["_load_error"],
                })
                continue
            script = job.get("script")
            if not script or not enabled(job):
                continue
            script = str(script)
            actual = script_path(profile_home, script)
            central = central_counterpart(root, profile_home, actual)
            covered.add((profile, script))
            if (profile, script) in DRIFT_EXCLUSIONS:
                continue
            alert = classify_script_pair(
                profile,
                script,
                actual,
                central,
                {"job_id": job.get("id"), "job_name": job.get("name")},
            )
            if alert is not None:
                alerts.append(alert)
    # Bundle-runner-indirected checks (t_25086e48): executed copies whose runner is
    # not any job's `script` field. Deduped against everything the jobs.json scan
    # above already covered.
    alerts.extend(inspect_bundle_runner_checks(root, covered))
    # Add duplicate-mutation-script checks
    dupe_alerts, dormant_alerts = inspect_duplicate_mutation_scripts(root)
    alerts.extend(dupe_alerts)
    # Add retention-policy duplicate/value-drift checks (t_c198fcb5 seat decision)
    alerts.extend(inspect_retention_policy_duplicates(root))
    return alerts, dormant_alerts



def auto_canonical_copy(alerts: list[dict]) -> list[dict]:
    """Copy central to profile-local for PROFILE_SCRIPT_MISSING rows.

    Symlinks are insufficient (path-guard). Returns list of copy result dicts.
    """
    results: list[dict] = []
    for item in alerts:
        if item.get("type") != "PROFILE_SCRIPT_MISSING":
            continue
        central = Path(str(item.get("central", "")))
        actual = Path(str(item.get("actual", "")))
        if not central.is_file():
            results.append({**item, "copy": "skip_no_central"})
            continue
        try:
            actual.parent.mkdir(parents=True, exist_ok=True)
            data = central.read_bytes()
            actual.write_bytes(data)
            mode = central.stat().st_mode & 0o777
            if central.suffix.lower() in {".sh", ".bash", ".py"}:
                mode |= 0o755
            actual.chmod(mode)
            results.append({
                "type": "PROFILE_SCRIPT_COPIED",
                "profile": item.get("profile"),
                "job_id": item.get("job_id"),
                "job_name": item.get("job_name"),
                "script": item.get("script"),
                "from": str(central),
                "to": str(actual),
                "bytes": len(data),
            })
        except Exception as exc:
            results.append({
                "type": "PROFILE_SCRIPT_COPY_FAILED",
                "profile": item.get("profile"),
                "job_id": item.get("job_id"),
                "script": item.get("script"),
                "error": f"{type(exc).__name__}: {exc}",
            })
    return results


def format_alerts(alerts: list[dict], deliver: str = DEFAULT_DELIVER) -> str:
    if not alerts:
        return ""
    lines = [
        "SCRIPT_FORK_DRIFT_ALERT",
        f"task={TASK_ID}",
        f"source_map_task={SOURCE_MAP_TASK_ID}",
        f"deliver={deliver}",
        f"count={len(alerts)}",
    ]
    for item in alerts[:MAX_ALERT_ROWS]:
        lines.append(json.dumps(item, sort_keys=True))
    if len(alerts) > MAX_ALERT_ROWS:
        lines.append(f"... truncated {len(alerts) - MAX_ALERT_ROWS} additional alerts")
    return "\n".join(lines) + "\n"


def format_dormant_shadow_risk(alerts: list[dict]) -> str:
    """Format the dormant shadow risk report as a separate output section.

    Returns empty string if no dormant risks to report.
    """
    if not alerts:
        return ""
    lines = [
        "===== DORMANT SHADOW RISK REPORT =====",
        f"task={TASK_ID}",
        f"deliver={DORMANT_SHADOW_RISK_DELIVER}",
        "Paused jobs referencing mutation scripts — resuming would risk",
        "double-execution alongside the active instance(s) below.",
        f"count={len(alerts)}",
    ]
    for item in alerts[:MAX_ALERT_ROWS]:
        lines.append(json.dumps(item, sort_keys=True))
    if len(alerts) > MAX_ALERT_ROWS:
        lines.append(f"... truncated {len(alerts) - MAX_ALERT_ROWS} additional alerts")
    lines.append("===== END DORMANT SHADOW RISK REPORT =====")
    return "\n".join(lines) + "\n"


def write(path: Path, text: str, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if mode is not None:
        path.chmod(mode)


def make_fixture_root() -> Path:
    root = Path(tempfile.mkdtemp(prefix="profile-script-drift-watch-"))
    central = root / "scripts"
    hooks = root / "agent-hooks"
    profile = root / "profiles" / "fixture" / "scripts"
    cron = root / "profiles" / "fixture" / "cron"
    config = root / "profiles" / "fixture" / "config.yaml"
    central.mkdir(parents=True)
    hooks.mkdir(parents=True)
    profile.mkdir(parents=True)
    cron.mkdir(parents=True)

    write(hooks / "gate-kanban-dupe-create.sh", "#!/usr/bin/env bash\necho '{}'\n", stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    write(
        config,
        "hooks:\n"
        "  pre_tool_call:\n"
        f"  - command: {KANBAN_DUPE_HOOK}\n"
        f"    matcher: {KANBAN_DUPE_MATCHER}\n"
        "    timeout: 20\n"
        "hooks_auto_accept: true\n",
    )

    write(central / "ok_exact.py", "print('ok exact')\n")
    write(profile / "ok_exact.py", "print('ok exact')\n")

    write(central / "ok_shim.py", "print('central shim target')\n")
    write(
        profile / "ok_shim.py",
        "#!/usr/bin/env python3\n"
        f'"""{SHIM_MARKER}: fixture shim."""\n'
        "import os, sys\n"
        f"TARGET = {str(central / 'ok_shim.py')!r}\n"
        "os.execv(sys.executable, [sys.executable, TARGET, *sys.argv[1:]])\n",
        stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR,
    )

    write(central / "drift.py", "print('central')\n")
    write(profile / "drift.py", "print('diverged profile fork')\n")

    write(profile / "local_only.py", "print('intentional profile-local')\n")

    # --- bundle-runner-indirected fixture (t_25086e48) -------------------------
    # The runner's manifest names the check files the runner executes; the drift
    # watcher must resolve them from THIS scripts dir (the actual consumer path).
    write(central / "bundle_exact.py", "print('bundle exact')\n")
    write(profile / "bundle_exact.py", "print('bundle exact')\n")

    write(central / "bundle_adapter.sh", "# canonical body\nexit 0\n")
    write(
        profile / "bundle_adapter.sh",
        "#!/usr/bin/env bash\n"
        "# marker-less exec adapter -> canonical (sanctioned shim form)\n"
        f'exec {central / "bundle_adapter.sh"} "$@"\n',
        stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR,
    )

    write(central / "bundle_adapter.py", "# canonical body\nprint('central')\n")
    write(
        profile / "bundle_adapter.py",
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        f"TARGET = {str(central / 'bundle_adapter.py')!r}\n"
        "os.execv(sys.executable, [sys.executable, TARGET, *sys.argv[1:]])\n",
        stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR,
    )

    write(central / "bundle_drift.py", "print('central bundle check')\n")
    write(profile / "bundle_drift.py", "print('stale 411-line style fork')\n")

    write(profile / "bundle_nocentral.py", "print('profile-local by policy')\n")

    write(central / "bundle_absent.py", "print('never installed in the profile')\n")

    write(
        profile / BUNDLE_RUNNER_NAME,
        "#!/usr/bin/env python3\n"
        '"""Fixture guard-bundle runner (mirrors the real manifest shape)."""\n'
        "def _min(v):\n"
        "    return v\n\n"
        "SOME_VAR = 'dynamic-value-not-a-literal'\n"
        "CHECKS: dict[str, dict] = {\n"
        '    "bundle-exact": {"script": "bundle_exact.py", "cadence": _min(5)},\n'
        '    "bundle-adapter": {"script": "bundle_adapter.sh", "cadence": _min(5)},\n'
        '    "bundle-py-adapter": {"script": "bundle_adapter.py", "cadence": _min(5)},\n'
        '    "bundle-drift": {"script": "bundle_drift.py", "cadence": _min(5)},\n'
        '    "bundle-nocentral": {"script": "bundle_nocentral.py", "cadence": _min(5)},\n'
        '    "bundle-missing": {"script": "bundle_absent.py", "cadence": _min(5)},\n'
        '    "bundle-drift-already-covered": {"script": "drift.py", "cadence": _min(5)},\n'
        '    "bundle-dynamic-skipped": {"script": SOME_VAR, "cadence": _min(5)},\n'
        "}\n",
    )
    write(
        central / BUNDLE_RUNNER_NAME,
        "#!/usr/bin/env python3\nCHECKS: dict[str, dict] = {}\n",
    )
    # Symlink-alias profile: its jobs.json AND bundle runner resolve to the same
    # realpath as profiles/fixture -> both scans must dedupe (t_a39fa15e).
    alias = root / "profiles" / "fixture-alias"
    try:
        alias.symlink_to(root / "profiles" / "fixture", target_is_directory=True)
    except OSError:
        pass

    # Mutation script — placed in both central and profile so it doesn't
    # trigger PROFILE_SCRIPT_MISSING path in the drift watch
    write(central / "macro-regime-change-monitor.py", "print('mutation script fixture')\n")
    write(profile / "macro-regime-change-monitor.py", "print('mutation script fixture')\n")

    jobs = {
        "jobs": [
            {"id": "fixture-exact", "name": "fixture-exact", "enabled": True, "script": "ok_exact.py"},
            {"id": "fixture-shim", "name": "fixture-shim", "enabled": True, "script": "ok_shim.py"},
            {"id": "fixture-drift", "name": "fixture-drift", "enabled": True, "script": "drift.py"},
            {"id": "fixture-local", "name": "fixture-local", "enabled": True, "script": "local_only.py"},
            # Same mutation script, different job_id → should trigger DUPLICATE_MUTATION_SCRIPT
            {"id": "fixture-mutation-a", "name": "fixture-mutation-a", "enabled": True, "script": "macro-regime-change-monitor.py"},
            {"id": "fixture-mutation-b", "name": "fixture-mutation-b", "enabled": True, "script": "macro-regime-change-monitor.py"},
            # Paused mutation script → should trigger DORMANT_SHADOW_RISK
            {"id": "fixture-mutation-paused", "name": "fixture-mutation-paused", "enabled": False, "script": "macro-regime-change-monitor.py", "state": "paused", "paused_at": "2026-07-28T10:00:00Z"},
        ]
    }
    write(cron / "jobs.json", json.dumps(jobs, indent=2) + "\n")
    return root


def run_retention_fixture() -> int:
    """Prove the retention-policy duplicate guard (t_c198fcb5) fires correctly.

    Scenarios:
      1. Single reviewed copy (RETENTION_DAYS=45) at profiles/*/scripts -> clean.
      2. Deliberate second copy under root/scripts -> DUPLICATE_RETENTION_POLICY.
      3. Single copy with drifted value (90) -> RETENTION_POLICY_VALUE_DRIFT.
      4. bak_litter_janitor.py (different policy, RETENTION_DAYS=7) -> NOT counted.
      5. Historical .claude/worktrees copy -> NOT counted (non-executable).
    """
    root = Path(tempfile.mkdtemp(prefix="retention-policy-fixture-"))
    try:
        central = root / "scripts"
        profile = root / "profiles" / "fixture" / "scripts"
        worktree = root / "scripts" / ".claude" / "worktrees" / "wt-historical" / "scripts"
        central.mkdir(parents=True)
        profile.mkdir(parents=True)
        worktree.mkdir(parents=True)

        reviewed = "# policy\nRETENTION_DAYS = 45\n"
        drifted = "# policy\nRETENTION_DAYS = 90\n"
        other_policy = "# different policy domain\nRETENTION_DAYS = 7\n"

        # 1. single reviewed copy -> clean
        write(profile / "prune-default-state-db.py", reviewed)
        a = inspect_retention_policy_duplicates(root)
        if a:
            sys.stderr.write(f"retention fixture 1 expected clean, got {json.dumps(a, sort_keys=True)}\n")
            return 1

        # 2. second copy under root/scripts -> DUPLICATE_RETENTION_POLICY
        write(central / "prune-default-state-db.py", reviewed)
        a = inspect_retention_policy_duplicates(root)
        if len(a) != 1 or a[0].get("type") != "DUPLICATE_RETENTION_POLICY" or a[0].get("count") != 2:
            sys.stderr.write(f"retention fixture 2 expected DUPLICATE_RETENTION_POLICY count=2, got {json.dumps(a, sort_keys=True)}\n")
            return 1

        # remove duplicate again -> clean
        (central / "prune-default-state-db.py").unlink()
        a = inspect_retention_policy_duplicates(root)
        if a:
            sys.stderr.write(f"retention fixture 2b expected clean after removal, got {json.dumps(a, sort_keys=True)}\n")
            return 1

        # 3. drifted value -> RETENTION_POLICY_VALUE_DRIFT
        write(profile / "prune-default-state-db.py", drifted)
        a = inspect_retention_policy_duplicates(root)
        if len(a) != 1 or a[0].get("type") != "RETENTION_POLICY_VALUE_DRIFT" or a[0].get("found_retention_days") != 90:
            sys.stderr.write(f"retention fixture 3 expected VALUE_DRIFT(90), got {json.dumps(a, sort_keys=True)}\n")
            return 1

        # restore reviewed value -> clean
        write(profile / "prune-default-state-db.py", reviewed)

        # 4. bak_litter_janitor (different policy) must NOT trip the guard
        write(central / "bak_litter_janitor.py", other_policy)
        a = inspect_retention_policy_duplicates(root)
        if a:
            sys.stderr.write(f"retention fixture 4 expected bak_litter_janitor ignored, got {json.dumps(a, sort_keys=True)}\n")
            return 1

        # 5. historical worktree copy must NOT trip the guard
        write(worktree / "prune-default-state-db.py", drifted)
        a = inspect_retention_policy_duplicates(root)
        if a:
            sys.stderr.write(f"retention fixture 5 expected worktree copy ignored, got {json.dumps(a, sort_keys=True)}\n")
            return 1

        return 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


def run_fixture() -> int:
    root = make_fixture_root()
    try:
        alerts, dormant = inspect(root)
        output = format_alerts(alerts)
        if output:
            sys.stdout.write(output)
        dormant_output = format_dormant_shadow_risk(dormant)
        if dormant_output:
            sys.stdout.write(dormant_output)

        # Expect: 2 SCRIPT_FORK_DRIFT (fixture-drift job row + bundle_drift.py
        #           via the bundle-runner manifest) +
        #         1 PROFILE_SCRIPT_MISSING (bundle_absent.py, manifest-only) +
        #         1 DUPLICATE_MUTATION_SCRIPT (macro-regime-change-monitor.py 2x)
        #         1 DORMANT_SHADOW_RISK (fixture-mutation-paused)
        if len(alerts) != 4:
            sys.stderr.write(
                f"fixture expected 4 drift+dupe alerts, got {len(alerts)} in {root}: "
                f"{json.dumps(alerts, sort_keys=True)}\n"
            )
            return 1
        if len(dormant) != 1:
            sys.stderr.write(f"fixture expected 1 dormant shadow risk, got {len(dormant)} in {root}\n")
            return 1

        drift = [a for a in alerts if a.get("type") == "SCRIPT_FORK_DRIFT"]
        dupe = [a for a in alerts if a.get("type") == "DUPLICATE_MUTATION_SCRIPT"]
        missing = [a for a in alerts if a.get("type") == "PROFILE_SCRIPT_MISSING"]
        if len(drift) != 2 or len(dupe) != 1 or len(missing) != 1:
            sys.stderr.write(
                f"fixture type mismatch: drift={len(drift)} dupe={len(dupe)} "
                f"missing={len(missing)} in {root}\n"
            )
            return 1

        job_drift = [a for a in drift if a.get("job_id") == "fixture-drift"]
        bundle_drift = [a for a in drift if a.get("via") == "guard-bundle"]
        if len(job_drift) != 1 or len(bundle_drift) != 1:
            sys.stderr.write(
                f"fixture drift routing mismatch: job={len(job_drift)} bundle={len(bundle_drift)} "
                f"in {root}: {json.dumps(drift, sort_keys=True)}\n"
            )
            return 1
        drift_alert = job_drift[0]
        dupe_alert = dupe[0]
        if "local_only" in (output or ""):
            sys.stderr.write(f"fixture drift mismatch in {root}: {json.dumps(drift_alert, sort_keys=True)}\n")
            return 1

        # Regression for the t_f0afde60 detection gap: a diverged copy that is
        # reachable ONLY through the bundle runner must still be reported, from the
        # actual consumer path, exactly once, and identified as bundle-indirected.
        bd = bundle_drift[0]
        profile_home = root / "profiles" / "fixture"
        if (
            bd.get("script") != "bundle_drift.py"
            or bd.get("check") != "bundle-drift"
            or bd.get("profile") != "fixture"
            or bd.get("actual") != str(profile_home / "scripts" / "bundle_drift.py")
            or bd.get("central") != str(root / "scripts" / "bundle_drift.py")
            or bd.get("bundle_runner") != str(profile_home / "scripts" / BUNDLE_RUNNER_NAME)
            or bd.get("bundle_task") != BUNDLE_COVERAGE_TASK_ID
        ):
            sys.stderr.write(f"fixture bundle-drift row mismatch in {root}: {json.dumps(bd, sort_keys=True)}\n")
            return 1
        if output.count('"script": "bundle_drift.py"') != 1:
            sys.stderr.write(f"fixture bundle drift reported more than once in {root}\n")
            return 1
        # The manifest-only missing copy is reported as PROFILE_SCRIPT_MISSING,
        # not as drift, and keeps its bundle provenance.
        if (
            missing[0].get("script") != "bundle_absent.py"
            or missing[0].get("via") != "guard-bundle"
            or missing[0].get("check") != "bundle-missing"
        ):
            sys.stderr.write(f"fixture bundle-missing row mismatch in {root}: {json.dumps(missing[0], sort_keys=True)}\n")
            return 1
        # Manifest-only pairs with no central counterpart, byte-identical copies,
        # marker-less exec adapters and the dynamic (non-literal) manifest value
        # must all stay silent.
        for silent in ("bundle_nocentral", "bundle_exact", "bundle_adapter", "dynamic-value-not-a-literal"):
            if silent in (output or ""):
                sys.stderr.write(f"fixture false positive for {silent} in {root}: {output}\n")
                return 1
        # A script reachable BOTH ways is reported once, by the job scan (no `via`).
        if output.count('"script": "drift.py"') != 1 or job_drift[0].get("via"):
            sys.stderr.write(f"fixture dedupe failure for drift.py in {root}: {output}\n")
            return 1

        if dupe_alert.get("script") != "macro-regime-change-monitor.py":
            sys.stderr.write(f"fixture dupe script mismatch: {json.dumps(dupe_alert, sort_keys=True)}\n")
            return 1
        dormant_alert = dormant[0]
        if dormant_alert.get("type") != "DORMANT_SHADOW_RISK":
            sys.stderr.write(f"fixture dormant type mismatch: {json.dumps(dormant_alert, sort_keys=True)}\n")
            return 1
        return 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=os.environ.get("HERMES_SCRIPT_DRIFT_ROOT", str(DEFAULT_ROOT)))
    parser.add_argument("--deliver", default=os.environ.get("HERMES_SCRIPT_DRIFT_DELIVER", DEFAULT_DELIVER))
    parser.add_argument("--fixture", action="store_true", help="create a temp divergent pair and print the alert payload")
    parser.add_argument("--json", action="store_true", help="emit raw alert rows as JSON, including [] when clean")
    parser.add_argument("--auto-canonical-copy", action="store_true",
                        help="for PROFILE_SCRIPT_MISSING: copy central script into profile-local (real file, not symlink)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.fixture:
        rc = run_fixture()
        if rc != 0:
            return rc
        return run_retention_fixture()
    root = Path(args.root).expanduser()
    alerts, dormant = inspect(root)
    copy_results: list[dict] = []
    if getattr(args, "auto_canonical_copy", False):
        copy_results = auto_canonical_copy(alerts)
        copied_keys = {
            (r.get("profile"), r.get("script"))
            for r in copy_results
            if r.get("type") == "PROFILE_SCRIPT_COPIED"
        }
        alerts = [
            a for a in alerts
            if not (
                a.get("type") == "PROFILE_SCRIPT_MISSING"
                and (a.get("profile"), a.get("script")) in copied_keys
            )
        ]
        alerts = copy_results + alerts
    if args.json:
        # JSON mode: emit both in one payload with sections
        import json as jmod
        payload = {"drift_and_dupe_alerts": alerts, "dormant_shadow_risk": dormant}
        sys.stdout.write(jmod.dumps(payload, indent=2, sort_keys=True) + "\n")
    else:
        output = format_alerts(alerts, args.deliver)
        if output:
            sys.stdout.write(output)
        dormant_output = format_dormant_shadow_risk(dormant)
        if dormant_output:
            sys.stdout.write(dormant_output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
