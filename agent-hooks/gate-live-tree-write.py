#!/usr/bin/env python3
# Logic for gate-live-tree-write.sh. Reads a pre_tool_call JSON payload on
# stdin; prints a block reason on stdout, or nothing to allow. FAIL-OPEN on
# any parse error (never wedge the fleet on a guard malfunction).
#
# INCIDENT (t_8b5495cd, 2026-09-09 22:52-22:55 BST): kanban worker t_9722795a
# (profile builder) ran `git commit` three times directly inside the LIVE
# Hermes source checkout /home/frank/.hermes/hermes-agent — the tree every
# running gateway imports code from. No hook stopped it: gate-terminal-docker-
# safety.sh only classifies docker/podman commands, and gate-config-writes.sh
# only classifies .hermes/config.yaml writes. This hook closes that gap by
# refusing (a) any git write/mutate subcommand and (b) any file-write tool
# call whose target path resolves under the protected live source tree, for
# any KANBAN WORKER (HERMES_KANBAN_TASK set in the environment — the signal
# that this is an unattended dispatcher-spawned run, not an interactive
# operator/Frank session). Bugs belong in a scratch worktree/contribution
# branch per AMENDMENT 7; see live-tree-mutation-guard skill.
#
# Bypass: ALLOW_LIVE_TREE_WRITE=1 in the environment (operator-set only, never
# settable mid-reasoning by a worker) lets the write through — mirrors the
# ALLOW_CONFIG_WRITE=1 pattern in gate-config-writes.py and the
# HERMES_ALLOW_CHECKOUT=1 pattern in scripts/git-live-checkout-guard.sh.
from __future__ import annotations

import datetime
import json
import os
import re
import sys

LOG = "/home/frank/.hermes/cron/state/live-tree-write-gate.log"

# Canonicalized, space-separated protected roots. Override via env for tests.
PROTECTED_ROOTS = [
    p.rstrip("/")
    for p in os.environ.get(
        "HERMES_LIVE_TREE_GUARD_ROOTS",
        "/home/frank/.hermes/hermes-agent",
    ).split(":")
    if p.strip()
]

GIT_WRITE_SUBCOMMANDS = {
    "commit", "checkout", "switch", "reset", "merge", "rebase", "stash",
    "pull", "cherry-pick", "revert", "clean", "am", "apply", "push",
    "branch", "tag", "gc", "reflog", "filter-branch", "worktree",
}
# Subcommands in GIT_WRITE_SUBCOMMANDS that have safe read-only forms we must
# not falsely flag (e.g. `git branch --list`, `git worktree list`).
_SAFE_SUFFIXES = {
    "branch": {"-l", "--list", "-v", "-vv", "-a", "-r"},
    "worktree": {"list"},
    "tag": {"-l", "--list"},
    "stash": {"list", "show"},
    "reflog": {"show"},
}

FILE_WRITE_TOOLS = {
    "create_file", "apply_patch", "str_replace", "str_replace_editor",
    "write_file", "edit_file", "file_write", "fs_write", "patch_file",
    "patch",
}


def _log(line: str) -> None:
    try:
        with open(LOG, "a") as f:
            f.write(
                f"{datetime.datetime.now(datetime.timezone.utc).isoformat()} {line}\n"
            )
    except Exception:
        pass


def _under_protected_root(path: str) -> str:
    """Return the matching protected root if `path` resolves under one, else ''."""
    if not path:
        return ""
    try:
        # Best-effort normalize without touching the filesystem (os.path.normpath
        # is enough here — these are hook-time string checks, not a security
        # boundary against symlink tricks; the git wrapper / worktree isolation
        # remain the enforcement layer of record).
        norm = os.path.normpath(path)
    except Exception:
        norm = path
    for root in PROTECTED_ROOTS:
        if norm == root or norm.startswith(root + "/"):
            return root
    return ""


def _collect_strings(o) -> list:
    acc = []
    if isinstance(o, str):
        acc.append(o)
    elif isinstance(o, dict):
        for v in o.values():
            acc += _collect_strings(v)
    elif isinstance(o, list):
        for v in o:
            acc += _collect_strings(v)
    return acc


def _git_subcommand(tokens: list) -> str:
    """Given a shell-split command list starting at (or containing) `git`,
    find the git invocation and return its write subcommand if dangerous,
    else ''. Handles leading global options / -C like the live checkout
    guard does (see scripts/git-live-checkout-guard.sh)."""
    for i, tok in enumerate(tokens):
        if tok == "git" or tok.endswith("/git"):
            rest = tokens[i + 1:]
            j = 0
            while j < len(rest):
                arg = rest[j]
                if arg in ("-C", "-c", "--git-dir", "--work-tree",
                           "--namespace", "--config-env"):
                    j += 2
                    continue
                if arg.startswith(("-C", "--git-dir=", "--work-tree=",
                                    "--namespace=", "--config-env=")):
                    j += 1
                    continue
                if arg == "--":
                    j += 1
                    continue
                if arg.startswith("-"):
                    j += 1
                    continue
                sub = arg
                tail = rest[j + 1:]
                if sub in GIT_WRITE_SUBCOMMANDS:
                    safe = _SAFE_SUFFIXES.get(sub)
                    if safe and any(t in safe for t in tail):
                        return ""
                    return sub
                return ""
            return ""
    return ""


