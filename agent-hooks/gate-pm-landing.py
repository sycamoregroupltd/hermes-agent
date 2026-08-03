#!/usr/bin/env python3
# Logic for gate-pm-landing.sh — pre_tool_call hook for PM / LANDING profiles.
#
# Context (t_ecf1d553, spun out of t_f2b75a26): six PM seats mandate `git push` and merge
# verification in their SOULs, and /home/frank/uaa-rules/delegated-authority.md:6-7 grants
# them that duty ("PMs land approved work to trunk and verify the merge"). Their
# platform_toolsets allowlist omitted `terminal`, so the duty was unsatisfiable and 80
# dispatch cycles hard-blocked on it. This gate is the containment that makes restoring
# exec safe: it turns each seat's own hand-written rule into a runtime guarantee.
#
# The rule being enforced is NOT invented here. It is the bespoke SOUL section
# "Pushing to our repos — don't false-block (2026-06-15)" already present in all six:
#   2. clean fast-forward to a FEATURE branch, commits are the approved task's work -> may push
#   3. push landing MORE than approved scope, or to main/master -> DON'T push, surface to Frank
#   4. NEVER push to NousResearch/* (no write access)
#
# This is a PM gate, deliberately NOT gate-critic-readonly. A critic must never commit;
# a PM MUST commit (their SOUL requires `git add`+`git commit` before kanban_complete, or
# the worktree is reaped and the artifact is lost). So local mutation is allowed and only
# the irreversible/out-of-scope REMOTE operations are blocked.
#
# BLOCKS (all are remote-side or history-destroying):
#   - push to main/master/trunk/production/release* on any remote
#   - push to any NousResearch/* remote
#   - force push (--force / -f / --force-with-lease) to any branch
#   - push --delete / --mirror / --all, and refspec deletions (:branch)
#   - direct-to-remote history rewrite (push +ref)
#   - deploy-owned tree mutation (~/.hermes/deploy-state/build-tree — fleet git rule)
# ALLOWS (everything a landing PM legitimately does):
#   - git push origin <feature-branch>, -u, --dry-run, --set-upstream
#   - git fetch/pull/clone/ls-remote/merge-base/worktree/status/diff/log
#   - local git add/commit/branch/merge/checkout, tests, typecheck, builds
#
# Contract: read pre_tool_call JSON on stdin; print {} to allow or
# {"decision":"block","reason":...} to veto. FAIL-OPEN on genuine parse ambiguity.
import datetime
import json
import os
import re
import sys

LOG = "/home/frank/.hermes/cron/state/pm-landing-gate.log"

# Branch names that are trunk everywhere in this fleet. Matched against the
# destination ref of a push, after stripping any refs/heads/ prefix and +force marker.
PROTECTED_BRANCH = re.compile(
    r"^(?:refs/heads/)?(?:main|master|trunk|prod|production|release(?:[/-].*)?)$", re.I
)
# Org we have no write access to (SOUL rule 4). Matched anywhere in the command.
FORBIDDEN_REMOTE = re.compile(r"NousResearch/", re.I)
# Deploy-owned tree: the fleet git standard forbids any commit/branch/stash here
# because the deploy pipeline resets it with --hard.
DEPLOY_TREE = re.compile(r"\.hermes/deploy-state/build-tree")

FORCE_FLAG = re.compile(r"(?:^|\s)(?:--force\b|--force-with-lease\b|-f\b|--mirror\b|--delete\b|-d\b|--all\b)")
GIT_PUSH = re.compile(r"\bgit\s+(?:-[^\s]+\s+|--[^\s]+(?:=[^\s]+)?\s+)*push\b", re.I)
# Destructive local ops that can silently discard other agents' unpushed work.
# `clean` flags may appear in any order (-fd, -df, -xfd, -f -d), so match any
# short-flag cluster containing `f`, or a standalone --force.
HISTORY_DESTROY = re.compile(
    r"\bgit\s+(?:reset\s+--hard"
    r"|clean\s+(?:-[a-z]*f[a-z]*\b|--force\b)"
    r"|branch\s+-D)\b",
    re.I,
)

REASON_TRUNK = (
    "PM landing gate: pushing to a protected trunk branch ({tgt}) is blocked. Your SOUL rule 3 "
    "('Pushing to our repos', 2026-06-15) says a push that would land to main/master or exceed "
    "your approved scope must NOT be pushed — leave the task blocked with "
    "'READY TO PUSH, needs Frank review: <branch>, <N commits>, <what they are>'. "
    "Feature-branch pushes are allowed; use `git push origin <feature-branch>`."
)
REASON_NOUS = (
    "PM landing gate: pushing to NousResearch/* is blocked — the fleet has no write access there "
    "(SOUL rule 4). This is a won't-do, not a blocked-waiting; record it as such."
)
REASON_FORCE = (
    "PM landing gate: force/delete/mirror push ({tgt}) is blocked. It can destroy another agent's "
    "unpushed work, which the fleet git standard treats as unrecoverable. Land a normal "
    "fast-forward push, or surface the conflict for review."
)
REASON_DEPLOY = (
    "PM landing gate: mutating the deploy-owned tree ({tgt}) is blocked. The deploy pipeline "
    "resets it with `reset --hard`; anything left there is destroyed and blocks every auto-deploy."
)
REASON_DESTROY = (
    "PM landing gate: destructive history/worktree operation ({tgt}) is blocked. It can discard "
    "commits that are not on origin. Inspect with `git status`/`git log` and route the conflict "
    "instead of resetting."
)


