#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
# 2026-07-05 claude-seat audit fix: removed dead xai-oauth/grok-4.3 pin (revoked 2026-06-25;
# profile default + fallback chain now applies), fixed literal {{now[:10]}} log path bug,
# and propagate the agent exit code so cron stops reporting silent failures as ok.
# 2026-07-09 devops fix (t_6554215b): the inner `hermes -z` skill-curator pass routinely exceeds the
# previous 600s inner timeout, raising an uncaught subprocess.TimeoutExpired that crashed the cron
# with exit 1. Now every subprocess failure (timeout, missing interpreter, non-zero agent exit) is
# handled defensively: a dated audit note is appended to the vault and a concise summary is printed to
# stdout, and the script exits 0 so the watchdog stays green (no crash alert) while the evidence remains.
# 2026-07-10 observability hardening (t_752c3b80): the t_6554215b fix traded a crash alert for a
# SILENT-GREEN blind spot — even a perpetually-stuck or no-op inner agent reported "ok" (exit 0).
# Now the wrapper CLASSES the outcome (InnerTimeout / InnerNonZero / InnerNoAudit / Success) and exits
# NON-ZERO with an explicit "SKILL-CURATOR DEGRADED: <reason>" line + a DEGRADED marker file the
# watchdog can see, so silent degradation is surfaced instead of masked. A genuine success still
# writes the dated audit note and exits 0. The prior crash-class bug (uncaught TimeoutExpired) is
# preserved as caught — we only change post-failure REPORTING/EXIT semantics, never re-raise.
import subprocess, datetime, sys, os, traceback

from second_brain_writer import append_markdown_event

# 50 min — comfortably under the scheduler's 1h script-timeout window, so a legitimate
# long curator pass can finish instead of being killed at 10 min.
INNER_TIMEOUT = 3000

# Distinct marker the watchdog / cron report can grep for to distinguish DEGRADED from green.
DEGRADED_MARKER_PATH = "/home/frank/.hermes/var/skill-curator-degraded.flag"

now = datetime.datetime.now(datetime.timezone.utc)
now_str = now.isoformat()
date_str = now_str[:10]
log_path = f"/home/frank/obsidian-fleet-vault/Operations/skill-curator/{date_str}.md"


def write_audit(heading, body):
    # Best-effort audit trail. A failure here must NEVER crash the cron run itself.
    try:
        append_markdown_event(
            log_path,
            f"## {heading} ({now_str})\n\n{body}",
            initial_body=f"# Skill Curator Audit — {date_str}",
            title=f"Skill Curator Audit — {date_str}",
            type="task-evidence",
            status="active",
            created=date_str,
            updated=date_str,
            confidence="high",
            tags=["hermes", "skills", "curator", "operations"],
            sources=["/home/frank/.hermes/skills"],
            project="control-plane",
            owners=["jarvis"],
            knowledge_tier="evidence",
            generated=True,
            generator="dgx_skill_curator.py",
        )
    except Exception as e:
        print(f"[skill-curator] WARN: could not write audit note: {e}")


def clear_degraded_marker():
    try:
        if os.path.exists(DEGRADED_MARKER_PATH):
            os.remove(DEGRADED_MARKER_PATH)
    except Exception:
        pass


def write_degraded_marker(reason):
    try:
        os.makedirs(os.path.dirname(DEGRADED_MARKER_PATH), exist_ok=True)
        with open(DEGRADED_MARKER_PATH, "w") as f:
            f.write(f"{now_str} DEGRADED: {reason}\n")
    except Exception:
        pass


def classify(proc):
    """Return (outcome, reason) classifying the inner subprocess result.

    outcome is one of: Success | InnerNonZero | InnerNoAudit
    (Timeout / NotFound are raised as exceptions by subprocess and handled in run())."""
    if proc.returncode == 0:
        summary = (proc.stdout or "").strip()
        # A genuine success must actually print a real summary (the PROMPT mandates
        # the inner agent print its findings to stdout). If it exited 0 but produced
        # no stdout, it did no real work — treat as a no-op. Detect on EMPTY STDOUT
        # ONLY; a log_path proxy would be wrong here because the InnerNoAudit branch
        # below itself writes log_path, which would mask every repeat same-day no-op
        # run as green (the t_752c3b80 reviewer-identified blind spot).
        if not summary:
            return "InnerNoAudit", "inner agent exited 0 but produced no audit output (no-op)"
        return "Success", ""
    return "InnerNonZero", f"inner agent exited non-zero (returncode={proc.returncode})"


PROMPT = (
    "Skill curator pass. Review skills/ for duplicates, stale references, and missing "
    "umbrella consolidation. Make minimal safe changes. Write a dated markdown summary of "
    f"findings and changes to {log_path} (create parent dirs if needed), and print the same "
    "summary to stdout so the cron report carries it."
)


def run():
    try:
        proc = subprocess.run(
            ["hermes", "-p", "jarvis", "-z", PROMPT],
            timeout=INNER_TIMEOUT,
            capture_output=True,
            text=True,
        )
        outcome, reason = classify(proc)
        if outcome == "Success":
            summary = (proc.stdout or "").strip() or "(no stdout; see vault note)"
            write_audit("Skill curator pass OK", summary)
            print(summary)
            clear_degraded_marker()
            return 0
        if outcome == "InnerNoAudit":
            detail = (
                f"returncode={proc.returncode}\n"
                f"--- stdout ---\n{proc.stdout or ''}\n"
                f"--- stderr ---\n{proc.stderr or ''}"
            )
            write_audit("Skill curator pass NO AUDIT (no-op)", detail)
            print(f"SKILL-CURATOR DEGRADED: {reason}; detail recorded to {log_path}")
            write_degraded_marker(reason)
            return 2
        # InnerNonZero
        detail = (
            f"returncode={proc.returncode}\n"
            f"--- stdout ---\n{proc.stdout or ''}\n"
            f"--- stderr ---\n{proc.stderr or ''}"
        )
        write_audit("Skill curator pass returned non-zero", detail)
        print(f"SKILL-CURATOR DEGRADED: {reason}; detail recorded to {log_path}")
        write_degraded_marker(reason)
        return 2
    except subprocess.TimeoutExpired as e:
        write_audit(
            "Skill curator pass TIMEOUT",
            f"Inner `hermes -z` pass exceeded {INNER_TIMEOUT}s budget and was skipped this tick.\n"
            f"{str(e)[:500]}",
        )
        print(f"SKILL-CURATOR DEGRADED: inner timeout after {INNER_TIMEOUT}s (no-op this tick); recorded to {log_path}")
        write_degraded_marker(f"inner timeout after {INNER_TIMEOUT}s")
        return 2
    except FileNotFoundError as e:
        write_audit("Skill curator pass SKIPPED", f"`hermes` not found on PATH in cron env: {e}")
        print(f"SKILL-CURATOR DEGRADED: `hermes` not on PATH in cron env; recorded to {log_path}")
        write_degraded_marker("hermes not on PATH")
        return 2
    except Exception as e:
        write_audit("Skill curator pass ERROR", f"Unexpected error: {e}\n{traceback.format_exc()}")
        print(f"SKILL-CURATOR DEGRADED: unexpected error — {e}; recorded to {log_path}")
        write_degraded_marker(f"unexpected error: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(run())
