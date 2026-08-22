"""Behavior tests for the Git workspace completion gate."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


def git(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def pushed_repo(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    subprocess.run(
        ["git", "init", "--bare", str(remote)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)],
        check=True,
        capture_output=True,
        text=True,
    )
    git(repo, "config", "user.email", "kanban@example.invalid")
    git(repo, "config", "user.name", "Kanban Test")
    (repo / "tracked.txt").write_text("initial\n", encoding="utf-8")
    git(repo, "add", "tracked.txt")
    git(repo, "commit", "-m", "initial")
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-u", "origin", "main")
    return repo


def create_git_task(conn, workspace: Path, *, kind: str = "worktree") -> str:
    return kb.create_task(
        conn,
        title="deliver source change",
        workspace_kind=kind,
        workspace_path=str(workspace),
        triage=True,
    )


def test_clean_pushed_worktree_can_complete(board: Path, pushed_repo: Path) -> None:
    with kb.connect() as conn:
        task_id = create_git_task(conn, pushed_repo)
        assert kb.complete_task(conn, task_id, summary="delivered") is True
        assert kb.get_task(conn, task_id).status == "done"


def test_dirty_worktree_blocks_without_recording_file_names(
    board: Path, pushed_repo: Path
) -> None:
    private_name = "do-not-record-this.env"
    (pushed_repo / private_name).write_text("fixture\n", encoding="utf-8")
    with kb.connect() as conn:
        task_id = create_git_task(conn, pushed_repo)
        with pytest.raises(kb.WorkspaceDeliveryError) as caught:
            kb.complete_task(conn, task_id, summary="not delivered")
        assert caught.value.issues == [{"code": "dirty_worktree", "count": 1}]
        assert kb.get_task(conn, task_id).status == "triage"
        event = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? "
            "AND kind = 'completion_blocked_workspace_delivery' "
            "ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        assert event is not None
        payload = json.loads(event["payload"])
        assert payload["discarded"] is False
        assert private_name not in event["payload"]


def test_local_commit_blocks_then_clears_after_push(
    board: Path, pushed_repo: Path
) -> None:
    (pushed_repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
    git(pushed_repo, "add", "tracked.txt")
    git(pushed_repo, "commit", "-m", "local only")
    with kb.connect() as conn:
        task_id = create_git_task(conn, pushed_repo)
        with pytest.raises(kb.WorkspaceDeliveryError) as caught:
            kb.complete_task(conn, task_id, summary="not pushed")
        assert caught.value.issues == [{"code": "unpushed_commits", "count": 1}]
        git(pushed_repo, "push")
        assert kb.complete_task(conn, task_id, summary="pushed") is True


def test_missing_explicit_worktree_fails_closed(board: Path, tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    with kb.connect() as conn:
        task_id = create_git_task(conn, missing)
        with pytest.raises(kb.WorkspaceDeliveryError) as caught:
            kb.complete_task(conn, task_id, summary="missing")
        assert caught.value.issues == [{"code": "workspace_missing"}]


def test_detached_head_blocks_completion(board: Path, pushed_repo: Path) -> None:
    git(pushed_repo, "checkout", "--detach")
    with kb.connect() as conn:
        task_id = create_git_task(conn, pushed_repo)
        with pytest.raises(kb.WorkspaceDeliveryError) as caught:
            kb.complete_task(conn, task_id, summary="detached")
        assert caught.value.issues == [{"code": "detached_head"}]


def test_repository_without_remote_blocks_completion(
    board: Path, tmp_path: Path
) -> None:
    repo = tmp_path / "local-only"
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)],
        check=True,
        capture_output=True,
        text=True,
    )
    git(repo, "config", "user.email", "kanban@example.invalid")
    git(repo, "config", "user.name", "Kanban Test")
    (repo / "tracked.txt").write_text("initial\n", encoding="utf-8")
    git(repo, "add", "tracked.txt")
    git(repo, "commit", "-m", "initial")
    with kb.connect() as conn:
        task_id = create_git_task(conn, repo)
        with pytest.raises(kb.WorkspaceDeliveryError) as caught:
            kb.complete_task(conn, task_id, summary="no remote")
        assert caught.value.issues == [{"code": "no_remote"}]


def test_non_git_dir_keeps_existing_completion_semantics(
    board: Path, tmp_path: Path
) -> None:
    directory = tmp_path / "research-output"
    directory.mkdir()
    with kb.connect() as conn:
        task_id = create_git_task(conn, directory, kind="dir")
        assert kb.complete_task(conn, task_id, summary="non-git artifact") is True


def test_discard_wip_stashes_dirty_work_records_what_and_completes(
    board: Path, pushed_repo: Path
) -> None:
    # Dirty tracked + untracked work, including a secret-named file whose
    # CONTENTS must never reach the board payload.
    (pushed_repo / "tracked.txt").write_text("changed on branch\n", encoding="utf-8")
    secret_name = "credentials.env"
    secret_contents = "SUPER-SECRET-VALUE-7f3a9c"
    (pushed_repo / secret_name).write_text(secret_contents + "\n", encoding="utf-8")

    with kb.connect() as conn:
        task_id = create_git_task(conn, pushed_repo)
        assert kb.complete_task(
            conn, task_id, summary="abandoned approach", discard_wip=True
        ) is True
        assert kb.get_task(conn, task_id).status == "done"

        event = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? "
            "AND kind = 'workspace_wip_discarded' "
            "ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        assert event is not None, "discard trail event must be recorded"
        payload = json.loads(event["payload"])
        assert payload["discarded"] is True
        assert payload.get("stash_ref")
        assert set(payload["discarded_paths"]) == {"tracked.txt", secret_name}
        # The board records WHAT was discarded (path names + stash ref), never
        # file contents.
        assert secret_contents not in event["payload"]

    # The stash actually set the work aside (recoverable) and left the tree clean.
    assert (pushed_repo / "tracked.txt").read_text(encoding="utf-8") == "initial\n"
    assert not (pushed_repo / secret_name).exists()
    stash_list = git(pushed_repo, "stash", "list")
    assert "kanban discard" in stash_list.stdout


def test_discard_wip_resets_unpushed_commits_records_ref_and_completes(
    board: Path, pushed_repo: Path
) -> None:
    (pushed_repo / "tracked.txt").write_text("local only\n", encoding="utf-8")
    git(pushed_repo, "add", "tracked.txt")
    git(pushed_repo, "commit", "-m", "abandoned local commit")

    with kb.connect() as conn:
        task_id = create_git_task(conn, pushed_repo)
        assert kb.complete_task(
            conn, task_id, summary="abandoned", discard_wip=True
        ) is True
        assert kb.get_task(conn, task_id).status == "done"

        event = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? "
            "AND kind = 'workspace_wip_discarded' "
            "ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        assert event is not None
        payload = json.loads(event["payload"])
        assert payload["discarded"] is True
        assert payload.get("backup_ref") == f"refs/kanban-discard/{task_id}"
        assert len(payload["unpushed_commits"]) == 1

    # The unpushed commit is preserved under the backup ref and the branch is
    # back on its pushed upstream.
    assert git(pushed_repo, "rev-parse", f"refs/kanban-discard/{task_id}").returncode == 0
    assert (pushed_repo / "tracked.txt").read_text(encoding="utf-8") == "initial\n"


def test_discard_wip_cannot_override_unfixable_state(
    board: Path, pushed_repo: Path
) -> None:
    # Detached HEAD is not fixable by discarding WIP: the override must still
    # block and record the unfixable issue.
    git(pushed_repo, "checkout", "--detach")
    with kb.connect() as conn:
        task_id = create_git_task(conn, pushed_repo)
        with pytest.raises(kb.WorkspaceDeliveryError) as caught:
            kb.complete_task(conn, task_id, summary="detached", discard_wip=True)
        assert caught.value.discarded is True
        assert caught.value.issues == [{"code": "detached_head"}]
        assert kb.get_task(conn, task_id).status == "triage"
