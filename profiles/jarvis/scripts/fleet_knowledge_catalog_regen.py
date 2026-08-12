#!/usr/bin/env python3
"""Governed fleet knowledge-catalog regeneration cron (canonical implementation).

Closes the structural gap that the canonical fleet generator
``generate_knowledge_catalogs.py`` had no recurring mechanism: it only
regenerated when an analyst ran it by hand, so ``--check`` drifted stale.

This script is the sole implementation behind the jarvis no_agent cron
``fleet-knowledge-catalog-regen``. It:
  1. Runs the canonical generator, which writes through the governed atomic
     ``second_brain_writer.write_text_atomic`` into the fleet vault.
  2. Re-runs the generator with ``--check`` to confirm the live catalog tree
     is byte-exact against the current profiles/skills/quarantine state.
  3. Stays silent when clean (watchdog pattern).
  4. On any failure, prints a concise failure/debt alert to stdout (delivered
     verbatim to the gateway-connected sink ``discord:#fleet-reports``) and
     appends a durable run record to a log the second-brain health loop tails.

Edit this canonical file, not the profile-local shim.
"""
from __future__ import annotations

import datetime as dt
import subprocess
import sys
from pathlib import Path

GENERATOR = Path("/home/frank/obsidian-fleet-vault/System/Scripts/generate_knowledge_catalogs.py")
PROFILES_DIR = Path("/home/frank/.hermes/profiles")
SKILLS_DIR = Path("/home/frank/.hermes/skills")
QUARANTINED_PROFILES = Path("/home/frank/.hermes/quarantined-profiles")
QUARANTINED_SKILLS = Path("/home/frank/.hermes/quarantined-skills")
OUTPUT = Path("/home/frank/obsidian-fleet-vault")
REGISTRY = Path("/home/frank/obsidian-fleet-vault/Projects/Portfolio/registry.yaml")
LOG = Path("/home/frank/.hermes/var/fleet-knowledge-catalog-regen.log")
PY = sys.executable or "python3"


def run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run([PY, *map(str, args)], capture_output=True, text=True, timeout=300)


def parse_counts(stdout: str) -> str:
    for line in stdout.splitlines():
        if line.startswith(("generated", "catalogs current")):
            return line.strip()
    return ""


def main() -> int:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now(dt.timezone.utc).isoformat()
    base = [
        str(GENERATOR),
        "--profiles-dir", str(PROFILES_DIR),
        "--skills-dir", str(SKILLS_DIR),
        "--quarantined-profiles-dir", str(QUARANTINED_PROFILES),
        "--quarantined-skills-dir", str(QUARANTINED_SKILLS),
        "--output", str(OUTPUT),
        "--registry", str(REGISTRY),
    ]
    regen = run(base)
    check = run([*base, "--check"])
    counts = parse_counts(regen.stdout) or parse_counts(check.stdout)
    ok = regen.returncode == 0 and check.returncode == 0
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(
            f"{ts} ok={ok} regen_rc={regen.returncode} check_rc={check.returncode} {counts}\n"
        )
    if ok:
        return 0
    bits: list[str] = []
    if regen.returncode != 0:
        last = regen.stderr.strip().splitlines()[-1] if regen.stderr.strip() else "no stderr"
        bits.append(f"regen FAILED rc={regen.returncode}: {last}")
    if check.returncode != 0:
        bits.append("catalog --check DRIFT: generated tree out of sync with live profiles/skills/quarantine")
    print(
        f"[fleet-knowledge-catalog-regen] FAIL @ {ts} | {' | '.join(bits)} "
        f"| counts={counts or 'n/a'} | log={LOG}"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
