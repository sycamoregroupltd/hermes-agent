#!/usr/bin/env python3
"""Regression test for cron_store_git_clobber_guard (t_5e23f950, t_6c32b13c).

Proves the STRUCTURAL untracking model closes the 2026-08-05 outage class:
  - tracked live cron stores (index + HEAD) are detected as unhealthy;
  - --apply untracks them from the index AND commits the untracking on top of
    HEAD via a temporary index, without touching store content on disk or the
    caller's staged/unstaged state;
  - a `git reset --hard <historical-commit>` that re-tracks the stores is
    detected and repaired back to healthy by the same path;
  - the auto-commit is skipped-detection works while a merge is in progress.

Runs entirely against a throwaway git repo (monkeypatches the guard's REPO
global); NEVER touches /home/frank/.hermes. Exit 0 = all checks pass.

(Filename kept from the retired skip-worktree era so existing references keep
resolving; skip-worktree was abandoned because reset/rebase cleared the bits —
all 27 stores were observed back at 'H' on 2026-08-05.)
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPTS_DIR = Path("/home/frank/.hermes/scripts")
sys.path.insert(0, str(SCRIPTS_DIR))
import cron_store_git_clobber_guard as guard  # noqa: E402


def git(cwd: Path, *args: str) -> str:
    cp = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if cp.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {cp.stderr.strip()}")
    return cp.stdout


def make_store(repo: Path, rel: str, live: bool = False) -> Path:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    job = {"id": "j1", "name": "demo", "enabled": True}
    if live:
        job["next_run_at"] = "2026-08-05T12:00:00+01:00"
    p.write_text(json.dumps({"jobs": [job]}, indent=2) + "\n")
    return p


def main() -> int:
    saved_repo = guard.REPO
    try:
        with tempfile.TemporaryDirectory(prefix="clobber-guard-selftest-") as td:
            tmp = Path(td)
            git(tmp, "init", "-q", "-b", "main")
            git(tmp, "config", "user.email", "selftest@local")
            git(tmp, "config", "user.name", "selftest")

            # Historical state: sanitized stores TRACKED (pre-t_6c32b13c world).
            make_store(tmp, "cron/jobs.json")
            make_store(tmp, "profiles/alice/cron/jobs.json")
            (tmp / "README.md").write_text("fixture\n")
            git(tmp, "add", "-A")
            git(tmp, "commit", "-q", "-m", "historical: stores tracked (sanitized)")
            historical = git(tmp, "rev-parse", "HEAD").strip()
            git(tmp, "commit", "-q", "--allow-empty", "-m", "tip")

            guard.REPO = tmp

            # 1. Tracked stores detected as unhealthy (index + HEAD).
            st = guard.audit()
            assert not st["healthy"], "tracked stores must be unhealthy"
            assert "profiles/alice/cron/jobs.json" in st["index_tracked"], st
            assert "cron/jobs.json" in st["head_tracked"], st
            print("PASS 1: tracked stores detected in index + HEAD")

            # Simulate live runtime state + an unrelated staged change that
            # --apply must preserve.
            live = make_store(tmp, "profiles/alice/cron/jobs.json", live=True)
            (tmp / "README.md").write_text("staged change\n")
            git(tmp, "add", "README.md")

            # 2. --apply path: untrack index + commit untracking on HEAD.
            changed, failed = guard.untrack_index(st["index_tracked"])
            assert not failed, failed
            st = guard.audit()
            assert not st["index_tracked"], st
            changed2, failed2 = guard.commit_untrack_on_head(st["head_tracked"])
            assert not failed2, failed2
            assert any(c.startswith("untrack-commit:") for c in changed2), changed2

            st = guard.audit()
            assert st["healthy"], f"post-apply audit must be healthy: {st}"
            assert "next_run_at" in live.read_text(), "store content must be untouched"
            staged = git(tmp, "diff", "--cached", "--name-only")
            assert "README.md" in staged, "caller's staged change must survive"
            assert "jobs.json" not in git(tmp, "ls-files"), "no store may stay tracked"
            print("PASS 2: --apply untracks index+HEAD, preserves content and staging")

            # 3. Historical reset --hard re-tracks stores (and clobbers content —
            # gitignored files are expendable to git); guard repairs tracking.
            (tmp / ".gitignore").write_text("cron/jobs.json\nprofiles/*/cron/jobs.json\n")
            git(tmp, "reset", "--hard", "-q", historical)
            st = guard.audit()
            assert not st["healthy"], "historical reset must re-detect tracked stores"
            c, f = guard.untrack_index(st["index_tracked"])
            assert not f, f
            c, f = guard.commit_untrack_on_head(guard.audit()["head_tracked"])
            assert not f, f
            assert guard.audit()["healthy"], "must be healthy after historical repair"
            print("PASS 3: reset --hard to historical commit detected -> repaired -> healthy")

            # 4. In-progress merge detection (blocks the auto-commit in main()).
            gitdir = tmp / ".git"
            (gitdir / "MERGE_HEAD").write_text(historical + "\n")
            assert guard.git_op_in_progress() == "merge"
            (gitdir / "MERGE_HEAD").unlink()
            assert guard.git_op_in_progress() is None
            print("PASS 4: in-progress merge detected (auto-commit deferral path)")

        print("\nALL REGRESSION CHECKS PASS")
        return 0
    except AssertionError as exc:
        print(f"FAIL: {exc}")
        return 1
    finally:
        guard.REPO = saved_repo


if __name__ == "__main__":
    sys.exit(main())
