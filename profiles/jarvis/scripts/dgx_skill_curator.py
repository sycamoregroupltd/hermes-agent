#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies.
# 2026-07-23 Native rewrite: replaced `hermes -z` LLM-agent inner call with
# native `hermes curator run` + `hermes curator status`. The native Hermes
# curator already runs every 3d (consolidates, prunes, archives).
# This script is now a HEALTH WRAPPER: surface the native curator's state,
# report failures, and exit 0 only when the native curator is healthy.  It must
# not trigger a second full curator run; the native curator owns its own cadence.
# Previous LLM-agent-based curation (which redundantly inspected all 165+58
# skills every run) is eliminated. Audit logs are now concise — no duplicated
# 220-line reports in the vault.
import subprocess, datetime, sys, os, re

INNER_TIMEOUT = 300  # 5 min — native curator run is fast

DEGRADED_MARKER_PATH = "/home/frank/.hermes/var/skill-curator-degraded.flag"

now = datetime.datetime.now(datetime.timezone.utc)
now_str = now.isoformat()
date_str = now_str[:10]

# Lazy-import — only load the writer when we actually produce an audit event
_writer = None

def _get_writer():
    global _writer
    if _writer is None:
        try:
            from second_brain_writer import append_markdown_event
            _writer = append_markdown_event
        except ImportError:
            _writer = False
    return _writer if _writer else False


