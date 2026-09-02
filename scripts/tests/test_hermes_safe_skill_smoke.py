from __future__ import annotations

import importlib.util
import os
import stat
import subprocess
from pathlib import Path

import pytest


WRAPPER = Path(__file__).parents[1] / "hermes-safe-skill-smoke.sh"
WORKER_ENV = {
    "HERMES_KANBAN_TASK": "t_fixture_only",
    "HERMES_KANBAN_RUN_ID": "fixture-run",
    "HERMES_KANBAN_CLAIM_LOCK": "fixture-lock",
    "HERMES_KANBAN_DB": "/tmp/fixture-kanban.db",
    "HERMES_KANBAN_BOARD": "fixture-board",
    "HERMES_KANBAN_WORKSPACE": "/tmp/fixture-workspace",
    "HERMES_KANBAN_WORKSPACES_ROOT": "/tmp/fixture-workspaces",
    "HERMES_KANBAN_LOGS_ROOT": "/tmp/fixture-logs",
    "HERMES_KANBAN_HOME": "/tmp/fixture-kanban-home",
    "HERMES_KANBAN_DISPATCH_IN_GATEWAY": "1",
    "HERMES_SESSION_SOURCE": "kanban",
    "HERMES_TENANT": "fixture-tenant",
    "HERMES_DELEGATED_CHILD_CONTEXT": "1",
    "HERMES_SESSION_ID": "fixture-session",
    "HERMES_SUPERVISED_CHILD": "1",
    "HERMES_S6_SUPERVISED_CHILD": "1",
}


def _base_env() -> dict[str, str]:
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("HERMES_KANBAN_") or key in {
            "HERMES_SESSION_SOURCE",
            "HERMES_TENANT",
            "HERMES_DELEGATED_CHILD_CONTEXT",
            "HERMES_SESSION_ID",
            "HERMES_SUPERVISED_CHILD",
            "HERMES_S6_SUPERVISED_CHILD",
        }:
            env.pop(key, None)
    return env


