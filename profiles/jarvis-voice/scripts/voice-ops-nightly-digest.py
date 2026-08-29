#!/usr/bin/env python3
"""Nightly Jarvis Voice Ops digest (GAP-4).

Self-contained cron wrapper: runs the voice_edge.digest module from whichever
jarvis-talk worktree is deployed, writing the JSON report and a one-line human
summary. Fails open (never raises past the cron tick).

Used by the jarvis-voice cron job 'voice-ops-nightly-digest'.
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# The deployed bridge worktree (contains voice_edge). Prefer the live deployed
# path; fall back to the newest voice-ops worktree.
CANDIDATES = [
    "/home/frank/jarvis-worktrees/realtime-voice-edge",
    "/home/frank/jarvis-worktrees/voice-ops-t7406af99",
]
REPO = next((c for c in CANDIDATES if Path(c, "voice_edge", "digest.py").is_file()), None)

OUT_DIR = Path(os.environ.get(
    "JARVIS_VOICE_DIGEST_DIR", "/home/frank/.hermes/voice/digests"
))


def main() -> int:
    if REPO is None:
        print("voice-ops digest: no deployed jarvis-talk worktree found; skipping")
        return 0
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = OUT_DIR / f"digest-{stamp}.json"
    latest = OUT_DIR / "latest.json"

    proc = subprocess.run(
        [sys.executable, "-m", "voice_edge.digest", "--days", "1", "--json"],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        print(f"voice-ops digest failed rc={proc.returncode}: {proc.stderr[-500:]}")
        return 0  # fail open
    try:
        data = json.loads(proc.stdout)
    except Exception as exc:
        print(f"voice-ops digest output unparseable: {exc}")
        return 0
    out_path.write_text(proc.stdout)
    latest.write_text(proc.stdout)

    lat = data.get("latency_ms", {})
    v = data.get("vaqi", {})
    print(
        f"voice-ops digest {stamp}: calls={data.get('calls')} turns={data.get('turns')} "
        f"VAQI_avg={v.get('vaqi_avg')} TTFA_p50={lat.get('ttfa', {}).get('p50')}ms "
        f"done_p95={lat.get('done', {}).get('p95')}ms cost=${data.get('est_cost_usd')} "
        f"-> {out_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
