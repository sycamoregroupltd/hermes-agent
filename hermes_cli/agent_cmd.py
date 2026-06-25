"""Implementation for the ``hermes agent`` CLI family."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from hermes_cli.agent_runs import AgentRunStore
from hermes_cli.profiles import normalize_profile_name, profile_exists, validate_profile_name

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _parse_env(assignments: list[str] | None) -> tuple[dict[str, str], str | None]:
    parsed: dict[str, str] = {}
    for item in assignments or []:
        if "=" not in item:
            return {}, "--env must be NAME=VALUE"
        name, value = item.split("=", 1)
        if not _ENV_NAME_RE.match(name):
            return {}, "--env must be NAME=VALUE with a valid environment variable name"
        parsed[name] = value
    return parsed, None


def _resolve_profile(name: str | None) -> tuple[str, str | None]:
    profile = normalize_profile_name(name or "default")
    try:
        validate_profile_name(profile)
    except ValueError as exc:
        return profile, str(exc)
    if not profile_exists(profile):
        return profile, f"Unknown profile: {profile}"
    return profile, None


def _spawn_prompt(args: Any) -> str:
    prompt = (getattr(args, "prompt", None) or getattr(args, "goal", None) or "").strip()
    if not prompt:
        raise ValueError("agent spawn requires --prompt or --goal")
    return prompt


def _build_command(*, profile: str, prompt: str, toolsets: str | None) -> list[str]:
    cmd = [sys.executable, "-m", "hermes_cli.main", "--profile", profile, "--oneshot", prompt]
    if toolsets:
        cmd.extend(["--toolsets", toolsets])
    return cmd


def _prompt_with_context(prompt: str, context_files: list[str]) -> tuple[str, str | None]:
    if not context_files:
        return prompt, None

    parts = [prompt, "", "Additional context files:"]
    for raw_path in context_files:
        path = Path(raw_path)
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            return prompt, f"Failed to read context file {raw_path}: {exc}"
        parts.extend(
            [
                "",
                f"--- Context file: {path} ---",
                content,
                f"--- End context file: {path} ---",
            ]
        )
    return "\n".join(parts), None


def _print_spawn_result(*, run_id: str, status: str, pid: int | None = None, stdout: str | None = None, as_json: bool = False) -> None:
    if as_json:
        payload: dict[str, object] = {"run_id": run_id, "status": status}
        if pid is not None:
            payload["pid"] = pid
        if stdout is not None:
            payload["stdout"] = stdout
        print(json.dumps(payload, sort_keys=True))
        return
    print(f"run_id: {run_id}")
    print(f"status: {status}")
    if pid is not None:
        print(f"pid: {pid}")
    if stdout:
        print(stdout, end="" if stdout.endswith("\n") else "\n")


def _agent_spawn(args: Any) -> int:
    profile, profile_error = _resolve_profile(getattr(args, "profile", None))
    env_overrides, env_error = _parse_env(getattr(args, "env", None))
    if env_error:
        print(env_error, file=sys.stderr)
        return 2
    if profile_error:
        print(profile_error, file=sys.stderr)
        return 2

    try:
        prompt = _spawn_prompt(args)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    cwd = getattr(args, "cwd", None) or None
    if cwd:
        cwd_path = Path(cwd).expanduser()
        if not cwd_path.is_dir():
            print(f"--cwd is not a directory: {cwd}", file=sys.stderr)
            return 2
        cwd = str(cwd_path)

    context_files = [str(Path(path).expanduser()) for path in (getattr(args, "context_file", None) or [])]
    child_prompt, context_error = _prompt_with_context(prompt, context_files)
    if context_error:
        print(context_error, file=sys.stderr)
        return 2

    command = _build_command(profile=profile, prompt=child_prompt, toolsets=getattr(args, "toolsets", None))
    run_id = AgentRunStore.new_run_id()
    env = os.environ.copy()
    env.update(env_overrides)
    env["HERMES_AGENT_RUN_ID"] = run_id
    env["HERMES_AGENT_PARENT_PID"] = str(os.getpid())
    if context_files:
        env["HERMES_AGENT_CONTEXT_FILES"] = os.pathsep.join(context_files)

    store = AgentRunStore()
    block = bool(getattr(args, "block", False))
    if block:
        store.create(
            run_id=run_id,
            profile=profile,
            prompt=prompt,
            mode="blocking",
            status="running",
            command=command,
            context_files=context_files,
            env=env_overrides,
            cwd=cwd,
        )
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            text=True,
            capture_output=True,
            timeout=getattr(args, "timeout", None),
            check=False,
        )
        status = "completed" if completed.returncode == 0 else "failed"
        store.mark_finished(
            run_id,
            status=status,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
        _print_spawn_result(
            run_id=run_id,
            status=status,
            stdout=completed.stdout,
            as_json=bool(getattr(args, "json", False)),
        )
        return completed.returncode

    stdout_target = subprocess.DEVNULL
    stderr_target = subprocess.DEVNULL
    proc = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=stdout_target,
        stderr=stderr_target,
        text=True,
        start_new_session=(os.name != "nt"),
    )
    store.create(
        run_id=run_id,
        profile=profile,
        prompt=prompt,
        mode="background",
        status="running",
        pid=proc.pid,
        command=command,
        context_files=context_files,
        env=env_overrides,
        cwd=cwd,
    )
    _print_spawn_result(
        run_id=run_id,
        status="running",
        pid=proc.pid,
        as_json=bool(getattr(args, "json", False)),
    )
    return 0


def agent_command(args: Any) -> int:
    action = getattr(args, "agent_action", None)
    if action == "spawn":
        return _agent_spawn(args)
    print("usage: hermes agent spawn [--profile PROFILE] (--prompt TEXT | --goal TEXT)", file=sys.stderr)
    return 2
