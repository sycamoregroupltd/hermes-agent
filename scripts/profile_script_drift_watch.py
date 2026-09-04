#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
"""Alert when enabled profile-local cron scripts drift from canonical central scripts,
and detect duplicate mutation scripts across profiles.

Canonical-copy rule: scheduler.py resolves cron --script paths under the running
profile's $HERMES_HOME/scripts directory. Enabled profile-local jobs that are
intended to use /home/frank/.hermes/scripts must therefore be either exact byte
copies or small exec shims pointing at the central canonical script. Symlinks are
not sufficient because scheduler.py resolves and path-guards the target.

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
DEFAULT_DELIVER = "discord:#critical-alerts"
SHIM_MARKER = "CANONICAL-COPY RULE"
KANBAN_DUPE_HOOK = "/home/frank/.hermes/agent-hooks/gate-kanban-dupe-create.sh"
KANBAN_DUPE_MATCHER = "kanban_create"
MAX_ALERT_ROWS = 50

# Pairs (profile_name, script_name) intentionally exempted from drift detection.
# Each excluded pair must document why in a kanban task referenced in the installer
# shim header (see t_f8c1e76e for the pattern).
DRIFT_EXCLUSIONS: set[tuple[str, str]] = {
    # Jarvis PIT monitor is a CANONICAL-COPY RULE exec shim pointing at
    # sycode-trading-pm/scripts/pit-monitor.sh per intentional architecture
    # (task t_f8c1e76e). The central copy is a different full script body
    # — this is a correct fork, not a drift failure.
    ("jarvis", "pit-monitor.sh"),
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
DORMANT_SHADOW_RISK_DELIVER = "discord:#fleet-reports"


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


def approved_shim(actual: Path, central: Path) -> bool:
    try:
        text = actual.read_text(errors="replace")
    except Exception:
        return False
    if actual.suffix.lower() in {".sh", ".bash"}:
        return is_shell_shim(text, central)
    return is_python_shim(text, central)


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


def bundle_script_names(runner: Path) -> tuple[set[str], str | None]:
    """Extract subprocess script names from a guard-bundle manifest.

    Parse the runner rather than importing it: importing a live cron runner can
    execute setup or spawn work. Only string values under a ``script`` key in
    CHECKS/PIPELINES dispatch tables are considered.
    """
    try:
        tree = ast.parse(runner.read_text(errors="replace"), filename=str(runner))
    except (OSError, SyntaxError) as exc:
        return set(), f"{type(exc).__name__}: {exc}"

    names: set[str] = set()
    manifest_names = {"CHECKS", "PIPELINES"}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(isinstance(target, ast.Name) and target.id in manifest_names for target in targets):
            continue
        value = node.value
        if value is None:
            continue
        for child in ast.walk(value):
            if not isinstance(child, ast.Dict):
                continue
            for key, item in zip(child.keys, child.values):
                if isinstance(key, ast.Constant) and key.value == "script":
                    if isinstance(item, ast.Constant) and isinstance(item.value, str):
                        names.add(item.value)
    return names, None


def inspect_guard_bundle_scripts(root: Path) -> list[dict]:
    """Check profile-local scripts dispatched indirectly by guard bundles."""
    alerts: list[dict] = []
    profiles = root / "profiles"
    if not profiles.exists():
        return alerts
    seen_runners: set[str] = set()
    for runner in sorted(profiles.glob("*/scripts/cron_guard_bundle_runner.py")):
        if os.path.realpath(runner) in seen_runners:
            continue
        seen_runners.add(os.path.realpath(runner))
        profile_home = runner.parents[1]
        profile = profile_home.name
        scripts, parse_error = bundle_script_names(runner)
        if parse_error:
            alerts.append({
                "type": "GUARD_BUNDLE_MANIFEST_PARSE_ERROR",
                "profile": profile,
                "runner": str(runner),
                "error": parse_error,
                "task": TASK_ID,
            })
            continue
        for script in sorted(scripts):
            actual = profile_home / "scripts" / script
            central = root / "scripts" / script
            if not central.exists():
                continue
            if not actual.exists():
                alerts.append({
                    "type": "GUARD_BUNDLE_SCRIPT_MISSING",
                    "profile": profile,
                    "runner": str(runner),
                    "script": script,
                    "actual": str(actual),
                    "central": str(central),
                    "task": TASK_ID,
                })
                continue
            try:
                exact = sha256(actual) == sha256(central)
            except Exception as exc:
                alerts.append({
                    "type": "GUARD_BUNDLE_SCRIPT_HASH_ERROR",
                    "profile": profile,
                    "runner": str(runner),
                    "script": script,
                    "actual": str(actual),
                    "central": str(central),
                    "error": f"{type(exc).__name__}: {exc}",
                    "task": TASK_ID,
                })
                continue
            if exact or approved_shim(actual, central):
                continue
            alerts.append({
                "type": "GUARD_BUNDLE_SCRIPT_FORK_DRIFT",
                "profile": profile,
                "runner": str(runner),
                "script": script,
                "actual": str(actual),
                "central": str(central),
                "actual_sha256": sha256(actual),
                "central_sha256": sha256(central),
                "task": TASK_ID,
            })
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
    alerts.extend(inspect_guard_bundle_scripts(root))
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
            actual = script_path(profile_home, str(script))
            central = central_counterpart(root, profile_home, actual)
            if (profile, str(script)) in DRIFT_EXCLUSIONS:
                continue
            if not central.exists():
                # Profile-local by reconciliation policy: no central source of truth.
                continue
            if not actual.exists():
                alerts.append({
                    "type": "PROFILE_SCRIPT_MISSING",
                    "profile": profile,
                    "job_id": job.get("id"),
                    "job_name": job.get("name"),
                    "script": str(script),
                    "actual": str(actual),
                    "central": str(central),
                })
                continue
            try:
                exact = sha256(actual) == sha256(central)
            except Exception as exc:
                alerts.append({
                    "type": "SCRIPT_HASH_ERROR",
                    "profile": profile,
                    "job_id": job.get("id"),
                    "job_name": job.get("name"),
                    "actual": str(actual),
                    "central": str(central),
                    "error": f"{type(exc).__name__}: {exc}",
                })
                continue
            if exact or approved_shim(actual, central):
                continue
            alerts.append({
                "type": "SCRIPT_FORK_DRIFT",
                "profile": profile,
                "job_id": job.get("id"),
                "job_name": job.get("name"),
                "script": str(script),
                "actual": str(actual),
                "central": str(central),
                "actual_sha256": sha256(actual),
                "central_sha256": sha256(central),
                "task": TASK_ID,
                "source_map_task": SOURCE_MAP_TASK_ID,
            })
    # Add duplicate-mutation-script checks
    dupe_alerts, dormant_alerts = inspect_duplicate_mutation_scripts(root)
    alerts.extend(dupe_alerts)
    # Add retention-policy duplicate/value-drift checks (t_c198fcb5 seat decision)
    alerts.extend(inspect_retention_policy_duplicates(root))
    return alerts, dormant_alerts


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

        # Expect: 1 SCRIPT_FORK_DRIFT (fixture-drift) +
        #         1 DUPLICATE_MUTATION_SCRIPT (macro-regime-change-monitor.py 2x)
        #         1 DORMANT_SHADOW_RISK (fixture-mutation-paused)
        if len(alerts) != 2:
            sys.stderr.write(f"fixture expected 2 drift+dupe alerts, got {len(alerts)} in {root}\n")
            return 1
        if len(dormant) != 1:
            sys.stderr.write(f"fixture expected 1 dormant shadow risk, got {len(dormant)} in {root}\n")
            return 1

        drift = [a for a in alerts if a.get("type") == "SCRIPT_FORK_DRIFT"]
        dupe = [a for a in alerts if a.get("type") == "DUPLICATE_MUTATION_SCRIPT"]
        if len(drift) != 1 or len(dupe) != 1:
            sys.stderr.write(
                f"fixture type mismatch: drift={len(drift)} dupe={len(dupe)} in {root}\n"
            )
            return 1
        drift_alert = drift[0]
        dupe_alert = dupe[0]
        if drift_alert.get("job_id") != "fixture-drift" or "local_only" in (output or ""):
            sys.stderr.write(f"fixture drift mismatch in {root}: {json.dumps(drift_alert, sort_keys=True)}\n")
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
