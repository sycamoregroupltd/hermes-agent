#!/usr/bin/env python3
# CANONICAL-COPY RULE: this profile-local file is a dry-run exec shim for the
# central canonical script /home/frank/.hermes/scripts/kanban_dedupe_guard.py.
# Kanban t_d787b0f8 (2026-08-11): do NOT replace this shim with a byte copy of
# the canonical — the canonical defaults to ENFORCEMENT (dry_run=False) and a
# no-agent cron invoking it without args posts real board mutations.
"""Dry-run wrapper for kanban-dedupe-guard cron f96bc59a9657 (jarvis profile).

Control-bypass postmortem 2026-08-11 (kanban t_d787b0f8): this job previously
ran the canonical mutation script with no args, so dry_run defaulted to False
and it posted 15 REAL comments on sycode-trading tasks (task_comments
44838-44853, created ~1786439618-1786439633) and grew the state file 444 -> 563
actions. Enforcement is NOT approved (guardian verdict t_71d3e221 = KEEP
DRY-RUN ONLY); classifier hardening lives in t_f23d7ff9; decision gate
t_e2380003. This wrapper ALWAYS execs the canonical with --dry-run --boards all
so the cron emits WOULD-ACT/WOULD-RESOLVE findings only and never mutates a
board or the state file. To flip to enforcement, update this shim via the
owning kanban task with guardian + CEO approvals — never by file replacement.
"""
import os
import sys

CANONICAL = "/home/frank/.hermes/scripts/kanban_dedupe_guard.py"


def main() -> int:
    env = dict(os.environ)
    # Deterministic canonical paths regardless of cron HOME scoping: the
    # canonical script resolves HERMES_ROOT to ~/.hermes by default, so pin the
    # same absolute root to guarantee identical boards/state-file paths.
    env["HERMES_ROOT"] = "/home/frank/.hermes"
    os.execvpe(
        sys.executable,
        [sys.executable, CANONICAL, "--dry-run", "--boards", "all"],
        env,
    )
    return 1  # unreachable unless exec fails


if __name__ == "__main__":
    sys.exit(main())
