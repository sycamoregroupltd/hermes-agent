#!/usr/bin/env python3
"""Emit deploy provenance gauges for node_exporter's textfile collector.

Outcome-oriented (not run-oriented):
  - sycode_deploy_last_successful_timestamp ages by itself when deploys stop matching
    origin/main or the collector dies.
  - sycode_deploy_sha_drift is 1 while running image SHA != origin/main.

Never writes under a git worktree checkout operation; only textfile + state paths.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

TEXTFILE_DIR = Path(
    os.environ.get(
        "TEXTFILE_DIR",
        "/home/frank/sycode-trading/monitoring/node-exporter-textfile",
    )
)
OUTPUT = TEXTFILE_DIR / "sycode_deploy_watch.prom"
STATE_PATH = Path(
    os.environ.get(
        "DEPLOY_WATCH_STATE_PATH",
        "/home/frank/.hermes/state/sycode-deploy-watch-state.json",
    )
)
VERSION_URL = os.environ.get("DEPLOY_WATCH_VERSION_URL", "http://127.0.0.1:3001/version")
# Prefer pristine build-tree — never require the locked primary checkout.
GIT_DIR = Path(
    os.environ.get(
        "DEPLOY_WATCH_GIT_DIR",
        "/home/frank/.hermes/deploy-state/build-tree",
    )
)
FETCH = os.environ.get("DEPLOY_WATCH_FETCH", "0") == "1"
SHA_RE = re.compile(r"^[a-f0-9]{7,40}$", re.I)


def sanitize_label(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.:-]+", "_", value.strip())
    return value[:64] or "unknown"


def metric_line(name: str, labels: dict[str, str] | None, value: float | int) -> str:
    if labels:
        inner = ",".join(f'{k}="{sanitize_label(v)}"' for k, v in sorted(labels.items()))
        return f"{name}{{{inner}}} {value}"
    return f"{name} {value}"


def read_running_sha(url: str = VERSION_URL) -> str:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    sha = str(payload.get("builtFromSha") or "").strip()
    if not SHA_RE.match(sha) or sha.lower() == "unknown":
        raise ValueError(f"running builtFromSha unusable: {sha!r}")
    return sha.lower()


def read_origin_main_sha(git_dir: Path = GIT_DIR, fetch: bool = FETCH) -> str:
    if not git_dir.is_dir():
        raise FileNotFoundError(f"git dir missing: {git_dir}")
    if fetch:
        subprocess.run(
            ["git", "-C", str(git_dir), "fetch", "--quiet", "origin", "main"],
            check=True,
            timeout=60,
            capture_output=True,
        )
    out = subprocess.check_output(
        ["git", "-C", str(git_dir), "rev-parse", "origin/main"],
        text=True,
        timeout=15,
    ).strip()
    if not SHA_RE.match(out):
        raise ValueError(f"origin/main sha unusable: {out!r}")
    return out.lower()


def load_state(path: Path = STATE_PATH) -> dict:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict, path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".deploy-watch-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def build_metrics(
    *,
    running_sha: str,
    desired_sha: str,
    now: float | None = None,
    state: dict | None = None,
) -> tuple[list[str], dict]:
    now = time.time() if now is None else now
    state = dict(state or {})
    match = running_sha == desired_sha
    if match:
        state["last_successful_sha"] = running_sha
        state["last_successful_timestamp"] = int(now)

    last_sha = str(state.get("last_successful_sha") or "")
    last_ts = int(state.get("last_successful_timestamp") or 0)

    lines = [
        "# HELP sycode_deploy_watch_collector_success 1 when deploy-watch collector succeeded.",
        "# TYPE sycode_deploy_watch_collector_success gauge",
        metric_line("sycode_deploy_watch_collector_success", None, 1),
        "# HELP sycode_deploy_watch_collector_last_run_timestamp Unix time of last successful collector run.",
        "# TYPE sycode_deploy_watch_collector_last_run_timestamp gauge",
        metric_line("sycode_deploy_watch_collector_last_run_timestamp", None, int(now)),
        "# HELP sycode_deploy_running_info Running server image git sha (value always 1).",
        "# TYPE sycode_deploy_running_info gauge",
        metric_line("sycode_deploy_running_info", {"sha": running_sha}, 1),
        "# HELP sycode_deploy_desired_info Desired origin/main git sha (value always 1).",
        "# TYPE sycode_deploy_desired_info gauge",
        metric_line("sycode_deploy_desired_info", {"sha": desired_sha, "source": "origin_main"}, 1),
        "# HELP sycode_deploy_sha_drift 1 when running image sha differs from origin/main.",
        "# TYPE sycode_deploy_sha_drift gauge",
        metric_line("sycode_deploy_sha_drift", None, 0 if match else 1),
        "# HELP sycode_deploy_sha_match 1 when running image sha equals origin/main.",
        "# TYPE sycode_deploy_sha_match gauge",
        metric_line("sycode_deploy_sha_match", None, 1 if match else 0),
    ]

    if last_sha and last_ts > 0:
        lines.extend(
            [
                "# HELP sycode_deploy_last_successful_info Last observed matching deploy sha (value always 1).",
                "# TYPE sycode_deploy_last_successful_info gauge",
                metric_line("sycode_deploy_last_successful_info", {"sha": last_sha}, 1),
                "# HELP sycode_deploy_last_successful_timestamp Unix time when running last matched origin/main.",
                "# TYPE sycode_deploy_last_successful_timestamp gauge",
                metric_line("sycode_deploy_last_successful_timestamp", None, last_ts),
            ]
        )
    return lines, state


def write_textfile(lines: list[str], output: Path = OUTPUT) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(output.parent), prefix=".sycode_deploy_watch-", suffix=".prom")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        os.replace(tmp, output)
        # node-exporter runs as nobody; mkstemp default mode is 0600.
        os.chmod(output, 0o644)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def write_failure(error_class: str, output: Path = OUTPUT) -> None:
    now = int(time.time())
    lines = [
        "# HELP sycode_deploy_watch_collector_success 1 when deploy-watch collector succeeded.",
        "# TYPE sycode_deploy_watch_collector_success gauge",
        metric_line("sycode_deploy_watch_collector_success", {"error_class": error_class}, 0),
        "# HELP sycode_deploy_watch_collector_last_run_timestamp Unix time of last collector attempt.",
        "# TYPE sycode_deploy_watch_collector_last_run_timestamp gauge",
        metric_line("sycode_deploy_watch_collector_last_run_timestamp", None, now),
    ]
    write_textfile(lines, output)


def main() -> int:
    try:
        running = read_running_sha()
        desired = read_origin_main_sha()
        state = load_state()
        lines, new_state = build_metrics(running_sha=running, desired_sha=desired, state=state)
        write_textfile(lines)
        save_state(new_state)
        return 0
    except (urllib.error.URLError, TimeoutError, ValueError, FileNotFoundError, subprocess.SubprocessError, OSError) as exc:
        error_class = type(exc).__name__
        try:
            write_failure(error_class)
        except OSError:
            pass
        print(f"sycode deploy-watch collector failed: {error_class}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
