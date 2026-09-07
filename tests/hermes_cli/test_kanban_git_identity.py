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


def _wrapper_env(scratch: Path, name: str = "fleet-engineer") -> dict[str, str]:
    wrapper_dir = kb._git_identity_wrapper_dir(scratch)
    return {
        **os.environ,
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "HERMES_GIT_REAL": shutil.which("git") or "/usr/bin/git",
        "HERMES_GIT_IDENTITY_NAME": name,
        "HERMES_GIT_IDENTITY_EMAIL": f"{name}@fleet.local",
        "PATH": str(wrapper_dir) + os.pathsep + os.environ.get("PATH", ""),
    }


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


def test_scratch_workspace_provisions_enforced_clone_identity(
    tmp_path: Path, monkeypatch
) -> None:
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    assert kb._configure_workspace_git_identity(scratch, "fleet-engineer") is True
    wrapper_dir = kb._git_identity_wrapper_dir(scratch)
    wrapper = wrapper_dir / "git"
    assert wrapper.is_file()
    assert not (scratch / ".hermes-git-bin").exists()
    env = _wrapper_env(scratch)
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
    tmp_path: Path, monkeypatch, seat: str
) -> None:
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    scratch = tmp_path / seat
    scratch.mkdir()

    assert kb._configure_workspace_git_identity(scratch, seat) is True
    identity = kb._git_identity_wrapper_dir(scratch) / "identity"
    assert identity.read_text(encoding="utf-8") == (
        f"user.name={seat}\nuser.email={seat}@fleet.local\n"
    )
    assert not (scratch / ".hermes-git-bin").exists()


def test_wrapper_parses_leading_global_options_for_clone_and_worktree(
    tmp_path: Path, monkeypatch
) -> None:
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    scratch = tmp_path / "source"
    scratch.mkdir()
    assert kb._configure_workspace_git_identity(scratch, "fleet-engineer") is True
    wrapper = kb._git_identity_wrapper_dir(scratch) / "git"
    env = _wrapper_env(scratch)
    subprocess.run(
        [str(wrapper), "init", "-b", "main"],
        cwd=scratch,
        env=env,
        check=True,
    )
    (scratch / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run([str(wrapper), "add", "README.md"], cwd=scratch, env=env, check=True)
    subprocess.run(
        [str(wrapper), "-c", "user.name=fallback", "commit", "-m", "seed"],
        cwd=scratch,
        env=env,
        check=True,
    )

    clone_parent = tmp_path / "clone-parent"
    clone_parent.mkdir()
    subprocess.run(
        [str(wrapper), "-C", str(clone_parent), "clone", str(scratch), "relative-clone"],
        cwd=tmp_path,
        env=env,
        check=True,
    )
    clone = clone_parent / "relative-clone"
    assert _git(clone, "config", "--local", "user.name") == "fleet-engineer"
    assert _git(clone, "config", "--local", "user.email") == "fleet-engineer@fleet.local"

    subprocess.run(
        [str(wrapper), "-C", str(scratch), "config", "extensions.worktreeConfig", "true"],
        cwd=tmp_path,
        env=env,
        check=True,
    )
    subprocess.run(
        [
            str(wrapper),
            "-C",
            str(scratch),
            "worktree",
            "add",
            "-b",
            "wt/leading-options",
            "relative-worktree",
        ],
        cwd=tmp_path,
        env=env,
        check=True,
    )
    worktree = scratch / "relative-worktree"
    assert _git(worktree, "config", "--worktree", "user.name") == "fleet-engineer"
    assert _git(worktree, "config", "--worktree", "user.email") == "fleet-engineer@fleet.local"


def test_wrapper_configures_explicit_init_directory(
    tmp_path: Path, monkeypatch
) -> None:
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    assert kb._configure_workspace_git_identity(scratch, "fleet-engineer") is True
    wrapper = kb._git_identity_wrapper_dir(scratch) / "git"
    env = _wrapper_env(scratch)

    child = scratch / "child"
    subprocess.run(
        [str(wrapper), "init", "-b", "main", "child"],
        cwd=scratch,
        env=env,
        check=True,
    )
    assert child.is_dir()
    assert _git(child, "config", "--local", "user.name") == "fleet-engineer"
    assert _git(child, "config", "--local", "user.email") == "fleet-engineer@fleet.local"
    # Parent scratch must remain non-git / unconfigured as a repo
    assert not (scratch / ".git").exists()


def test_wrapper_tracks_option_bearing_clone_destination(
    tmp_path: Path, monkeypatch
) -> None:
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    source = tmp_path / "source"
    source.mkdir()
    assert kb._configure_workspace_git_identity(source, "fleet-engineer") is True
    wrapper = kb._git_identity_wrapper_dir(source) / "git"
    env = _wrapper_env(source)
    subprocess.run([str(wrapper), "init", "-b", "main"], cwd=source, env=env, check=True)
    (source / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run([str(wrapper), "add", "README.md"], cwd=source, env=env, check=True)
    subprocess.run(
        [str(wrapper), "-c", "user.name=fallback", "commit", "-m", "seed"],
        cwd=source, env=env, check=True,
    )

    template = tmp_path / "template"
    template.mkdir()
    (template / "hooks").mkdir()
    reference = tmp_path / "reference.git"
    subprocess.run(
        ["git", "clone", "--bare", str(source), str(reference)],
        check=True,
        capture_output=True,
    )

    dest_parent = tmp_path / "dest-parent"
    dest_parent.mkdir()
    # Option-bearing clone with omitted explicit destination must still resolve
    # the implied directory name, not mis-parse --reference-if-able / --template.
    subprocess.run(
        [
            str(wrapper),
            "-C",
            str(dest_parent),
            "clone",
            "--template",
            str(template),
            "--reference-if-able",
            str(reference),
            str(source),
        ],
        cwd=tmp_path,
        env=env,
        check=True,
    )
    clone = dest_parent / "source"
    assert clone.is_dir()
    assert _git(clone, "config", "--local", "user.name") == "fleet-engineer"
    assert _git(clone, "config", "--local", "user.email") == "fleet-engineer@fleet.local"


def test_wrapper_does_not_configure_unrelated_minus_c_repo(
    tmp_path: Path, monkeypatch
) -> None:
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    assert kb._configure_workspace_git_identity(scratch, "fleet-engineer") is True
    wrapper = kb._git_identity_wrapper_dir(scratch) / "git"
    env = _wrapper_env(scratch)

    other = _repo(tmp_path / "other-root", monkeypatch)
    # Seed a distinct local identity that must survive a read-only wrapper call.
    subprocess.run(
        [
            "git", "-C", str(other),
            "config", "--local", "user.name", "external-author",
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git", "-C", str(other),
            "config", "--local", "user.email", "external@example.com",
        ],
        check=True,
        capture_output=True,
    )

    subprocess.run(
        [str(wrapper), "-C", str(other), "status"],
        cwd=scratch,
        env=env,
        check=True,
    )
    assert _git(other, "config", "--local", "user.name") == "external-author"
    assert _git(other, "config", "--local", "user.email") == "external@example.com"
