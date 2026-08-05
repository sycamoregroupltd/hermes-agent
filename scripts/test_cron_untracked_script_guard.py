#!/usr/bin/env python3
"""Self-tests for cron_untracked_script_guard.

These tests exist because the ORIGINAL guard shipped green while 187 enabled jobs were at
risk: its FAIL fixture only exercised the (non-existent) global dir, and its tracked-check
used `git ls-files --others --exclude-standard`, which silently suppresses gitignored paths.
Every fixture below builds a throwaway git repo (no remote) and runs the REAL guard binary.

Cases:
  NEG-A  profile-local script, gitignored + untracked          -> exit 1 (the regression)
  POS-A  same script committed (tracked)                        -> exit 0
  POS-B  relative script, committed                             -> exit 0
  POS-C  absolute path inside owning profile scripts, committed -> exit 0  (criterion 3)
  NEG-C  missing script (nonexistent path)                      -> exit 1 (criterion 5)
  NEG-D  absolute path escaping the owning scripts dir         -> exit 1 (criterion 2)
  NEG-E  untracked script in the ROOT <repo>/cron/jobs.json     -> exit 1 (N1)
  POS-D  disabled root-store job with untracked script          -> exit 0 (N1)
  NEG-F  untracked script invoked from a prompt/command string  -> exit 1 (N2)
  POS-E  tracked command-ref + out-of-repo command-ref          -> exit 0 (N2)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

GUARD = Path(__file__).resolve().parent / "cron_untracked_script_guard.py"

# Track profile cron stores AND depth-1 profile scripts by default (unlike the
# production repo, which gitignores them pre-carve-out). This lets the POSITIVE and
# ABSOLUTE cases commit their scripts. NEG-A re-ignores its one script to exercise the
# gitignored-profile-script regression that the original guard missed.
BASE_GITIGNORE = """\
/profiles/*
!/profiles/*/
!/profiles/*/cron/
/profiles/*/cron/*
!/profiles/*/cron/jobs.json
!/profiles/*/scripts/
/profiles/*/scripts/*
!/profiles/*/scripts/*.py
!/profiles/*/scripts/*.sh
"""

PASS = 0
FAIL = 1


def run_guard(repo: Path) -> tuple[int, str]:
    env = dict(os.environ)
    env["HERMES_HOME"] = str(repo / "profiles" / "jarvis")
    r = subprocess.run(
        [sys.executable, str(GUARD)],
        cwd=str(repo), env=env,
        text=True, capture_output=True, timeout=120,
    )
    return r.returncode, r.stdout + r.stderr


def init_repo(tmp: Path) -> Path:
    repo = tmp / "repo"
    repo.mkdir()
    (repo / ".gitignore").write_text(BASE_GITIGNORE)
    (repo / "profiles" / "jarvis" / "scripts").mkdir(parents=True)
    (repo / "profiles" / "jarvis" / "cron").mkdir(parents=True)
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "selftest@local"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "selftest"], check=True)
    # CONTROL: install executable post-checkout hook so the guard's self-heal
    # assertion passes. Temp repos are throwaway; content doesn't need to match
    # the real scripts/git-live-cron-postcheckout.sh because that source file
    # is absent from the fixture trees (hash-compare gate skipped at line 302).
    hook = repo / ".git" / "hooks" / "post-checkout"
    hook.write_text("#!/bin/sh\n")
    hook.chmod(0o755)
    return repo


def write_store(repo: Path, jobs: list[dict]) -> None:
    (repo / "profiles" / "jarvis" / "cron" / "jobs.json").write_text(
        json.dumps({"jobs": jobs})
    )


def commit(repo: Path, paths: list[str]) -> None:
    subprocess.run(["git", "-C", str(repo), "add", *paths], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "fixture"], check=True)


def test_neg_a_profile_gitignored_untracked() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="guard-nega-"))
    try:
        repo = init_repo(tmp)
        (repo / "profiles" / "jarvis" / "scripts" / "evil_untracked.py").write_text(
            "print('evil')\n")
        write_store(repo, [{"id": "a", "name": "a", "script": "evil_untracked.py",
                            "enabled": True}])
        # Re-ignore THIS script (override the fixture's track-by-default) so we exercise
        # the gitignored-profile-script regression the original guard missed.
        with open(repo / ".gitignore", "a") as fh:
            fh.write("/profiles/*/scripts/evil_untracked.py\n")
        # Script is untracked AND gitignored; only the store + gitignore are committed.
        commit(repo, [".gitignore", "profiles/jarvis/cron/jobs.json"])
        rc, out = run_guard(repo)
        if rc != 1 or "evil_untracked.py" not in out:
            print("FAIL NEG-A: expected exit 1 naming the gitignored untracked script")
            print(out)
            return FAIL
        print("PASS NEG-A: guard fails (exit 1) on profile-local gitignored + untracked script")
        return PASS
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_pos_a_tracked() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="guard-posa-"))
    try:
        repo = init_repo(tmp)
        (repo / "profiles" / "jarvis" / "scripts" / "good.py").write_text("print('ok')\n")
        write_store(repo, [{"id": "p", "name": "p", "script": "good.py",
                           "enabled": True}])
        commit(repo, [".gitignore", "profiles/jarvis/cron/jobs.json",
                      "profiles/jarvis/scripts/good.py"])
        rc, out = run_guard(repo)
        if rc != 0:
            print("FAIL POS-A: expected exit 0 for a tracked profile script")
            print(out)
            return FAIL
        print("PASS POS-A: guard green (exit 0) for a tracked profile-local script")
        return PASS
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_pos_b_relative() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="guard-posb-"))
    try:
        repo = init_repo(tmp)
        (repo / "profiles" / "jarvis" / "scripts" / "rel.py").write_text("print('ok')\n")
        write_store(repo, [{"id": "r", "name": "r", "script": "rel.py",
                           "enabled": True}])
        commit(repo, [".gitignore", "profiles/jarvis/cron/jobs.json",
                      "profiles/jarvis/scripts/rel.py"])
        rc, out = run_guard(repo)
        if rc != 0:
            print("FAIL POS-B: expected exit 0 for a tracked relative script")
            print(out)
            return FAIL
        print("PASS POS-B: guard green (exit 0) for a tracked relative script")
        return PASS
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_pos_c_absolute_inside() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="guard-posc-"))
    try:
        repo = init_repo(tmp)
        script = repo / "profiles" / "jarvis" / "scripts" / "abs.py"
        script.write_text("print('ok')\n")
        # Absolute path that points INSIDE the owning profile scripts dir.
        write_store(repo, [{"id": "c", "name": "c", "script": str(script),
                           "enabled": True}])
        commit(repo, [".gitignore", "profiles/jarvis/cron/jobs.json",
                      "profiles/jarvis/scripts/abs.py"])
        rc, out = run_guard(repo)
        if rc != 0:
            print("FAIL POS-C: expected exit 0 for an absolute path inside the owning scripts dir")
            print(out)
            return FAIL
        print("PASS POS-C: guard green (exit 0) for an absolute path inside the owning scripts dir")
        return PASS
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_neg_c_missing() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="guard-negc-"))
    try:
        repo = init_repo(tmp)
        # Script never written; .gitignore ignores scripts/* anyway.
        write_store(repo, [{"id": "m", "name": "m", "script": "does_not_exist.py",
                           "enabled": True}])
        commit(repo, [".gitignore", "profiles/jarvis/cron/jobs.json"])
        rc, out = run_guard(repo)
        if rc != 1 or "MISSING" not in out:
            print("FAIL NEG-C: expected exit 1 with MISSING for a nonexistent script")
            print(out)
            return FAIL
        print("PASS NEG-C: guard fails (exit 1) with MISSING for a nonexistent script")
        return PASS
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_neg_d_absolute_escaping() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="guard-negd-"))
    try:
        repo = init_repo(tmp)
        # Absolute path OUTSIDE the owning profile scripts dir (e.g. global scripts/).
        outside = repo / "scripts" / "escape.py"
        outside.parent.mkdir(parents=True, exist_ok=True)
        outside.write_text("print('escape')\n")
        write_store(repo, [{"id": "d", "name": "d", "script": str(outside),
                           "enabled": True}])
        commit(repo, [".gitignore", "profiles/jarvis/cron/jobs.json",
                      "scripts/escape.py"])
        rc, out = run_guard(repo)
        if rc != 1 or "SCHEDULER-BLOCKED" not in out:
            print("FAIL NEG-D: expected exit 1 with SCHEDULER-BLOCKED for an escaping absolute path")
            print(out)
            return FAIL
        print("PASS NEG-D: guard fails (exit 1) with SCHEDULER-BLOCKED for an absolute path escaping the owning scripts dir")
        return PASS
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_neg_e_root_store_untracked() -> int:
    """N1: the ROOT <repo>/cron/jobs.json store must be scanned, not just profiles/*."""
    tmp = Path(tempfile.mkdtemp(prefix="guard-nege-"))
    try:
        repo = init_repo(tmp)
        (repo / "cron").mkdir(parents=True, exist_ok=True)
        (repo / "scripts").mkdir(parents=True, exist_ok=True)
        (repo / "scripts" / "root_untracked.py").write_text("print('root')\n")
        (repo / "cron" / "jobs.json").write_text(json.dumps({"jobs": [
            {"id": "rootjob", "name": "rootjob", "script": "root_untracked.py",
             "enabled": True}]}))
        # Store committed, script deliberately NOT committed.
        commit(repo, [".gitignore", "cron/jobs.json"])
        rc, out = run_guard(repo)
        if rc != 1 or "root_untracked.py" not in out or "<root>" not in out:
            print("FAIL NEG-E: expected exit 1 naming the untracked ROOT-store script")
            print(out)
            return FAIL
        print("PASS NEG-E: guard fails (exit 1) on an untracked script in the ROOT cron store")
        return PASS
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_pos_d_root_store_disabled_ignored() -> int:
    """A DISABLED root-store job with an untracked script stays green (matches live state)."""
    tmp = Path(tempfile.mkdtemp(prefix="guard-posd-"))
    try:
        repo = init_repo(tmp)
        (repo / "cron").mkdir(parents=True, exist_ok=True)
        (repo / "scripts").mkdir(parents=True, exist_ok=True)
        (repo / "scripts" / "root_disabled.py").write_text("print('root')\n")
        (repo / "cron" / "jobs.json").write_text(json.dumps({"jobs": [
            {"id": "rootoff", "name": "rootoff", "script": "root_disabled.py",
             "enabled": False}]}))
        commit(repo, [".gitignore", "cron/jobs.json"])
        rc, out = run_guard(repo)
        if rc != 0:
            print("FAIL POS-D: expected exit 0 for a DISABLED root-store job")
            print(out)
            return FAIL
        print("PASS POS-D: guard green (exit 0) for a disabled root-store job")
        return PASS
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_neg_f_command_ref_untracked() -> int:
    """N2: a script invoked via an absolute path inside a prompt/command string is checked."""
    tmp = Path(tempfile.mkdtemp(prefix="guard-negf-"))
    try:
        repo = init_repo(tmp)
        helper = repo / "scripts" / "shelled_out.py"
        helper.parent.mkdir(parents=True, exist_ok=True)
        helper.write_text("print('shelled')\n")
        write_store(repo, [{"id": "cmd", "name": "cmd", "enabled": True,
                            "prompt": f"Run `python3 {helper}` and report the output."}])
        # helper deliberately NOT committed
        commit(repo, [".gitignore", "profiles/jarvis/cron/jobs.json"])
        rc, out = run_guard(repo)
        if rc != 1 or "COMMAND-REF untracked" not in out or "shelled_out.py" not in out:
            print("FAIL NEG-F: expected exit 1 with COMMAND-REF untracked for a prompt-invoked script")
            print(out)
            return FAIL
        print("PASS NEG-F: guard fails (exit 1) on an untracked script invoked from a prompt string")
        return PASS
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_pos_e_command_ref_tracked_and_external() -> int:
    """A tracked command-ref is green; an out-of-repo command-ref is not a violation."""
    tmp = Path(tempfile.mkdtemp(prefix="guard-pose-"))
    try:
        repo = init_repo(tmp)
        helper = repo / "scripts" / "shelled_ok.py"
        helper.parent.mkdir(parents=True, exist_ok=True)
        helper.write_text("print('ok')\n")
        outside = tmp / "elsewhere" / "external.py"
        outside.parent.mkdir(parents=True, exist_ok=True)
        outside.write_text("print('external')\n")
        write_store(repo, [{"id": "cmdok", "name": "cmdok", "enabled": True,
                            "prompt": f"Run `python3 {helper}` then `python3 {outside}`."}])
        commit(repo, [".gitignore", "profiles/jarvis/cron/jobs.json",
                      "scripts/shelled_ok.py"])
        rc, out = run_guard(repo)
        if rc != 0:
            print("FAIL POS-E: expected exit 0 for a tracked command-ref plus an out-of-repo ref")
            print(out)
            return FAIL
        print("PASS POS-E: guard green (exit 0) for a tracked command-ref; out-of-repo ref ignored")
        return PASS
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    if not GUARD.exists():
        print(f"FAIL: guard not found at {GUARD}", file=sys.stderr)
        return 2
    results = [
        test_neg_a_profile_gitignored_untracked(),
        test_pos_a_tracked(),
        test_pos_b_relative(),
        test_pos_c_absolute_inside(),
        test_neg_c_missing(),
        test_neg_d_absolute_escaping(),
        test_neg_e_root_store_untracked(),
        test_pos_d_root_store_disabled_ignored(),
        test_neg_f_command_ref_untracked(),
        test_pos_e_command_ref_tracked_and_external(),
    ]
    failed = sum(1 for r in results if r != PASS)
    print(f"\n=== self-test summary: {len(results) - failed}/{len(results)} passed ===")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