def classify_terminal(cmd: str) -> str:
    if not cmd or "git" not in cmd:
        return ""
    # Cheap path already applied by caller shell script; do the real parse here.
    try:
        import shlex
        # Split on shell separators too so `cd x && git commit` is inspected.
        segments = re.split(r"(?:&&|\|\||;|\|)", cmd)
    except Exception:
        segments = [cmd]
    for seg in segments:
        try:
            tokens = shlex.split(seg, posix=True)
        except Exception:
            tokens = seg.split()
        if "git" not in tokens and not any(t.endswith("/git") for t in tokens):
            continue
        sub = _git_subcommand(tokens)
        if not sub:
            continue
        # Does this invocation target (via -C, or cwd token, or plain path
        # in the command text) the protected tree? We can't always resolve
        # cwd here, so also flag if the protected root string literally
        # appears ANYWHERE in the full command (covers `git -C <root> ...`
        # and a preceding `cd <root> && git ...` in an earlier &&-segment).
        for root in PROTECTED_ROOTS:
            if root in cmd:
                return (
                    f"git {sub} refused: kanban worker attempted a git write "
                    f"operation directly inside the live Hermes source tree "
                    f"({root}) — every running gateway imports code from this "
                    f"path. Bugs go upstream from a scratch worktree/contribution "
                    f"branch (AMENDMENT 7); see live-tree-mutation-guard skill. "
                    f"Operator-only override: ALLOW_LIVE_TREE_WRITE=1."
                )
    return ""


def classify_file_write(tool: str, ti: dict) -> str:
    path = str(ti.get("path") or ti.get("file_path") or ti.get("filename") or "")
    has_content = any(
        k in ti and ti.get(k) is not None
        for k in ("content", "new_str", "new_string", "text", "file_text",
                   "old_str", "patch")
    )
    root = _under_protected_root(path)
    if root and (has_content or tool in FILE_WRITE_TOOLS):
        return (
            f"file write refused: kanban worker attempted to write {path!r} "
            f"directly inside the live Hermes source tree ({root}). Prepare "
            f"the change in an isolated worktree/contribution branch and land "
            f"it via os-reviewer, per live-tree-mutation-guard. Operator-only "
            f"override: ALLOW_LIVE_TREE_WRITE=1."
        )
    # apply_patch / diff-style payloads: scan for a file-target header
    # pointing into the protected tree (mirrors gate-config-writes.py Case C).
    if tool in FILE_WRITE_TOOLS or has_content:
        for line in "\n".join(_collect_strings(ti)).splitlines():
            m = re.search(r"([^\s\"]*hermes-agent/[^\s\"]+)", line)
            if m and re.search(
                r"(\*\*\*|\+\+\+|^---|\bFile:|Update File|Add File|"
                r"Move \(to\|from\)|Delete File)",
                line,
            ):
                candidate = m.group(1)
                for root in PROTECTED_ROOTS:
                    if root.endswith("hermes-agent") and "hermes-agent/" in candidate:
                        return (
                            f"patch refused: target {candidate!r} resolves "
                            f"under the live Hermes source tree ({root}). "
                            f"Prepare the change in an isolated worktree and "
                            f"land it via os-reviewer. Operator-only override: "
                            f"ALLOW_LIVE_TREE_WRITE=1."
                        )
    return ""


def main() -> int:
    raw = sys.stdin.read()
    # Only a KANBAN WORKER run (dispatcher-spawned, unattended) is gated —
    # never an interactive operator/Frank session.
    if not os.environ.get("HERMES_KANBAN_TASK"):
        return 0
    if os.environ.get("ALLOW_LIVE_TREE_WRITE") == "1":
        _log(
            "ALLOW(bypass) ALLOW_LIVE_TREE_WRITE=1 "
            f"task={os.environ.get('HERMES_KANBAN_TASK')}"
        )
        return 0
    try:
        d = json.loads(raw)
    except Exception:
        return 0
    if not isinstance(d, dict):
        return 0
    tool = (d.get("tool_name") or "").strip()
    ti = d.get("tool_input") or d.get("args") or {}
    if not isinstance(ti, dict):
        ti = {}

    reason = ""
    if tool == "terminal" or "command" in ti or "cmd" in ti or "script" in ti:
        cmd = ti.get("command") or ti.get("cmd") or ti.get("script") or ""
        if isinstance(cmd, list):
            cmd = " ".join(map(str, cmd))
        reason = classify_terminal(str(cmd))

    if not reason:
        reason = classify_file_write(tool, ti)

    if reason:
        _log(
            f"BLOCK task={os.environ.get('HERMES_KANBAN_TASK')} "
            f"profile={os.environ.get('HERMES_PROFILE', '?')} tool={tool} "
            f"reason={reason[:160]}"
        )
        sys.stdout.write(reason)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
