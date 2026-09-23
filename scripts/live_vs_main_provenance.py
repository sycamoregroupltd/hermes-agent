#!/usr/bin/env python3
"""Weekly live-vs-main provenance probe (kanban t_403eb2aa).

Probe ONLY the 11-row seed register from
Control-Plane/task-evidence/2026-09-21-t_e483012d-live-vs-main-provenance.md.

Isolation HOLD — this script is read-only:
  - no git checkout/switch/reset/stash/pull/fetch/merge/rebase/commit/clean/worktree
  - no hermes update, no live-tree write, no auto-merge, no auto-reset
  - ancestry via `git ls-remote` and `gh compare` only (never merge-base / rev-list)
  - shallow clones therefore never use local history walks

Cron contract (no_agent):
  exit 0 + stdout  = weekly report (always; dry-run must match the seed register)
  exit != 0        = wrapper/crash only
  --dry-run / LIVE_VS_MAIN_DRY_RUN=1  -> stdout only, no kanban write

Live tick (no --dry-run): if any row is not reachable from its default branch
or a probe fails, mint/comment one jarvis-os card per ISO week
(idempotency_key live-vs-main-provenance:YYYY-WW, assignee guardian).
Dirty is reported separately and does not by itself mint a card.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

SCHEMA = "live-vs-main-provenance/v1"
SEED_NOTE = (
    "/home/frank/obsidian-fleet-vault/Control-Plane/task-evidence/"
    "2026-09-21-t_e483012d-live-vs-main-provenance.md"
)
HERMES_BIN = os.environ.get("HERMES_BIN", "/home/frank/.local/bin/hermes")
BOARD = os.environ.get("LIVE_VS_MAIN_BOARD", "jarvis-os")
ASSIGNEE = os.environ.get("LIVE_VS_MAIN_ASSIGNEE", "guardian")
TZ_NAME = os.environ.get("LIVE_VS_MAIN_TZ", "Europe/London")
SSH_MAC = os.environ.get("LIVE_VS_MAIN_SSH", "mac")
GH_TIMEOUT = 45
GIT_TIMEOUT = 30
SSH_TIMEOUT = 25
DOCKER_TIMEOUT = 20
SYS_TIMEOUT = 15

GIT_WRITE_VERBS = frozenset(
    {
        "checkout",
        "switch",
        "reset",
        "stash",
        "pull",
        "fetch",
        "merge",
        "rebase",
        "commit",
        "clean",
        "worktree",
        "cherry-pick",
        "revert",
        "am",
        "gc",
        "gc.auto",
        "update-ref",
        "symbolic-ref",
        "push",
        "tag",
        "branch",
        "remote",
        "config",
        "add",
        "rm",
        "mv",
        "restore",
        "sparse-checkout",
        "submodule",
        "filter-branch",
        "replace",
        "notes",
        "reflog",
    }
)
GIT_READ_VERBS = frozenset(
    {
        "rev-parse",
        "status",
        "ls-remote",
        "hash-object",
        "remote",  # only `remote get-url` is allowed; enforced below
        "symbolic-ref",  # local read of refs/remotes/origin/HEAD only
        "show-ref",
        "cat-file",
        "ls-files",
        "diff",
        "log",
        "describe",
        "name-rev",
        "rev-list",  # FORBIDDEN even though git has it — intercepted
        "merge-base",  # FORBIDDEN
    }
)

# Seed register — ids and default branches frozen from t_e483012d.
COMPONENTS: list[dict[str, Any]] = [
    {
        "id": "mac-backtalk",
        "host": "mac",
        "kind": "git-worktree",
        "default_branch": "main",
        "gh_repo": "jaredrhod/backtalk",
        "remote": "https://github.com/jaredrhod/backtalk.git",
    },
    {
        "id": "dgx-hermes-overlay",
        "host": "dgx",
        "kind": "git-dir",
        "path": "/home/frank/.hermes",
        "default_branch": "main",
        "gh_repo": "sycamoregroupltd/hermes-agent",
        "remote": "git@github.com:sycamoregroupltd/hermes-agent.git",
        "shallow_ok": True,
    },
    {
        "id": "dgx-hermes-agent",
        "host": "dgx",
        "kind": "git-dir",
        "path": "/home/frank/.hermes/hermes-agent",
        "default_branch": "main",
        "gh_repo": "NousResearch/hermes-agent",
        "remote": "https://github.com/NousResearch/hermes-agent.git",
        "also_latest_release_tag": True,
    },
    {
        "id": "mac-uaa",
        "host": "mac",
        "kind": "mac-git-dir",
        "path": "/Users/frankspencer/ultimate-agent-architecture",
        "default_branch": "main",
        "gh_repo": "sycamoregroupltd/ultimate-agent-architecture",
        "remote": "https://github.com/sycamoregroupltd/ultimate-agent-architecture.git",
    },
    {
        "id": "dgx-upero-web",
        "host": "dgx",
        "kind": "git-dir",
        "path": "/home/frank/upero",
        "default_branch": "main",
        "gh_repo": "sycamoregroupltd/Sycode-AI",
        "remote": "https://github.com/sycamoregroupltd/Sycode-AI.git",
        "dirty_pathspec": None,
    },
    {
        "id": "dgx-mission-gateway",
        "host": "dgx",
        "kind": "git-dir",
        "path": "/home/frank/mission-gateway",
        "default_branch": None,  # resolved from ls-remote HEAD
        "gh_repo": "sycamoregroupltd/dgx-home-workspace",
        "remote": "git@github.com:sycamoregroupltd/dgx-home-workspace.git",
        "dirty_pathspec": "mission-gateway",
        "repo_root": "/home/frank",
    },
    {
        "id": "mac-jarvis-app",
        "host": "mac",
        "kind": "plist-commit",
        "plist": "/Users/frankspencer/Applications/Jarvis.app/Contents/Info.plist",
        "plist_key": "JarvisGitCommit",
        "default_branch": "master",
        "gh_repo": "sycamoregroupltd/jarvis-mac",
        "remote": "https://github.com/sycamoregroupltd/jarvis-mac.git",
    },
    {
        "id": "dgx-sycodetrading-server",
        "host": "dgx",
        "kind": "docker-label",
        "container": "sycodetrading-server",
        "label": "com.sycodetrading.git.sha",
        "default_branch": "main",
        "gh_repo": "sycamoregroupltd/sycode-trading",
        "remote": "https://github.com/sycamoregroupltd/sycode-trading.git",
    },
    {
        "id": "mac-second-brain-backup",
        "host": "mac",
        "kind": "launchd-hash",
        "script": (
            "/Users/frankspencer/Library/Application Support/Sycamore/"
            "second-brain/backup_dgx_second_brain.py"
        ),
        "label": "gui/501/com.sycamore.second-brain-backup",
        "gh_repo": "sycamoregroupltd/ultimate-agent-architecture",
        "blob_ref": "main:control-spine/scripts/backup_dgx_second_brain.py",
        "stamp": "$HOME/dgx-fleet-backups/second-brain.ok",
    },
    {
        "id": "dgx-jarvis-os-dashboard",
        "host": "dgx",
        "kind": "git-dir",
        "path": "/home/frank/jarvis-os-dashboard",
        "default_branch": "master",
        "gh_repo": "sycamoregroupltd/jarvis-os-dashboard",
        "remote": "https://github.com/sycamoregroupltd/jarvis-os-dashboard.git",
    },
    {
        "id": "dgx-lean-core-units",
        "host": "dgx",
        "kind": "systemd-units",
        "units": [
            "hermes-lean-core-gateway.service",
            "hermes-lean-core-cron.service",
            "hermes-lean-core-gateway-watchdog.service",
            "hermes-lean-core-voice.service",
        ],
    },
]

FORBIDDEN_SUBSTR = (
    "hermes update",
    "git checkout",
    "git switch",
    "git reset",
    "git stash",
    "git pull",
    "git fetch",
    "git merge",
    "git rebase",
    "merge-base",
    "rev-list",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_week_key(now: datetime | None = None) -> str:
    now = now or datetime.now(ZoneInfo(TZ_NAME))
    ic = now.astimezone(ZoneInfo(TZ_NAME)).isocalendar()
    return f"live-vs-main-provenance:{ic.year}-{ic.week:02d}"


def run(
    argv: list[str],
    timeout: int,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    joined = " ".join(argv)
    low = joined.lower()
    for bad in FORBIDDEN_SUBSTR:
        if bad in low:
            raise RuntimeError(f"refused forbidden command: {joined}")
    try:
        p = subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        return p.returncode, p.stdout or "", p.stderr or ""
    except subprocess.TimeoutExpired as e:
        return 124, (e.stdout or "") if isinstance(e.stdout, str) else "", f"timeout after {timeout}s"
    except OSError as e:
        return 1, "", f"{type(e).__name__}: {e}"


def git_ro(git_args: list[str], cwd: str | None = None, timeout: int = GIT_TIMEOUT) -> tuple[int, str, str]:
    if not git_args:
        raise RuntimeError("empty git argv")
    verb = git_args[0]
    if verb in ("merge-base", "rev-list"):
        raise RuntimeError(f"forbidden git verb {verb} (use ls-remote / gh compare)")
    if verb in GIT_WRITE_VERBS and verb not in {"remote", "symbolic-ref"}:
        raise RuntimeError(f"forbidden git write verb {verb}")
    if verb == "remote" and git_args[1:] != ["get-url", "origin"]:
        raise RuntimeError(f"forbidden git remote form: {git_args}")
    if verb == "symbolic-ref" and git_args[1:] != ["refs/remotes/origin/HEAD"]:
        raise RuntimeError(f"forbidden git symbolic-ref form: {git_args}")
    argv = ["git"]
    if cwd:
        argv += ["-C", cwd]
    argv += git_args
    return run(argv, timeout=timeout)


def ssh_mac(remote_cmd: str, timeout: int = SSH_TIMEOUT) -> tuple[int, str, str]:
    argv = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=12",
        "-o",
        "StrictHostKeyChecking=accept-new",
        SSH_MAC,
        remote_cmd,
    ]
    return run(argv, timeout=timeout)


def ls_remote_head(remote: str, branch: str | None) -> tuple[str | None, str | None, str]:
    """Return (branch, sha, note) from git ls-remote --symref. Never fetch."""
    rc, out, err = git_ro(["ls-remote", "--symref", remote, "HEAD"], timeout=GH_TIMEOUT)
    if rc != 0:
        return None, None, f"ls-remote HEAD failed rc={rc} {(err or out).strip()[:200]}"
    branch_name = branch
    sha = None
    for line in out.splitlines():
        if line.startswith("ref: ") and "\tHEAD" in line:
            ref = line.split("\t", 1)[0].replace("ref: ", "").strip()
            if ref.startswith("refs/heads/"):
                branch_name = ref[len("refs/heads/") :]
        elif line.endswith("\tHEAD") or line.endswith(" HEAD"):
            sha = line.split()[0]
    if branch and branch != branch_name:
        # Caller pinned a default branch; resolve that ref too.
        rc2, out2, err2 = git_ro(
            ["ls-remote", "--heads", remote, f"refs/heads/{branch}"],
            timeout=GH_TIMEOUT,
        )
        if rc2 == 0:
            for line in out2.splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[1].endswith(f"/{branch}"):
                    return branch, parts[0], "ls-remote heads"
        return branch, None, f"ls-remote heads {branch} failed {(err2 or out2).strip()[:160]}"
    return branch_name, sha, "ls-remote HEAD"


def gh_compare(repo: str, base: str, head: str) -> dict[str, Any]:
    """GitHub compare. Never merge-base/rev-list. 404 => no common ancestor / missing."""
    rc, out, err = run(
        [
            "gh",
            "api",
            f"repos/{repo}/compare/{base}...{head}",
            "--jq",
            "{status,ahead_by,behind_by,total_commits,message}",
        ],
        timeout=GH_TIMEOUT,
    )
    if rc != 0:
        text = (out + err).strip()
        kind = "COMPARE_404" if "HTTP 404" in text or '"status":"404"' in text or "Not Found" in text else "COMPARE_FAIL"
        if "No common ancestor" in text:
            kind = "NO_COMMON_ANCESTOR"
        return {"ok": False, "kind": kind, "error": text[:300]}
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return {"ok": False, "kind": "COMPARE_FAIL", "error": out[:200]}
    data["ok"] = True
    return data


def classify_ancestry(live_sha: str | None, default_sha: str | None, cmp: dict[str, Any] | None) -> tuple[str, str]:
    """Return (ancestry, reachable).

    Reachable from default branch == HEAD is contained in default (equal or behind).
    Not reachable == ahead, diverged, or no common ancestor.
    """
    if not live_sha:
        return "UNKNOWN", "unknown"
    if default_sha and live_sha == default_sha:
        return "ON_DEFAULT", "yes"
    if not cmp:
        return "UNKNOWN", "unknown"
    if not cmp.get("ok"):
        kind = cmp.get("kind") or "UNKNOWN"
        if kind == "NO_COMMON_ANCESTOR":
            return kind, "no"
        # live SHA != default tip and compare unavailable: fail-visible, not silent.
        if default_sha and live_sha != default_sha:
            return kind or "SHA_MISMATCH", "no"
        return kind, "unknown"
    status = (cmp.get("status") or "").lower()
    ahead = int(cmp.get("ahead_by") or 0)
    behind = int(cmp.get("behind_by") or 0)
    if status in {"identical"} or (ahead == 0 and behind == 0):
        return "ON_DEFAULT", "yes"
    if status == "behind" or (ahead == 0 and behind > 0):
        return "BEHIND", "yes"
    if status == "ahead" or (ahead > 0 and behind == 0):
        return "AHEAD", "no"
    if status == "diverged" or (ahead > 0 and behind > 0):
        return "DIVERGED", "no"
    return status.upper() or "UNKNOWN", "unknown"


def git_facts(path: str) -> dict[str, Any]:
    facts: dict[str, Any] = {"path": path}
    rc, out, err = git_ro(["rev-parse", "--is-inside-work-tree"], cwd=path)
    if rc != 0 or out.strip() != "true":
        facts["error"] = f"not a git work tree: {(err or out).strip()[:160]}"
        return facts
    rc, out, _ = git_ro(["rev-parse", "--show-toplevel"], cwd=path)
    facts["toplevel"] = out.strip() if rc == 0 else path
    rc, out, _ = git_ro(["rev-parse", "--is-shallow-repository"], cwd=path)
    facts["shallow"] = out.strip() == "true"
    rc, out, _ = git_ro(["rev-parse", "--abbrev-ref", "HEAD"], cwd=path)
    facts["branch"] = out.strip() if rc == 0 else ""
    rc, out, _ = git_ro(["rev-parse", "HEAD"], cwd=path)
    facts["head"] = out.strip() if rc == 0 else ""
    rc, out, _ = git_ro(["remote", "get-url", "origin"], cwd=path)
    facts["origin"] = out.strip() if rc == 0 else ""
    return facts


def dirty_count(repo: str, pathspec: str | None = None) -> int | None:
    args = ["status", "--porcelain", "-uall"]
    if pathspec:
        args += ["--", pathspec]
    rc, out, _ = git_ro(args, cwd=repo, timeout=60)
    if rc != 0:
        return None
    lines = [ln for ln in out.splitlines() if ln.strip()]
    return len(lines)


def row_base(comp: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": comp["id"],
        "host": comp["host"],
        "kind": comp["kind"],
        "live_sha": "",
        "live_ref": "",
        "default_branch": comp.get("default_branch") or "",
        "default_sha": "",
        "ancestry": "UNKNOWN",
        "reachable": "unknown",
        "dirty": None,
        "shallow": False,
        "notes": [],
        "verdict": "PROBE_FAIL",
    }


def add_note(row: dict[str, Any], note: str) -> None:
    if note and note not in row["notes"]:
        row["notes"].append(note)


def finalize(row: dict[str, Any]) -> dict[str, Any]:
    reachable = row.get("reachable")
    if row.get("verdict") == "PROBE_FAIL":
        return row
    if reachable == "no":
        row["verdict"] = "DRIFT"
    elif reachable == "yes":
        row["verdict"] = "ON_DEFAULT"
    else:
        row["verdict"] = "UNKNOWN"
    return row


def probe_git_dir(comp: dict[str, Any]) -> dict[str, Any]:
    row = row_base(comp)
    path = comp.get("repo_root") or comp["path"]
    facts = git_facts(path)
    if facts.get("error"):
        add_note(row, facts["error"])
        return row
    row["shallow"] = bool(facts.get("shallow"))
    row["live_ref"] = facts.get("branch") or ""
    row["live_sha"] = facts.get("head") or ""
    if not row["live_sha"]:
        add_note(row, "empty HEAD")
        return row
    remote = comp.get("remote") or facts.get("origin")
    branch, default_sha, note = ls_remote_head(remote, comp.get("default_branch"))
    row["default_branch"] = branch or row["default_branch"]
    row["default_sha"] = default_sha or ""
    add_note(row, note)
    if facts.get("shallow"):
        add_note(row, "shallow: ancestry via ls-remote/gh compare only")
    cmp = None
    if comp.get("gh_repo") and row["live_sha"] and row["default_branch"]:
        cmp = gh_compare(comp["gh_repo"], row["default_branch"], row["live_sha"])
        if not cmp.get("ok"):
            add_note(row, f"{cmp.get('kind')}: {str(cmp.get('error') or '')[:160]}")
        else:
            add_note(
                row,
                f"gh compare {row['default_branch']}...HEAD status={cmp.get('status')} "
                f"ahead={cmp.get('ahead_by')} behind={cmp.get('behind_by')}",
            )
    ancestry, reachable = classify_ancestry(row["live_sha"], row["default_sha"], cmp)
    row["ancestry"] = ancestry
    row["reachable"] = reachable
    spec = comp.get("dirty_pathspec")
    repo_for_dirty = facts.get("toplevel") or path
    row["dirty"] = dirty_count(repo_for_dirty, spec)
    if comp.get("also_latest_release_tag"):
        tag_info = latest_release_tag(comp["gh_repo"])
        if tag_info.get("tag"):
            add_note(row, f"latest_release_tag={tag_info['tag']} sha={tag_info.get('commit') or '?'}")
            if tag_info.get("commit") and row["live_sha"] != tag_info["commit"]:
                add_note(row, "live SHA != latest official release tag (report only; no update)")
    row["verdict"] = "OK"  # finalize() overwrites
    return finalize(row)


def latest_release_tag(repo: str) -> dict[str, str]:
    rc, out, err = run(
        ["gh", "api", f"repos/{repo}/releases/latest", "--jq", "{tag_name,target_commitish}"],
        timeout=GH_TIMEOUT,
    )
    if rc != 0:
        return {"error": (err or out)[:160]}
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return {"error": out[:160]}
    tag = data.get("tag_name") or ""
    commit = ""
    if tag:
        rc2, out2, _ = run(
            ["gh", "api", f"repos/{repo}/git/ref/tags/{tag}", "--jq", ".object.sha,.object.type"],
            timeout=GH_TIMEOUT,
        )
        # object may be an annotated tag; peel when possible
        rc3, out3, _ = run(
            [
                "gh",
                "api",
                f"repos/{repo}/git/refs/tags/{tag}",
                "--jq",
                "{sha:.object.sha,type:.object.type}",
            ],
            timeout=GH_TIMEOUT,
        )
        if rc3 == 0:
            try:
                ref = json.loads(out3)
                if ref.get("type") == "commit":
                    commit = ref.get("sha") or ""
                elif ref.get("type") == "tag" and ref.get("sha"):
                    rc4, out4, _ = run(
                        ["gh", "api", f"repos/{repo}/git/tags/{ref['sha']}", "--jq", ".object.sha"],
                        timeout=GH_TIMEOUT,
                    )
                    if rc4 == 0:
                        commit = out4.strip().strip('"')
            except json.JSONDecodeError:
                pass
    return {"tag": tag, "commit": commit}


def probe_mac_git_dir(comp: dict[str, Any]) -> dict[str, Any]:
    """Read-only git facts on the Mac via ssh. Never git-write."""
    row = row_base(comp)
    path = comp["path"]
    q = shlex.quote(path)
    rc, out, err = ssh_mac(
        f"echo TOP=$(git -C {q} rev-parse --show-toplevel); "
        f"echo BRANCH=$(git -C {q} rev-parse --abbrev-ref HEAD); "
        f"echo HEAD=$(git -C {q} rev-parse HEAD); "
        f"echo SHALLOW=$(git -C {q} rev-parse --is-shallow-repository); "
        f"echo ORIGIN=$(git -C {q} remote get-url origin); "
        f"echo DIRTY=$(git -C {q} status --porcelain -uall | wc -l | tr -d ' ')"
    )
    if rc != 0:
        add_note(row, f"ssh git failed: {(err or out).strip()[:200]}")
        return row
    parsed: dict[str, str] = {}
    for ln in out.splitlines():
        if "=" in ln:
            k, v = ln.split("=", 1)
            parsed[k.strip()] = v.strip()
    row["live_ref"] = parsed.get("BRANCH") or ""
    row["live_sha"] = parsed.get("HEAD") or ""
    row["shallow"] = parsed.get("SHALLOW") == "true"
    try:
        row["dirty"] = int(parsed.get("DIRTY") or "0")
    except ValueError:
        row["dirty"] = None
    if not row["live_sha"]:
        add_note(row, f"empty HEAD from ssh: {out.strip()[:160]}")
        return row
    remote = comp.get("remote") or parsed.get("ORIGIN")
    branch, default_sha, note = ls_remote_head(remote, comp.get("default_branch"))
    row["default_branch"] = branch or row["default_branch"]
    row["default_sha"] = default_sha or ""
    add_note(row, note)
    if row["shallow"]:
        add_note(row, "shallow: ancestry via ls-remote/gh compare only")
    cmp = None
    if comp.get("gh_repo") and row["live_sha"] and row["default_branch"]:
        cmp = gh_compare(comp["gh_repo"], row["default_branch"], row["live_sha"])
        if not cmp.get("ok"):
            add_note(row, f"{cmp.get('kind')}: {str(cmp.get('error') or '')[:160]}")
        else:
            add_note(
                row,
                f"gh compare {row['default_branch']}...HEAD status={cmp.get('status')} "
                f"ahead={cmp.get('ahead_by')} behind={cmp.get('behind_by')}",
            )
    ancestry, reachable = classify_ancestry(row["live_sha"], row["default_sha"], cmp)
    row["ancestry"] = ancestry
    row["reachable"] = reachable
    row["verdict"] = "OK"
    return finalize(row)


def probe_mac_backtalk(comp: dict[str, Any]) -> dict[str, Any]:
    row = row_base(comp)
    rc, out, err = ssh_mac(
        "PID=$(pgrep -f 'backtalk.main' | head -1); "
        "echo PID=$PID; "
        "if [ -z \"$PID\" ]; then echo ERROR=no-backtalk-pid; exit 0; fi; "
        "CWD=$(lsof -a -p \"$PID\" -d cwd -Fn | awk '/^n/{print substr($0,2); exit}'); "
        "echo CWD=$CWD; "
        "echo BRANCH=$(git -C \"$CWD\" rev-parse --abbrev-ref HEAD); "
        "echo HEAD=$(git -C \"$CWD\" rev-parse HEAD); "
        "echo SHALLOW=$(git -C \"$CWD\" rev-parse --is-shallow-repository); "
        "echo ORIGIN=$(git -C \"$CWD\" remote get-url origin); "
        "echo DIRTY=$(git -C \"$CWD\" status --porcelain -uall | wc -l | tr -d ' ')"
    )
    if rc != 0:
        add_note(row, f"ssh failed: {(err or out).strip()[:200]}")
        return row
    parsed = _parse_backtalk_text(out)
    if parsed.get("error"):
        add_note(row, parsed["error"])
        return row
    row["live_ref"] = parsed.get("branch") or ""
    row["live_sha"] = parsed.get("head") or ""
    row["shallow"] = bool(parsed.get("shallow"))
    row["dirty"] = parsed.get("dirty")
    add_note(row, f"cwd={parsed.get('cwd') or '?'} pid={parsed.get('pid') or '?'}")
    branch, default_sha, note = ls_remote_head(comp["remote"], comp.get("default_branch"))
    row["default_branch"] = branch or row["default_branch"]
    row["default_sha"] = default_sha or ""
    add_note(row, note)
    cmp = gh_compare(comp["gh_repo"], row["default_branch"], row["live_sha"]) if row["live_sha"] else None
    if cmp and not cmp.get("ok"):
        add_note(row, f"{cmp.get('kind')}: {str(cmp.get('error') or '')[:160]}")
    elif cmp:
        add_note(
            row,
            f"gh compare {row['default_branch']}...HEAD status={cmp.get('status')} "
            f"ahead={cmp.get('ahead_by')} behind={cmp.get('behind_by')}",
        )
    ancestry, reachable = classify_ancestry(row["live_sha"], row["default_sha"], cmp)
    row["ancestry"] = ancestry
    row["reachable"] = reachable
    row["verdict"] = "OK"
    return finalize(row)


def _parse_backtalk_text(out: str) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for ln in out.splitlines():
        s = ln.strip()
        if not s or "=" not in s:
            continue
        k, v = s.split("=", 1)
        k, v = k.strip(), v.strip()
        if k == "PID":
            data["pid"] = v
        elif k == "CWD":
            data["cwd"] = v
        elif k == "BRANCH":
            data["branch"] = v
        elif k == "HEAD":
            data["head"] = v
        elif k == "SHALLOW":
            data["shallow"] = v == "true"
        elif k == "ORIGIN":
            data["origin"] = v
        elif k == "DIRTY":
            try:
                data["dirty"] = int(v)
            except ValueError:
                data["dirty"] = None
        elif k == "ERROR":
            data["error"] = v
    return data


def probe_plist_commit(comp: dict[str, Any]) -> dict[str, Any]:
    row = row_base(comp)
    rc, out, err = ssh_mac(
        f'defaults read {shlex.quote(comp["plist"])} {shlex.quote(comp["plist_key"])} 2>/dev/null; '
        f'defaults read {shlex.quote(comp["plist"])} JarvisBuiltAt 2>/dev/null'
    )
    if rc != 0:
        add_note(row, f"plist read failed: {(err or out).strip()[:200]}")
        return row
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    live = lines[0] if lines else ""
    built = lines[1] if len(lines) > 1 else ""
    row["live_sha"] = live
    row["live_ref"] = "Jarvis.app Info.plist JarvisGitCommit"
    if built:
        add_note(row, f"JarvisBuiltAt={built}")
    branch, default_sha, note = ls_remote_head(comp["remote"], comp.get("default_branch"))
    row["default_branch"] = branch or row["default_branch"]
    row["default_sha"] = default_sha or ""
    add_note(row, note)
    cmp = gh_compare(comp["gh_repo"], row["default_branch"], live) if live else None
    if cmp and not cmp.get("ok"):
        add_note(row, f"{cmp.get('kind')}: {str(cmp.get('error') or '')[:160]}")
    elif cmp:
        add_note(
            row,
            f"gh compare {row['default_branch']}...plist status={cmp.get('status')} "
            f"ahead={cmp.get('ahead_by')} behind={cmp.get('behind_by')}",
        )
    ancestry, reachable = classify_ancestry(live, row["default_sha"], cmp)
    row["ancestry"] = ancestry
    row["reachable"] = reachable
    row["dirty"] = 0
    row["verdict"] = "OK"
    return finalize(row)


def probe_docker_label(comp: dict[str, Any]) -> dict[str, Any]:
    row = row_base(comp)
    fmt = "{{.State.Running}} {{index .Config.Labels %s}}" % json.dumps(comp["label"])
    rc, out, err = run(
        ["docker", "inspect", "-f", fmt, comp["container"]],
        timeout=DOCKER_TIMEOUT,
    )
    if rc != 0:
        add_note(row, f"docker inspect failed: {(err or out).strip()[:200]}")
        return row
    parts = out.strip().split(None, 1)
    running = parts[0] if parts else ""
    sha = parts[1] if len(parts) > 1 else ""
    row["live_sha"] = sha
    row["live_ref"] = f"container:{comp['container']} running={running}"
    branch, default_sha, note = ls_remote_head(comp["remote"], comp.get("default_branch"))
    row["default_branch"] = branch or row["default_branch"]
    row["default_sha"] = default_sha or ""
    add_note(row, note)
    cmp = None
    if sha and row["default_branch"]:
        cmp = gh_compare(comp["gh_repo"], row["default_branch"], sha)
        if cmp.get("ok"):
            add_note(
                row,
                f"gh compare {row['default_branch']}...image status={cmp.get('status')} "
                f"ahead={cmp.get('ahead_by')} behind={cmp.get('behind_by')}",
            )
        else:
            add_note(row, f"{cmp.get('kind')}: {str(cmp.get('error') or '')[:160]}")
    ancestry, reachable = classify_ancestry(sha, row["default_sha"], cmp)
    row["ancestry"] = ancestry
    row["reachable"] = reachable
    row["dirty"] = 0
    row["verdict"] = "OK"
    return finalize(row)


def probe_launchd_hash(comp: dict[str, Any]) -> dict[str, Any]:
    row = row_base(comp)
    rc, out, err = ssh_mac(
        "echo HASH=$(git hash-object "
        + shlex.quote(comp["script"])
        + "); "
        "echo EXISTS=$([ -f "
        + shlex.quote(comp["script"])
        + " ] && echo yes || echo no); "
        "launchctl print "
        + shlex.quote(comp["label"])
        + " 2>/dev/null | awk '/state = |pid = |program = |runs = /{print}'; "
        "STAMP=\"$HOME/dgx-fleet-backups/second-brain.ok\"; "
        "if [ -f \"$STAMP\" ]; then echo STAMP=present; cat \"$STAMP\"; else echo STAMP=missing; fi"
    )
    if rc != 0:
        add_note(row, f"ssh failed: {(err or out).strip()[:200]}")
        return row
    live_hash = ""
    stamp = "unknown"
    state = ""
    for ln in out.splitlines():
        s = ln.strip()
        if s.startswith("HASH="):
            live_hash = s.split("=", 1)[1].strip()
        elif s.startswith("STAMP="):
            stamp = s.split("=", 1)[1].strip()
        elif "state =" in s:
            state = s
    row["live_sha"] = live_hash
    row["live_ref"] = "backup_dgx_second_brain.py git-hash-object"
    add_note(row, state or "launchd print")
    add_note(row, f"stamp={stamp}")
    # Compare blob to origin/main via GitHub Contents API (no clone, no merge-base).
    rc2, out2, err2 = run(
        [
            "gh",
            "api",
            f"repos/{comp['gh_repo']}/contents/control-spine/scripts/backup_dgx_second_brain.py?ref=main",
            "--jq",
            "{sha,size}",
        ],
        timeout=GH_TIMEOUT,
    )
    origin_sha = ""
    if rc2 == 0:
        try:
            meta = json.loads(out2)
            origin_sha = meta.get("sha") or ""
        except json.JSONDecodeError:
            add_note(row, "contents api unparseable")
    else:
        add_note(row, f"contents api fail {(err2 or out2).strip()[:160]}")
    row["default_branch"] = "main"
    row["default_sha"] = origin_sha
    if live_hash and origin_sha and live_hash == origin_sha:
        row["ancestry"] = "ON_DEFAULT"
        row["reachable"] = "yes"
    elif live_hash and origin_sha:
        row["ancestry"] = "HASH_MISMATCH"
        row["reachable"] = "no"
        add_note(row, "installed script blob != origin/main blob")
    else:
        row["ancestry"] = "UNKNOWN"
        row["reachable"] = "unknown"
    row["dirty"] = 0
    row["verdict"] = "OK"
    return finalize(row)


def probe_systemd_units(comp: dict[str, Any]) -> dict[str, Any]:
    row = row_base(comp)
    units = comp["units"]
    present = []
    active = []
    missing = []
    for unit in units:
        rc, out, _ = run(["systemctl", "--user", "is-active", unit], timeout=SYS_TIMEOUT)
        state = (out or "").strip() or "unknown"
        rc_show, out_show, _ = run(
            ["systemctl", "--user", "show", unit, "-p", "LoadState,ActiveState,FragmentPath", "--no-pager"],
            timeout=SYS_TIMEOUT,
        )
        load = ""
        for ln in (out_show or "").splitlines():
            if ln.startswith("LoadState="):
                load = ln.split("=", 1)[1]
        if load in {"not-found", "masked"} or rc_show != 0:
            missing.append(unit)
            add_note(row, f"{unit}: load={load or 'not-found'} active={state}")
        else:
            present.append(unit)
            if state == "active":
                active.append(unit)
            add_note(row, f"{unit}: load={load} active={state}")
    row["live_ref"] = f"{len(active)}/{len(units)} active, {len(present)}/{len(units)} installed"
    row["live_sha"] = f"active={len(active)} installed={len(present)}"
    row["default_branch"] = "n/a"
    row["default_sha"] = "4/4 active (seed expectation)"
    row["dirty"] = 0
    if len(present) == 0:
        row["ancestry"] = "UNITS_MISSING"
        row["reachable"] = "no"
        add_note(row, "lean-core 4 units not installed on this host")
    elif len(active) == 4:
        row["ancestry"] = "ON_DEFAULT"
        row["reachable"] = "yes"
    else:
        row["ancestry"] = "UNITS_DEGRADED"
        row["reachable"] = "no"
    row["verdict"] = "OK"
    return finalize(row)


PROBES = {
    "git-dir": probe_git_dir,
    "git-worktree": probe_mac_backtalk,
    "mac-git-dir": probe_mac_git_dir,
    "plist-commit": probe_plist_commit,
    "docker-label": probe_docker_label,
    "launchd-hash": probe_launchd_hash,
    "systemd-units": probe_systemd_units,
}


def probe_all() -> list[dict[str, Any]]:
    rows = []
    for comp in COMPONENTS:
        fn = PROBES[comp["kind"]]
        try:
            row = fn(comp)
        except Exception as e:  # noqa: BLE001 — row must fail visible, not crash the report
            row = row_base(comp)
            add_note(row, f"{type(e).__name__}: {e}")
        rows.append(row)
    return rows


def render(rows: list[dict[str, Any]], measured_at: str) -> str:
    drift = [r for r in rows if r["verdict"] == "DRIFT"]
    fails = [r for r in rows if r["verdict"] == "PROBE_FAIL"]
    dirty = [r for r in rows if isinstance(r.get("dirty"), int) and r["dirty"] > 0]
    on_def = [r for r in rows if r["verdict"] == "ON_DEFAULT"]
    overall = "DRIFT" if drift or fails else "ON_DEFAULT"
    lines = [
        f"# live-vs-main provenance  {measured_at}",
        f"schema: {SCHEMA}",
        f"seed: {SEED_NOTE}",
        f"register_rows: {len(rows)} (seed=11)",
        f"verdict: {overall}  drift={len(drift)} on_default={len(on_def)} probe_fail={len(fails)} dirty_rows={len(dirty)}",
        "isolation: HOLD — no merge/reset/checkout/fetch/hermes-update/live-tree-write",
        "ancestry: ls-remote + gh compare only (no merge-base, no rev-list)",
        "",
        "| id | verdict | ancestry | reachable | live | default | dirty | notes |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        live = (r.get("live_sha") or "")[:12]
        default = (r.get("default_sha") or "")[:12]
        dirty_s = r.get("dirty")
        dirty_s = "" if dirty_s is None else str(dirty_s)
        notes = "; ".join(r.get("notes") or [])[:180].replace("|", "/")
        lines.append(
            f"| {r['id']} | {r['verdict']} | {r['ancestry']} | {r['reachable']} | "
            f"{live} | {default} | {dirty_s} | {notes} |"
        )
    lines.append("")
    lines.append("## Drift (HEAD not reachable from component default branch)")
    if not drift:
        lines.append("(none)")
    else:
        for r in drift:
            lines.append(f"- {r['id']}: {r['ancestry']} live={(r.get('live_sha') or '')[:12]} default={(r.get('default_sha') or '')[:12]} ref={r.get('live_ref')}")
    lines.append("")
    lines.append("## Dirty (reported separately; not an auto-reset signal)")
    if not dirty:
        lines.append("(none)")
    else:
        for r in dirty:
            lines.append(f"- {r['id']}: dirty={r['dirty']}")
    lines.append("")
    lines.append("## Probe failures")
    if not fails:
        lines.append("(none)")
    else:
        for r in fails:
            lines.append(f"- {r['id']}: {'; '.join(r.get('notes') or [])}")
    lines.append("")
    lines.append("## On default")
    if not on_def:
        lines.append("(none)")
    else:
        for r in on_def:
            extra = f" dirty={r['dirty']}" if r.get("dirty") else ""
            lines.append(f"- {r['id']}: {(r.get('live_sha') or '')[:12]}{extra}")
    lines.append("")
    return "\n".join(lines) + "\n"


def maybe_file_card(report: str, rows: list[dict[str, Any]], dry_run: bool) -> str | None:
    if dry_run or os.environ.get("LIVE_VS_MAIN_SKIP_KANBAN") == "1":
        return None
    drift = [r for r in rows if r["verdict"] in {"DRIFT", "PROBE_FAIL"}]
    if not drift:
        return None
    key = iso_week_key()
    title = f"[weekly] live-vs-main provenance {key.split(':', 1)[1]} ({len(drift)} drift/fail)"
    body = (
        "Weekly live-vs-main provenance (no auto-reset).\n\n"
        "Isolation HOLD. Do not merge, reset, checkout, fetch, or run hermes update.\n\n"
        + report
    )
    env = os.environ.copy()
    for k in (
        "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_RUN_ID",
        "HERMES_KANBAN_CLAIM_LOCK",
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_WORKSPACE",
        "HERMES_TENANT",
    ):
        env.pop(k, None)
    argv = [
        HERMES_BIN,
        "kanban",
        "--board",
        BOARD,
        "create",
        "--assignee",
        ASSIGNEE,
        "--idempotency-key",
        key,
        "--created-by",
        "live-vs-main-provenance",
        "--body",
        body[:12000],
        "--json",
        title,
    ]
    rc, out, err = run(argv, timeout=60, env=env)
    if rc != 0:
        return f"kanban create failed rc={rc} {(err or out).strip()[:200]}"
    try:
        data = json.loads(out)
        tid = (data.get("task") or data).get("id") or data.get("id")
    except json.JSONDecodeError:
        tid = out.strip()[:40]
    return f"card {tid} key={key}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Weekly live-vs-main provenance probe")
    parser.add_argument("--dry-run", action="store_true", help="stdout only; no kanban write")
    parser.add_argument("--json", action="store_true", help="also emit JSON after the markdown report to stdout")
    args = parser.parse_args(argv)
    dry = bool(args.dry_run or os.environ.get("LIVE_VS_MAIN_DRY_RUN") == "1")
    measured = utc_now()
    rows = probe_all()
    if len(rows) != 11:
        print(f"FATAL: register_rows={len(rows)} expected 11", file=sys.stderr)
        return 1
    report = render(rows, measured)
    sys.stdout.write(report)
    if args.json:
        sys.stdout.write(json.dumps({"measured_at": measured, "rows": rows}, indent=2) + "\n")
    extra = maybe_file_card(report, rows, dry)
    if extra:
        sys.stdout.write(f"\nkanban: {extra}\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:  # noqa: BLE001
        print(f"CRASH: {type(e).__name__}: {e}", file=sys.stderr)
        raise
