from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path


_SCRIPT = Path(__file__).parents[2] / "scripts" / "ci" / "check_git_author_provenance.py"
_SPEC = importlib.util.spec_from_file_location("check_git_author_provenance", _SCRIPT)
assert _SPEC and _SPEC.loader
_provenance = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_provenance)


def _git(repo: Path, *args: str, **kwargs) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        **kwargs,
    )
    return result.stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(
        repo,
        "-c", "user.name=builder", "-c", "user.email=builder@fleet.local",
        "commit", "-m", "base",
    )
    return repo, _git(repo, "rev-parse", "HEAD")


def test_global_claude_author_is_rejected(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path)
    (repo / "change.txt").write_text("bad identity\n", encoding="utf-8")
    _git(repo, "add", "change.txt")
    _git(
        repo,
        "-c", "user.name=Claude", "-c", "user.email=claude@anthropic.com",
        "commit", "-m", "leaked",
    )
    head = _git(repo, "rev-parse", "HEAD")

    old_cwd = Path.cwd()
    try:
        os.chdir(repo)
        offenders = _provenance.leaked_authors(base, head)
    finally:
        os.chdir(old_cwd)
    assert len(offenders) >= 1
    assert offenders[0][1:] == ("author", "Claude", "claude@anthropic.com")


def test_global_claude_committer_is_rejected(tmp_path: Path) -> None:
    """Leak via committer alone (e.g. --author override) must still fail."""
    repo, base = _repo(tmp_path)
    (repo / "change.txt").write_text("committer leak\n", encoding="utf-8")
    _git(repo, "add", "change.txt")
    env = {
        **os.environ,
        "GIT_COMMITTER_NAME": "Claude",
        "GIT_COMMITTER_EMAIL": "claude@anthropic.com",
        "GIT_AUTHOR_NAME": "codex",
        "GIT_AUTHOR_EMAIL": "codex@fleet.local",
    }
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "leaked-committer"],
        check=True,
        capture_output=True,
        env=env,
    )
    head = _git(repo, "rev-parse", "HEAD")

    old_cwd = Path.cwd()
    try:
        os.chdir(repo)
        offenders = _provenance.leaked_authors(base, head)
    finally:
        os.chdir(old_cwd)
    assert any(role == "committer" for _, role, _, _ in offenders)
    assert all(name == "Claude" for _, role, name, _ in offenders if role == "committer")


def test_repo_local_profile_author_is_accepted(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path)
    (repo / "change.txt").write_text("good identity\n", encoding="utf-8")
    _git(repo, "add", "change.txt")
    _git(
        repo,
        "-c", "user.name=codex", "-c", "user.email=codex@fleet.local",
        "commit", "-m", "local",
    )
    head = _git(repo, "rev-parse", "HEAD")

    old_cwd = Path.cwd()
    try:
        os.chdir(repo)
        assert _provenance.leaked_authors(base, head) == []
    finally:
        os.chdir(old_cwd)


def test_failure_message_mentions_author_and_committer(tmp_path: Path, capsys) -> None:
    repo, base = _repo(tmp_path)
    (repo / "change.txt").write_text("msg\n", encoding="utf-8")
    _git(repo, "add", "change.txt")
    _git(
        repo,
        "-c", "user.name=Claude", "-c", "user.email=claude@anthropic.com",
        "commit", "-m", "leaked",
    )
    head = _git(repo, "rev-parse", "HEAD")
    old_cwd = Path.cwd()
    try:
        os.chdir(repo)
        code = _provenance.main(["--base", base, "--head", head])
    finally:
        os.chdir(old_cwd)
    assert code == 1
    err = capsys.readouterr().err
    assert "author/committer" in err
