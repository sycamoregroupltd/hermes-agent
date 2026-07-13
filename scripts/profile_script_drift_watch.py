#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
"""Alert when enabled profile-local cron scripts drift from canonical central scripts.

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
    for config_path in sorted(profiles.glob("*/config.yaml")):
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


def inspect(root: Path) -> list[dict]:
    """Return alert rows for enabled jobs whose profile scripts are unsafe forks.

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
        return [{"type": "ROOT_MISSING", "root": str(root)}]
    alerts.extend(inspect_kanban_dupe_hook_coverage(root))
    for jobs_path in sorted(profiles.glob("*/cron/jobs.json")):
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
    return alerts


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

    jobs = {
        "jobs": [
            {"id": "fixture-exact", "name": "fixture-exact", "enabled": True, "script": "ok_exact.py"},
            {"id": "fixture-shim", "name": "fixture-shim", "enabled": True, "script": "ok_shim.py"},
            {"id": "fixture-drift", "name": "fixture-drift", "enabled": True, "script": "drift.py"},
            {"id": "fixture-local", "name": "fixture-local", "enabled": True, "script": "local_only.py"},
        ]
    }
    write(cron / "jobs.json", json.dumps(jobs, indent=2) + "\n")
    return root


def run_fixture() -> int:
    root = make_fixture_root()
    try:
        alerts = inspect(root)
        output = format_alerts(alerts)
        sys.stdout.write(output)
        if len(alerts) != 1:
            sys.stderr.write(f"fixture expected 1 alert, got {len(alerts)} in {root}\n")
            return 1
        alert = alerts[0]
        if alert.get("job_id") != "fixture-drift" or "local_only" in output:
            sys.stderr.write(f"fixture alert mismatch in {root}: {json.dumps(alert, sort_keys=True)}\n")
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
        return run_fixture()
    root = Path(args.root).expanduser()
    alerts = inspect(root)
    if args.json:
        sys.stdout.write(json.dumps(alerts, indent=2, sort_keys=True) + "\n")
    else:
        output = format_alerts(alerts, args.deliver)
        if output:
            sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
