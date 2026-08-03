#!/usr/bin/env python3
"""DEPRECATED: DIAR (Data Integrity Active Remediator) — superseded by DQSH.

This script was a never-implemented 66-line stub (t_71f7c2f1 falsely claimed
completion). The fleet's actual self-healing daemon is:

    /home/frank/.hermes/scripts/dqsh_daemon.py   (via run_dqsh.sh)

This shim exists only to disambiguate for future reviewers and to redirect
any stray invocation to DQSH in paper-mode. See platform-reviewer verdict:
obsidian-fleet-vault/Governance/2026-07-29-platform-reviewer-diar-code-review-t_174d30e5.md
(t_174d30e5, FAIL 4) and rework packet t_7c75ea77.
"""
import sys

DQSH_PATH = "/home/frank/.hermes/scripts/dqsh_daemon.py"


def main():
    print(
        "[DIAR-DEPRECATED] data_integrity_remediator.py is a retired stub. "
        f"Use the DQSH daemon instead: python3 {DQSH_PATH} --run (paper-mode).",
        file=sys.stderr,
    )
    # Do NOT auto-exec DQSH: avoid accidental double-scheduling alongside the
    # registered DQSH crons. Fail loudly instead.
    sys.exit(2)


if __name__ == "__main__":
    main()
