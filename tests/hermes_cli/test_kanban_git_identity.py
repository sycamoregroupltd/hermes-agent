from __future__ import annotations

import subprocess
from pathlib import Path

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


def test_non_git_scratch_is_not_misreported_as_configured(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    assert kb._configure_workspace_git_identity(scratch, "fleet-engineer") is False