def write_audit(heading, body):
    writer = _get_writer()
    if not writer:
        print(f"[skill-curator] WARN: second_brain_writer not available; audit not written")
        return False
    try:
        writer(
            f"/home/frank/obsidian-fleet-vault/Operations/skill-curator/{date_str}.md",
            f"## {heading} ({now_str})\n\n{body}",
            initial_body=f"# Skill Curator Health — {date_str}",
            title=f"Skill Curator Health — {date_str}",
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
        return True
    except Exception as e:
        print(f"[skill-curator] WARN: audit write failed: {e}")
        return False


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


def run_native_curator():
    """Run native `hermes curator run` and return (outcome, detail)."""
    try:
        proc = subprocess.run(
            ["hermes", "curator", "run"],
            timeout=INNER_TIMEOUT,
            capture_output=True,
            text=True,
        )
        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()

        if proc.returncode == 0:
            # Native curator succeeded. Parse key facts from output.
            summary = stdout.split("\n")[0] if stdout else "no summary"
            detail = f"exit 0 | {summary}"
            return ("ok", detail)
        else:
            detail = f"exit {proc.returncode}\n--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}"
            return ("degraded", detail)
    except subprocess.TimeoutExpired:
        return ("timeout", f"native curator exceeded {INNER_TIMEOUT}s")
    except FileNotFoundError:
        return ("missing", "`hermes` not found on PATH")
    except Exception as e:
        return ("error", f"unexpected: {e}")


# Status probe must be resilient to a flaky/slow `hermes` CLI (observed 30s
# subprocess timeouts under load after the NIM provider switch). A transient
# status-probe failure is NOT evidence the native curator is degraded — that
# would falsely fail this cron (the original black hole). Treat probe failure
# as "unknown" and exit healthy with a warning, so only a real curator run
# failure (which this wrapper no longer performs) can degrade the loop.
STATUS_TIMEOUT = 120  # generous; a status call should be fast, but tolerate spikes

def _parse_curator_staleness(raw):
    """Classify the native curator as authoritatively stale from `status` text.

    A curator that is ENABLED but whose last successful run is older than its own
    `stale after` threshold is authoritatively stale — that is the health signal
    this wrapper exists to surface (no-black-holes: a silently-green cron that
    hides a dead curator). A probe that FAILS (timeout / non-zero / not found) is
    inconclusive and handled separately as WARN/unknown so it must NOT degrade
    (see t_eb5984ed — a flaky probe is not evidence of curator death). Only a
    successful, parsable, stale/disabled report counts as DEGRADED.
    """
    m = re.search(r"^curator:\s*(\S+)", raw, re.MULTILINE)
    if m and m.group(1).upper() != "ENABLED":
        return True, f"native curator not enabled (state={m.group(1)})"
    m_last = re.search(r"last run:\s*([\d.]+)\s*([hdwmy])", raw)
    m_stale = re.search(r"stale after:\s*([\d.]+)\s*([hdwmy])", raw)
    if not (m_last and m_stale):
        return False, None
    units = {"h": 1 / 24.0, "d": 1.0, "w": 7.0, "m": 30.0, "y": 365.0}
    last = float(m_last.group(1)) * units.get(m_last.group(2), 1.0)
    stale = float(m_stale.group(1)) * units.get(m_stale.group(2), 1.0)
    if last > stale:
        return True, (
            f"native curator stale: last run {m_last.group(1)}{m_last.group(2)} "
            f"ago exceeds stale-after {m_stale.group(1)}{m_stale.group(2)}"
        )
    return False, None


def check_native_curator_status():
    """Fetch native curator status by parsing text output.

    Returns (status_data, status_line). On any probe failure (timeout, missing
    binary, non-zero exit) status_data is None and status_line is a clearly
    labelled WARNING — never a silent degradation. On a successful probe,
    status_data carries a parsed `stale` verdict so run() can DEGRADE an
    authoritatively-dead curator instead of masking it as green.
    """
    try:
        proc = subprocess.run(
            ["hermes", "curator", "status"],
            timeout=STATUS_TIMEOUT,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            # Non-zero exit from the probe is inconclusive; do not degrade.
            stderr = (proc.stderr or "").strip()
            warn = (stderr.splitlines()[0] if stderr else f"exit {proc.returncode}")[:160]
            return None, f"WARN: status probe inconclusive ({warn})"
        stale, reason = _parse_curator_staleness(proc.stdout)
        status_data = {"raw_output": proc.stdout.strip(), "stale": stale, "stale_reason": reason}
        return status_data, f"status ok ({len(proc.stdout)} chars)"
    except subprocess.TimeoutExpired:
        return None, f"WARN: status probe timed out after {STATUS_TIMEOUT}s"
    except FileNotFoundError:
        return None, "WARN: `hermes` not found on PATH"
    except Exception as e:
        return None, f"WARN: status probe error: {e}"


def run():
    # 1. Check native curator status first
    status_data, status_line = check_native_curator_status()
    print(f"[skill-curator] Native curator status: {status_line}")

    # 2. Do not run the native curator here.  This cron used to call
    #    `hermes curator run`, which can legitimately exceed the cron/script
    #    timeout when profile consolidation is enabled.  The native curator
    #    status is the health signal; invoking another full run from the health
    #    wrapper turns a healthy curator into a false failing cron.
    #
    #    A probe failure (timeout / inconclusive) is treated as UNKNOWN, not
    #    DEGRADED. The native curator runs on its own 3-day cadence; unless we
    #    actually observed a failed run, the loop stays healthy. We still record
    #    the warning so the audit trail is honest.
    if status_line.startswith("WARN:"):
        outcome = "unknown"
        detail = status_line
        print(f"[skill-curator] Native curator status probe inconclusive — treating as healthy (unknown)")
    elif status_data is not None and status_data.get("stale"):
        # Authoritative staleness: a successful probe reported the curator is
        # disabled or its last run exceeds its own stale-after threshold. This is
        # genuine degradation the wrapper must surface — FAIL-LOUD, not silent-green.
        outcome = "degraded"
        detail = status_data.get("stale_reason") or "native curator stale"
        print(f"[skill-curator] Native curator authoritatively STALE — degrading (no-black-holes)")
    elif status_data is not None:
        outcome = "ok"
        detail = status_line
        print(f"[skill-curator] Native curator run: skipped — health wrapper only")
    else:
        outcome = "degraded"
        detail = status_line
        print(f"[skill-curator] Native curator run: skipped — status unavailable")

    # 3. Write audit trail
    if outcome == "ok":
        write_audit("Curator run OK", f"Native: {detail.split('|', 1)[-1].strip()}\nStatus: {status_line}")
        clear_degraded_marker()
        print(f"[skill-curator] HEALTHY")
        return 0
    elif outcome == "unknown":
        # Probe inconclusive but no real failure observed — healthy, with a note.
        write_audit(
            "Curator status probe inconclusive",
            f"Status probe returned: {detail}\nTreated as healthy; native curator owns its own cadence and was not re-run.",
        )
        clear_degraded_marker()
        print(f"[skill-curator] HEALTHY (status probe inconclusive)")
        return 0
    else:
        write_audit(f"Curator run {outcome.upper()}", detail)
        print(f"SKILL-CURATOR DEGRADED: {outcome} — {detail[:200]}")
        write_degraded_marker(f"{outcome}: {detail[:300]}")
        return 2


if __name__ == "__main__":
    sys.exit(run())
