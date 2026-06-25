from __future__ import annotations

import json
from types import SimpleNamespace
import sys
from pathlib import Path

import pytest


@pytest.fixture
def hermes_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return home


def test_agent_spawn_background_records_run_and_prints_run_id(
    hermes_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from hermes_cli.agent_cmd import agent_command
    from hermes_cli.agent_runs import AgentRunStore

    profile_dir = hermes_home / "profiles" / "research"
    profile_dir.mkdir(parents=True)
    context_path = tmp_path / "ctx.txt"
    context_path.write_text("file context here", encoding="utf-8")

    popen_calls: list[dict[str, object]] = []

    class FakePopen:
        pid = 4321

        def __init__(self, cmd, **kwargs):  # noqa: ANN001
            popen_calls.append({"cmd": cmd, "kwargs": kwargs})

    monkeypatch.setattr("hermes_cli.agent_cmd.subprocess.Popen", FakePopen)
    monkeypatch.setattr(sys, "argv", ["hermes", "agent", "spawn"])

    rc = agent_command(
        SimpleNamespace(
            agent_action="spawn",
            profile="research",
            prompt="write a market brief",
            goal=None,
            block=False,
            toolsets="web,file",
            context_file=[str(context_path)],
            env=["FOO=bar"],
            cwd=str(tmp_path),
            json=True,
            timeout=30,
        )
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["run_id"].startswith("ar_")
    assert payload["status"] == "running"
    assert payload["pid"] == 4321

    assert popen_calls
    cmd = popen_calls[0]["cmd"]
    assert isinstance(cmd, list)
    assert cmd[:5] == [sys.executable, "-m", "hermes_cli.main", "--profile", "research"]
    assert "--oneshot" in cmd
    prompt_arg = cmd[cmd.index("--oneshot") + 1]
    assert "write a market brief" in prompt_arg
    assert f"Context file: {context_path}" in prompt_arg
    assert "file context here" in prompt_arg
    assert "--toolsets" in cmd
    assert "web,file" in cmd
    kwargs = popen_calls[0]["kwargs"]
    assert isinstance(kwargs, dict)
    env = kwargs["env"]
    assert isinstance(env, dict)
    assert env["FOO"] == "bar"
    assert env["HERMES_AGENT_RUN_ID"] == payload["run_id"]
    assert env["HERMES_AGENT_PARENT_PID"] == str(__import__("os").getpid())

    record = AgentRunStore().get(payload["run_id"])
    assert record is not None
    assert record["profile"] == "research"
    assert record["prompt"] == "write a market brief"
    assert record["status"] == "running"
    assert record["pid"] == 4321
    assert record["mode"] == "background"
    assert record["context_files"] == [str(context_path)]
    assert record["env"] == {"FOO": "bar"}


def test_agent_spawn_blocking_records_completed_output(
    hermes_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from hermes_cli.agent_cmd import agent_command
    from hermes_cli.agent_runs import AgentRunStore

    class Completed:
        returncode = 0
        stdout = "done\n"
        stderr = ""

    monkeypatch.setattr(
        "hermes_cli.agent_cmd.subprocess.run",
        lambda cmd, **kwargs: Completed(),
    )

    rc = agent_command(
        SimpleNamespace(
            agent_action="spawn",
            profile="default",
            prompt=None,
            goal="summarize this repo",
            block=True,
            toolsets=None,
            context_file=None,
            env=None,
            cwd=None,
            json=False,
            timeout=10,
        )
    )

    assert rc == 0
    output = capsys.readouterr().out
    assert "run_id: ar_" in output
    assert "status: completed" in output
    assert "done" in output

    run_id = next(line.split(": ", 1)[1] for line in output.splitlines() if line.startswith("run_id:"))
    record = AgentRunStore().get(run_id)
    assert record is not None
    assert record["status"] == "completed"
    assert record["returncode"] == 0
    assert record["stdout"] == "done\n"


def test_agent_spawn_rejects_invalid_env_and_unknown_profile(
    hermes_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from hermes_cli.agent_cmd import agent_command

    args = SimpleNamespace(
        agent_action="spawn",
        profile="missing",
        prompt="hello",
        goal=None,
        block=False,
        toolsets=None,
        context_file=None,
        env=["NOT_AN_ASSIGNMENT"],
        cwd=None,
        json=False,
        timeout=None,
    )

    assert agent_command(args) == 2
    assert "--env must be NAME=VALUE" in capsys.readouterr().err

    args.env = None
    assert agent_command(args) == 2
    assert "Unknown profile" in capsys.readouterr().err


def test_agent_parser_is_registered_in_main(monkeypatch: pytest.MonkeyPatch) -> None:
    import hermes_cli.main as main_mod

    captured = {}

    def fake_agent_command(args):  # noqa: ANN001
        captured["action"] = args.agent_action
        captured["profile"] = args.profile
        captured["prompt"] = args.prompt
        captured["block"] = args.block

    monkeypatch.setattr(main_mod, "cmd_agent", fake_agent_command)
    monkeypatch.setattr(
        sys,
        "argv",
        ["hermes", "agent", "spawn", "--profile", "research", "--prompt", "hi", "--block"],
    )

    main_mod.main()

    assert captured == {
        "action": "spawn",
        "profile": "research",
        "prompt": "hi",
        "block": True,
    }
