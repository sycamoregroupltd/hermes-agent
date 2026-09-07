from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return result.stdout.strip()


def _repo(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    subprocess.run(
        [
            "git", "-C", str(repo),
            "-c", "user.name=bootstrap", "-c", "user.email=bootstrap@example.com",
            "commit", "-m", "init",
        ],
        check=True,
        capture_output=True,
    )
    return repo


def test_plain_repo_gets_profile_local_identity(tmp_path: Path, monkeypatch) -> None:
    repo = _repo(tmp_path, monkeypatch)

    assert kb._configure_workspace_git_identity(repo, "Codex") is True

    assert _git(repo, "config", "--local", "user.name") == "codex"
    assert _git(repo, "config", "--local", "user.email") == "codex@fleet.local"


def test_linked_worktree_identity_does_not_touch_common_repo(
    tmp_path: Path, monkeypatch
) -> None:
    repo = _repo(tmp_path, monkeypatch)
    worktree = repo / ".worktrees" / "worker"
    kb._ensure_git_worktree(repo, worktree, "wt/worker")

    assert kb._configure_workspace_git_identity(worktree, "grok") is True

    assert _git(worktree, "config", "user.name") == "grok"
    assert _git(worktree, "config", "user.email") == "grok@fleet.local"
    common_name = subprocess.run(
        ["git", "-C", str(repo), "config", "--local", "--get", "user.name"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert common_name.returncode != 0
    assert _git(worktree, "config", "--worktree", "user.name") == "grok"


def test_scratch_workspace_provisions_enforced_clone_identity(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    assert kb._configure_workspace_git_identity(scratch, "fleet-engineer") is True
    wrapper = scratch / ".hermes-git-bin" / "git"
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "HERMES_GIT_REAL": shutil.which("git") or "/usr/bin/git",
        "HERMES_GIT_IDENTITY_NAME": "fleet-engineer",
        "HERMES_GIT_IDENTITY_EMAIL": "fleet-engineer@fleet.local",
    }
    subprocess.run([str(wrapper), "init", "-b", "main"], cwd=scratch, env=env, check=True)
    (scratch / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run([str(wrapper), "add", "README.md"], cwd=scratch, env=env, check=True)
    subprocess.run(
        [str(wrapper), "-c", "user.name=fallback", "commit", "-m", "seed"],
        cwd=scratch, env=env, check=True,
    )

    clone = tmp_path / "clone"
    subprocess.run(
        [str(wrapper), "clone", str(scratch), str(clone)],
        cwd=tmp_path, env=env, check=True,
    )
    assert _git(clone, "config", "--local", "user.name") == "fleet-engineer"
    assert _git(clone, "config", "--local", "user.email") == "fleet-engineer@fleet.local"

    assert _git(scratch, "config", "--local", "user.name") == "fleet-engineer"
    assert _git(scratch, "config", "--local", "user.email") == "fleet-engineer@fleet.local"


@pytest.mark.parametrize("seat", ["fable", "codex", "grok"])
def test_external_seats_receive_profile_scoped_identity_seed(
    tmp_path: Path, seat: str
) -> None:
    scratch = tmp_path / seat
    scratch.mkdir()

    assert kb._configure_workspace_git_identity(scratch, seat) is True
    assert (scratch / ".hermes-git-bin" / "identity").read_text(encoding="utf-8") == (
        f"user.name={seat}\nuser.email={seat}@fleet.local\n"
    )
