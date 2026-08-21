#!/usr/bin/env python3
"""cron-store disabled-state post-ticker watchdog (t_fcb6141f, AC5).

Runs the canonical verifier's --assert-disabled sweep against every live
cron store and exits non-zero on ANY divergence. Intended to be scheduled
every 15m as a no_agent cron job: silent (empty stdout, exit 0) when every
paused job is still durably paused; prints + exits 1 on a regression, which
the cron scheduler delivers as an alert.

A reviewed scheduler disable is only GREEN once a ticker cycle has passed
and this sweep still reports the job paused with no new run after the pause.
"""
import subprocess
import sys
from pathlib import Path

VERIFIER = "/home/frank/.hermes/scripts/cron_store_mutation_verifier.py"
STORE_GLOB = "/home/frank/.hermes/profiles/*/cron/jobs.json"


def main() -> int:
    stores = sorted(Path("/home/frank/.hermes/profiles").glob("*/cron/jobs.json"))
    if not stores:
        print("WARN: no live cron stores found under profiles/", file=sys.stderr)
        return 0
    any_regression = False
    for store in stores:
        r = subprocess.run(
            [sys.executable, VERIFIER, "--assert-disabled", "--jobs-file", str(store)],
            capture_output=True, text=True,
        )
        out = r.stdout.strip()
        if out:
            print(out)  # verifier prints on divergence; healthy is silent
        if r.returncode != 0:
            any_regression = True
    return 1 if any_regression else 0


if __name__ == "__main__":
    sys.exit(main())