def _fake_hermes(tmp_path: Path) -> tuple[Path, Path, Path]:
    launched = tmp_path / "launched"
    argv = tmp_path / "argv"
    child_env = tmp_path / "child-env"
    fake = tmp_path / "fake-hermes"
    fake.write_text(
        "#!/bin/sh\n"
        f"touch {launched}\n"
        f"printf '%s\\n' \"$@\" > {argv}\n"
        f"env > {child_env}\n"
        "printf 'HERMES_SAFE_SKILL_SMOKE_PASS\\n'\n",
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    return fake, launched, argv


def test_inherited_worker_environment_fails_closed_before_launch(tmp_path: Path):
    fake, launched, _argv = _fake_hermes(tmp_path)
    env = _base_env()
    env.update(WORKER_ENV)
    env["HERMES_SAFE_SMOKE_BIN"] = str(fake)

    result = subprocess.run(
        [str(WRAPPER), "gap-plugging"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "refusing" in result.stderr.lower()
    assert "HERMES_KANBAN_TASK" in result.stderr
    assert not launched.exists(), "unsafe child must not be launched"


@pytest.mark.parametrize(
    "marker",
    [
        "HERMES_DELEGATED_CHILD_CONTEXT",
        "HERMES_SESSION_ID",
        "HERMES_SUPERVISED_CHILD",
        "HERMES_S6_SUPERVISED_CHILD",
    ],
)
def test_process_context_marker_fails_closed_before_launch(tmp_path: Path, marker: str):
    fake, launched, _argv = _fake_hermes(tmp_path)
    env = _base_env()
    env["HERMES_SAFE_SMOKE_BIN"] = str(fake)
    env[marker] = WORKER_ENV[marker]

    result = subprocess.run(
        [str(WRAPPER), "gap-plugging"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 78
    assert marker in result.stderr
    assert not launched.exists(), "worker-context child must not be launched"


def _load_installer():
    path = Path(__file__).parents[1] / "install-hermes-safe-skill-guidance.py"
    spec = importlib.util.spec_from_file_location("safe_skill_guidance_installer", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _unsafe_guidance_fixtures(skills_root: Path) -> tuple[Path, Path]:
    gap = skills_root / "devops" / "gap-plugging" / "SKILL.md"
    sector = skills_root / "devops" / "sector-development-codebase-loop" / "SKILL.md"
    gap.parent.mkdir(parents=True)
    sector.parent.mkdir(parents=True)
    gap.write_text(
        "# fixture gap skill\n"
        "## 8. Smoke-test pattern\n"
        "unsafe inline guidance\n"
        "For mechanism fixtures\n",
        encoding="utf-8",
    )
    sector.write_text(
        "# fixture sector skill\n"
        "Delivery target and routing consumers: `jarvis-os-pm`, `sycode-ai-pm`, "
        "and `yorkstone-supplies-pm`.\n"
        "Do not create/resume a cron, change permissions/branch protection, deploy, "
        "mutate live data, or spawn a dynamic workforce from this skill.\n",
        encoding="utf-8",
    )
    return gap, sector


def test_inconsistent_second_target_failure_rolls_back_both_consumers(tmp_path: Path, monkeypatch):
    installer = _load_installer()
    gap, sector = _unsafe_guidance_fixtures(tmp_path / "skills")
    before = {gap: gap.read_bytes(), sector: sector.read_bytes()}
    calls = []
    real_replace = installer.os.replace

    def fail_on_second_replace(source, destination):
        calls.append(Path(destination))
        if len(calls) == 2:
            raise OSError("fixture second-target replace failure")
        return real_replace(source, destination)

    monkeypatch.setattr(installer.os, "replace", fail_on_second_replace)
    result = installer.main(["--skills-root", str(tmp_path / "skills")])

    assert result == 2
    assert calls == [gap, sector]
    assert gap.read_bytes() == before[gap]
    assert sector.read_bytes() == before[sector]
    assert not list(gap.parent.glob("SKILL.md.bak-safe-skill-smoke-*"))
    assert not list(sector.parent.glob("SKILL.md.bak-safe-skill-smoke-*"))
    assert not list(gap.parent.glob(".SKILL.md.*"))
    assert not list(sector.parent.glob(".SKILL.md.*"))


def test_rollback_failure_retains_recovery_artifacts(tmp_path: Path, monkeypatch):
    installer = _load_installer()
    gap, sector = _unsafe_guidance_fixtures(tmp_path / "skills")
    calls = []
    real_replace = installer.os.replace
    real_copy2 = installer.shutil.copy2

    def fail_on_second_replace(source, destination):
        calls.append(Path(destination))
        if len(calls) == 2:
            raise OSError("fixture second-target replace failure")
        return real_replace(source, destination)

    def fail_during_rollback(source, destination):
        if Path(source).name.startswith("SKILL.md.bak-safe-skill-smoke-"):
            raise OSError("fixture rollback copy failure")
        return real_copy2(source, destination)

    monkeypatch.setattr(installer.os, "replace", fail_on_second_replace)
    monkeypatch.setattr(installer.shutil, "copy2", fail_during_rollback)
    result = installer.main(["--skills-root", str(tmp_path / "skills")])

    assert result == 2
    assert calls == [gap, sector]
    backups = list(gap.parent.glob("SKILL.md.bak-safe-skill-smoke-*"))
    backups += list(sector.parent.glob("SKILL.md.bak-safe-skill-smoke-*"))
    assert len(backups) == 2, "rollback backups must remain for operator recovery"
    assert list(gap.parent.glob(".SKILL.md.*")) or list(sector.parent.glob(".SKILL.md.*")), (
        "uncommitted temp state must remain when rollback fails"
    )


def test_guidance_installer_rewrites_both_consumers_in_fixture(tmp_path: Path):
    installer = Path(__file__).parents[1] / "install-hermes-safe-skill-guidance.py"
    skills_root = tmp_path / "skills"
    gap, sector = _unsafe_guidance_fixtures(skills_root)
    assert "SAFE_SKILL_SMOKE_WRAPPER" not in gap.read_text(encoding="utf-8")
    assert "SAFE_SKILL_SMOKE_WRAPPER" not in sector.read_text(encoding="utf-8")

    result = subprocess.run(
        ["python3", str(installer), "--skills-root", str(skills_root)],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    for path in (gap, sector):
        content = path.read_text(encoding="utf-8")
        assert "SAFE_SKILL_SMOKE_WRAPPER" in content
        assert "hermes-safe-skill-smoke.sh" in content
        assert "hermes --accept-hooks --skills" not in content
        assert "sycode-ai-pm" not in content
        if path == gap:
            assert content.count("For mechanism fixtures") == 1
        else:
            assert "upero-pm (registry tracker alias: sycode-ai)" in content
        assert list(path.parent.glob("SKILL.md.bak-safe-skill-smoke-*"))

    check = subprocess.run(
        ["python3", str(installer), "--check", "--skills-root", str(skills_root)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert check.returncode == 0, check.stderr
    assert "CHECK_PASS" in check.stdout

    second = subprocess.run(
        ["python3", str(installer), "--skills-root", str(skills_root)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert second.returncode == 0, second.stderr
    assert "already installed" in second.stdout


def test_clean_environment_preloads_named_skill_with_empty_toolsets(tmp_path: Path):
    fake, launched, argv = _fake_hermes(tmp_path)
    env = _base_env()
    env["HERMES_SAFE_SMOKE_BIN"] = str(fake)

    result = subprocess.run(
        [str(WRAPPER), "gap-plugging"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "HERMES_SAFE_SKILL_SMOKE_PASS"
    assert launched.exists()
    args = argv.read_text(encoding="utf-8").splitlines()
    assert args == [
        "--accept-hooks",
        "--skills",
        "gap-plugging",
        "--toolsets",
        "",
        "chat",
        "-q",
        "Return exactly HERMES_SAFE_SKILL_SMOKE_PASS. Do not use tools.",
    ]
    child_env = (tmp_path / "child-env").read_text(encoding="utf-8")
    assert not any(
        line.startswith(key + "=")
        for line in child_env.splitlines()
        for key in (*WORKER_ENV, "HERMES_PROFILE")
    )


def test_unknown_or_extra_arguments_fail_without_launch(tmp_path: Path):
    fake, launched, _argv = _fake_hermes(tmp_path)
    env = _base_env()
    env["HERMES_SAFE_SMOKE_BIN"] = str(fake)

    result = subprocess.run(
        [str(WRAPPER), "gap-plugging", "--toolsets", "kanban"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "usage" in result.stderr.lower()
    assert not launched.exists()
