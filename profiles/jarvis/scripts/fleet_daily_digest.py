#!/usr/bin/env python3
"""fleet_daily_digest.py — one-message daily fleet health digest (KNOW-16, t_9fa78c30).

WHY THIS EXISTS: the estate reliability review (2026-08-12, round 1 + round 2) surfaced
27 crons in error, a 44-day backup gap, and a growing blocked-task backlog — none of
which were visible anywhere Frank actually reads. Every one of those findings would
have been caught by this digest on day one. It is a no-agent (script-only) cron:
deterministic, no LLM, cheap, and it runs even if every LLM provider is down.

ASSERTS (per KNOW-16 acceptance):
  1. Crons in error, grouped by cause, EXCLUDING known by-design signallers
     (see BY_DESIGN_JOBS below — jobs whose non-zero exit IS their alert).
  2. Nightly backup: cron exit status AND off-box artifact presence/freshness.
  3. Blocked-task count per board, with delta vs the previous run (state file).
  4. Disk headroom (root fs) and swap usage.
  5. Per-vault (fleet vault, sycode-trading vault, sycode-trading code repo,
     .hermes live tree): commits ahead of upstream, dirty file count, last commit time.

DESIGN: no-agent watchdog convention inverted deliberately — this job is NOT silent
on "clean" because a digest with nothing to say is itself information (things are
healthy); it always sends once daily. It runs standalone as a delivery-direct cron
(hermes send -t telegram), not through report-to-board.py, because Frank asked for
"one message to Telegram each morning" specifically, not a board card.

Exit code: 0 always (delivery failures are logged to stderr but do not redden the
cron — a digest that fails to format should not also fail to run tomorrow).
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

STATE_PATH = Path("/home/frank/.hermes/profiles/jarvis/state/fleet_daily_digest_state.json")
BOARD_ROOT = Path("/home/frank/.hermes/kanban/boards")
BOARDS = ["jarvis-os", "sycode-trading", "upero", "yorkstone-supplies", "ai-restaurant", "ecohome"]
CRON_STORE = Path("/home/frank/.hermes/profiles/jarvis/cron/jobs.json")
BACKUP_LATEST = Path("/home/frank/fleet-backups/LATEST")
BACKUP_MAX_AGE_H = 36
VAULTS = {
    "fleet-vault": "/home/frank/obsidian-fleet-vault",
    "sycode-vault": "/home/frank/obsidian/sycode-trading",
    "sycode-trading-repo": "/home/frank/sycode-trading",
    "hermes-live-tree": "/home/frank/.hermes",
}

# Jobs whose non-zero exit is an INTENTIONAL alert, not a defect (exit-code liveness
# doctrine, per nous_token_presence.sh / KNOW-16 methodology). Keyed by cron job name
# as it appears in `hermes cron list`. Digest EXCLUDES these from the "real error"
# grouping but still counts them in a separate by-design tally so the signal is not
# silently dropped either.
BY_DESIGN_JOBS = {
    "nous-token-presence",
    "primary-provider-liveness",  # absorbed alias of nous_token_presence.sh (t_db689c47)
    "guard-bundle-tick-5m",
    "guard-bundle-tick-15m",
    "guard-bundle-tick-hourly",
    "guard-bundle-tick-daily",
    # dgx-fleet-chain-validator / data-freshness-probe / channel-liveness-oob /
    # profile-toolset-obligation-audit / skill-cli-drift-guard / kanban-gc-hygiene-bundle
    # etc. are now absorbed into the guard-bundle-tick-* jobs (t_db689c47 CONDENSE 1/4);
    # their by-design semantics are preserved inside cron_guard_bundle_runner.py's
    # per-check reporting, not as standalone cron entries anymore.
    "automation-vc-keeper",  # secret-scan pre-commit hit = it did its job correctly
}

# Already-carded-elsewhere jobs the digest should not duplicate-report on (KNOW-03 etc.)
KNOWN_CARDED_ELSEWHERE = {"llm-wiki-health-check"}


def sh(cmd: list[str], cwd: str | None = None, timeout: int = 30) -> str:
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return ((r.stdout or "") + (r.stderr or "")).strip()
    except Exception as e:  # noqa: BLE001
        return f"ERR({type(e).__name__}:{e})"


def load_jobs() -> list[dict]:
    try:
        d = json.loads(CRON_STORE.read_text())
    except Exception:
        return []
    jobs = d.get("jobs", d) if isinstance(d, dict) else d
    if isinstance(jobs, dict):
        jobs = list(jobs.values())
    return jobs


def section_quota_headroom() -> str:
    """Expose provider quota position without secrets or fabricated numeric limits."""
    lines = ["PROVIDER QUOTA HEADROOM (sanitized metadata; numeric remaining quota unavailable):"]
    try:
        data = json.loads(Path("/home/frank/.hermes/profiles/jarvis/auth.json").read_text())
        pool = data.get("credential_pool", {})
        for provider in ("openai-codex", "nous", "nvidia", "xai-oauth", "anthropic"):
            entries = pool.get(provider, [])
            if isinstance(entries, dict):
                entries = [entries]
            if not entries:
                lines.append(f"  - {provider}: unavailable; numeric_remaining=unknown")
                continue
            statuses = sorted({str(e.get("last_status") or "unknown") for e in entries})
            resets = sorted({str(e.get("last_error_reset_at")) for e in entries
                             if e.get("last_error_reset_at")})
            expiries = sorted({str(e.get("expires_at") or e.get("expires_at_ms"))
                               for e in entries if e.get("expires_at") or e.get("expires_at_ms")})
            window = (f"reset={resets[0]}" if resets else
                      (f"expiry={expiries[0]}" if expiries else "reset/expiry=unknown"))
            lines.append(f"  - {provider}: status={','.join(statuses)}; {window}; "
                         f"credentials={len(entries)}; numeric_remaining=not_exposed")
    except Exception as exc:
        lines.append(f"  - unavailable: {type(exc).__name__}; numeric_remaining=unknown")
    return "\n".join(lines)


def section_cron_errors() -> tuple[str, dict]:
    jobs = load_jobs()
    real_errors: list[str] = []
    by_design_errors: list[str] = []
    carded_elsewhere: list[str] = []
    for j in jobs:
        if not j.get("enabled"):
            continue
        if j.get("last_status") != "error":
            continue
        name = str(j.get("name") or j.get("id"))
        base_name = name.split(" (")[0].strip()
        if base_name in KNOWN_CARDED_ELSEWHERE:
            carded_elsewhere.append(name)
            continue
        if base_name in BY_DESIGN_JOBS:
            by_design_errors.append(name)
            continue
        err = (j.get("last_error") or "").splitlines()[0][:140]
        real_errors.append(f"{name}: {err}")
    lines = []
    if real_errors:
        lines.append(f"CRONS IN ERROR ({len(real_errors)}, excluding by-design):")
        for e in sorted(real_errors):
            lines.append(f"  - {e}")
    else:
        lines.append("CRONS IN ERROR: none (excluding by-design signallers)")
    if by_design_errors:
        lines.append(f"  (by-design alert state, not a defect: {', '.join(sorted(by_design_errors))})")
    if carded_elsewhere:
        lines.append(f"  (tracked on separate cards, not duplicated here: {', '.join(sorted(carded_elsewhere))})")
    return "\n".join(lines), {
        "real_error_count": len(real_errors),
        "by_design_count": len(by_design_errors),
    }


def section_backup() -> str:
    if not BACKUP_LATEST.exists():
        return "BACKUP: MISSING — no off-box backup artifact has ever completed (LATEST pointer absent)."
    age_h = (time.time() - BACKUP_LATEST.stat().st_mtime) / 3600.0
    stamp = BACKUP_LATEST.read_text(errors="ignore")[:40].strip()
    fresh = "FRESH" if age_h <= BACKUP_MAX_AGE_H else "STALE"
    # cron exit status for the nightly job itself
    cron_status = "unknown"
    for j in load_jobs():
        if j.get("name") == "nightly-fleet-backup":
            cron_status = j.get("last_status", "unknown")
            break
    off_box = "unknown (mac unreachable or check skipped)"
    r = sh(["ssh", "-4", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "mac",
            f"test -d dgx-fleet-backups/{stamp} && echo PRESENT || echo ABSENT"], timeout=15)
    if r in ("PRESENT", "ABSENT"):
        off_box = r
    return (
        f"BACKUP: artifact {fresh} ({age_h:.1f}h old, stamp={stamp}); "
        f"cron_last_status={cron_status}; off_box_presence={off_box}"
    )


def section_blocked(prev_state: dict) -> tuple[str, dict]:
    lines = ["BLOCKED-TASK COUNT PER BOARD (delta vs previous digest run):"]
    counts = {}
    for b in BOARDS:
        db = BOARD_ROOT / b / "kanban.db"
        if not db.exists():
            continue
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
            row = conn.execute("select count(*) from tasks where status='blocked'").fetchone()
            conn.close()
            n = int(row[0]) if row else 0
        except Exception as e:  # noqa: BLE001
            lines.append(f"  - {b}: ERROR reading db ({e})")
            continue
        counts[b] = n
        prev = prev_state.get("blocked_counts", {}).get(b)
        delta = "" if prev is None else f" ({'+' if n - prev >= 0 else ''}{n - prev} vs last run)"
        lines.append(f"  - {b}: {n}{delta}")
    return "\n".join(lines), counts


def section_disk_swap() -> str:
    du = shutil.disk_usage("/")
    free_gb = du.free / (1024 ** 3)
    total_gb = du.total / (1024 ** 3)
    pct_used = 100.0 * (du.total - du.free) / du.total
    swap_line = "swap unknown"
    free_out = sh(["free", "-m"])
    for ln in free_out.splitlines():
        if ln.lower().startswith("swap"):
            parts = ln.split()
            if len(parts) >= 3:
                total_mb, used_mb = float(parts[1]), float(parts[2])
                pct = (100.0 * used_mb / total_mb) if total_mb else 0.0
                swap_line = f"swap {used_mb:.0f}MiB/{total_mb:.0f}MiB used ({pct:.0f}%)"
    return f"DISK: {free_gb:.0f}GB free / {total_gb:.0f}GB ({pct_used:.0f}% used) on /.  {swap_line}"


def section_vaults() -> str:
    lines = ["PER-VAULT GIT STATUS:"]
    for label, path in VAULTS.items():
        p = Path(path)
        if not p.exists():
            lines.append(f"  - {label}: MISSING ({path})")
            continue
        branch = sh(["git", "-C", path, "rev-parse", "--abbrev-ref", "HEAD"])
        dirty = sh(["git", "-C", path, "status", "--short"])
        dirty_n = len([l for l in dirty.splitlines() if l.strip()]) if dirty and "ERR(" not in dirty else "?"
        ahead = sh(["git", "-C", path, "log", "--oneline", "@{u}.."])
        if ahead.startswith("ERR(") or "fatal" in ahead:
            ahead_n = "no-upstream"
        else:
            ahead_n = len([l for l in ahead.splitlines() if l.strip()])
        last_commit = sh(["git", "-C", path, "log", "-1", "--format=%cI"])
        lines.append(
            f"  - {label} [{branch}]: {ahead_n} ahead of upstream, {dirty_n} dirty files, last commit {last_commit}"
        )
    return "\n".join(lines)


def main() -> int:
    prev_state = {}
    if STATE_PATH.exists():
        try:
            prev_state = json.loads(STATE_PATH.read_text())
        except Exception:
            prev_state = {}

    cron_section, cron_stats = section_cron_errors()
    backup_section = section_backup()
    blocked_section, blocked_counts = section_blocked(prev_state)
    quota_section = section_quota_headroom()
    disk_section = section_disk_swap()
    vault_section = section_vaults()

    stamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    body = (
        f"FLEET DAILY DIGEST — {stamp}\n"
        f"{'=' * 40}\n\n"
        f"{cron_section}\n\n"
        f"{backup_section}\n\n"
        f"{blocked_section}\n\n"
        f"{quota_section}\n\n"
        f"{disk_section}\n\n"
        f"{vault_section}\n"
    )

    injected = os.environ.get("FLEET_DIGEST_INJECT_FAULT")
    if injected:
        body += f"\n[INJECTED TEST FAULT] {injected}\n"

    # Deliver directly to Telegram (Frank's chosen channel for this digest, per
    # KNOW-16 body: "One message to Telegram each morning"). deliver=local at the
    # cron layer; this script owns delivery itself, same pattern as
    # arena-insert-liveness-monitor.sh's direct-Discord delivery.
    target = os.environ.get("FLEET_DIGEST_TARGET", "telegram")
    send_rc = 1
    try:
        r = subprocess.run(["hermes", "send", "-t", target, body], capture_output=True, text=True, timeout=60)
        send_rc = r.returncode
        if send_rc != 0:
            print(f"DELIVERY FAILED rc={send_rc}: {(r.stdout or '') + (r.stderr or '')}".strip())
    except Exception as e:  # noqa: BLE001
        print(f"DELIVERY EXCEPTION: {e}")

    # Always print to stdout too so `hermes cron run` / manual invocation shows the
    # rendered digest even if delivery is unavailable (e.g. no Telegram configured).
    print(body)

    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps({
        "last_run": stamp,
        "blocked_counts": blocked_counts,
        "cron_stats": cron_stats,
        "last_send_rc": send_rc,
    }, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