def emit(obj):
    sys.stdout.write(json.dumps(obj))
    sys.exit(0)


def allow():
    emit({})


def block(reason, profile, tool, tgt):
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a") as f:
            f.write(
                f"{datetime.datetime.now(datetime.timezone.utc).isoformat()} BLOCK "
                f"profile={profile} tool={tool} tgt={tgt}\n"
            )
    except Exception:
        pass
    emit({"decision": "block", "action": "block", "reason": reason, "message": reason})


def strip_quotes(tok):
    return tok.strip().strip("'\"")


def push_refspec_targets(cmd):
    """Extract destination branch names from a `git push` invocation.

    Handles: `git push origin feat`, `git push origin HEAD:main`, `git push origin :old`
    (deletion), `git push origin +feat:main` (force refspec), and bare `git push`.
    Returns (targets, deletes_ref, force_refspec).
    """
    # isolate the push segment so a chained `&& echo main` cannot poison the parse
    seg = re.split(r"[;&|]{1,2}", cmd)
    push_seg = next((s for s in seg if GIT_PUSH.search(s)), cmd)
    toks = [strip_quotes(t) for t in push_seg.split() if t.strip()]
    try:
        i = next(idx for idx, t in enumerate(toks) if t == "push")
    except StopIteration:
        return [], False, False
    rest = [t for t in toks[i + 1:] if not t.startswith("-")]
    targets, deletes, force_ref = [], False, False
    # rest[0] is the remote (origin/upstream/url); refspecs follow
    for spec in rest[1:]:
        if spec.startswith(":"):
            deletes = True
            targets.append(spec[1:])
            continue
        if spec.startswith("+"):
            force_ref = True
            spec = spec[1:]
        # src:dst -> the destination is what matters
        targets.append(spec.split(":")[-1] if ":" in spec else spec)
    return targets, deletes, force_ref


def read_payload():
    """Parse the pre_tool_call payload. Fail open (exit allow) on any ambiguity."""
    payload = None
    try:
        payload = json.load(sys.stdin)
    except Exception:
        allow()
    if not isinstance(payload, dict):
        allow()
        return {}
    return payload


d = read_payload()

tool = (d.get("tool_name") or "").strip()
ti = d.get("tool_input") or d.get("args") or {}
if not isinstance(ti, dict):
    allow()
    ti = {}
ex = d.get("extra") if isinstance(d.get("extra"), dict) else {}
profile = ex.get("profile") or d.get("profile") or os.environ.get("HERMES_PROFILE") or "?"

cmd = ti.get("command") or ti.get("cmd") or ti.get("script") or ""
if isinstance(cmd, list):
    cmd = " ".join(map(str, cmd))
cmd = str(cmd)

if not cmd:
    allow()

# A dry-run proves auth without touching the remote — SOUL rule 1 REQUIRES it, always allow.
if "--dry-run" in cmd:
    allow()

# Deploy-owned tree is off-limits for any mutation (fleet git standard).
if DEPLOY_TREE.search(cmd) and re.search(
    r"\bgit\s+(?:commit|add|branch|stash|checkout|switch|reset|merge|rebase|worktree)\b", cmd, re.I
):
    block(REASON_DEPLOY.format(tgt="~/.hermes/deploy-state/build-tree"), profile, tool or "terminal", "deploy-tree")

if HISTORY_DESTROY.search(cmd):
    hit = HISTORY_DESTROY.search(cmd)
    frag = hit.group(0) if hit else "history-destroying git op"
    block(REASON_DESTROY.format(tgt=frag), profile, tool or "terminal", frag)

if GIT_PUSH.search(cmd):
    if FORBIDDEN_REMOTE.search(cmd):
        block(REASON_NOUS, profile, tool or "terminal", "NousResearch")

    targets, deletes, force_ref = push_refspec_targets(cmd)

    force_hit = FORCE_FLAG.search(cmd)
    if force_hit or force_ref or deletes:
        if force_hit:
            flag = force_hit.group(0).strip()
        elif force_ref:
            flag = "+refspec (force)"
        else:
            flag = ":ref (delete)"
        block(REASON_FORCE.format(tgt=flag), profile, tool or "terminal", flag)

    for t in targets:
        if PROTECTED_BRANCH.match(t):
            block(REASON_TRUNK.format(tgt=t), profile, tool or "terminal", t)

    # A bare `git push` with no refspec pushes the CURRENT branch, which we cannot
    # resolve from the payload. push.default=simple makes that a same-name push, so it
    # is only dangerous if HEAD is already trunk. Allow it — the protected-branch case
    # is caught when the branch is named, and blocking every bare push would break the
    # SOUL-mandated "push before stopping" rule on legitimate feature branches.

allow()
