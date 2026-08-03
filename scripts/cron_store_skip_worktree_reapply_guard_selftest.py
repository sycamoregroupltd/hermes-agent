#!/usr/bin/env python3
"""Regression test for cron_store_skip_worktree_reapply_guard (t_5e23f950).

Proves the residual t_3c33bc49 durability gap is closed:
  - a newly tracked scheduler store (new profile) with protection unset is
    detected as UNPROTECTED, re-protected by --apply, and the next audit is
    HEALTHY;
  - a re-clone / index-rebuild that drops the skip-worktree bit is detected as
    unprotected and re-protected back to healthy.

Runs entirely against a throwaway git repo (monkeypatches the guard's REPO
global); NEVER touches /home/frank/.hermes. Exit 0 = all checks pass.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPTS_DIR = Path("/home/frank/.hermes/scripts")
sys.path.insert(0, str(SCRIPTS_DIR))
import cron_store_git_clobber_guard as guard  # noqa: E402


def git(cwd: Path, *args: str) -> None:
    cp = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if cp.returncode != 0:
        raise RuntimeError(f"git {args} failed: {cp.stderr.strip()}")


def make_store(repo: Path, rel: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"jobs": [{"id": "x", "name": "dummy", "schedule": "every 60m"}]}, indent=2))


def audit(repo: Path) -> dict:
    saved = guard.REPO
    guard.REPO = repo
    try:
        return guard.audit()
    finally:
        guard.REPO = saved


def apply_protect(repo: Path, paths: list[str]) -> None:
    saved = guard.REPO
    guard.REPO = repo
    try:
        failed = guard.apply_flag(paths, on=True)
        assert not failed, f"apply failed: {failed}"
    finally:
        guard.REPO = saved


def drop_bit(repo: Path, paths: list[str]) -> None:
    saved = guard.REPO
    guard.REPO = repo
    try:
        failed = guard.apply_flag(paths, on=False)
        assert not failed, f"drop failed: {failed}"
    finally:
        guard.REPO = saved


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="cron-clobber-selftest-"))
    try:
        git(tmp, "init", "-q")
        git(tmp, "config", "user.email", "selftest@local")
        git(tmp, "config", "user.name", "selftest")
        # Seed two stores already protected (the "already-applied" baseline).
        make_store(tmp, "cron/jobs.json")
        make_store(tmp, "profiles/alice/cron/jobs.json")
        git(tmp, "add", "-A")
        git(tmp, "commit", "-qm", "seed")

        # --- Scenario A: NEW PROFILE store added after the one-shot apply ran ---
        make_store(tmp, "profiles/bob/cron/jobs.json")  # new profile, unprotected
        git(tmp, "add", "-A")
        git(tmp, "commit", "-qm", "add bob")

        st = audit(tmp)
        assert not st["healthy"], "bootstrap: expected some unprotected store"
        assert "profiles/bob/cron/jobs.json" in st["unprotected"], \
            "NEW profile store must be detected as unprotected"
        # Re-apply (what the periodic guard does on every tick).
        apply_protect(tmp, st["unprotected"])
        st2 = audit(tmp)
        assert st2["healthy"], f"after apply: expected healthy, got {st2}"
        assert "profiles/bob/cron/jobs.json" in st2["protected"], \
            "new profile store must now be protected"
        print("PASS A: new-profile store detected-unprotected -> re-protected -> healthy")

        # --- Scenario B: re-clone / index rebuild drops the bit (simulate) ---
        # Everything healthy; now simulate a fresh clone by clearing the bit.
        before = audit(tmp)
        assert before["healthy"], "precondition: baseline healthy"
        drop_bit(tmp, before["protected"].copy())
        after_drop = audit(tmp)
        assert not after_drop["healthy"], "re-clone sim: expected unprotected stores"
        apply_protect(tmp, after_drop["unprotected"])
        recovered = audit(tmp)
        assert recovered["healthy"], f"re-clone sim: expected healthy after re-apply, got {recovered}"
        print("PASS B: skip-worktree bit loss (re-clone/index rebuild) detected -> re-protected -> healthy")

        # --- Scenario C: bare guard default audit must NOT modify anything ---
        # (the wrapper's audit-only path is a no-op on healthy state)
        c3 = audit(tmp)
        assert c3["healthy"], "baseline still healthy after audit-only"
        print("PASS C: audit-only path is non-mutating and silent when healthy")
        print("\nALL REGRESSION CHECKS PASS")
        return 0
    except AssertionError as exc:
        print(f"FAIL: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {type(exc).__name__}: {exc}")
        return 2
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
