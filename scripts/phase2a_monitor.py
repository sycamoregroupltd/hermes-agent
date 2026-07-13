#!/usr/bin/env python3
"""
Phase 2a — Continuous Improvement Monitor
Runs every 4 hours. Checks training data volume. If enough new 28-feature samples 
have accumulated since last retrain, triggers v12 build.

State file: /tmp/composite_scorer_improvement_state.json
"""

import json, os, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path

STATE_PATH = Path("/tmp/composite_scorer_improvement_state.json")

def get_training_log_count():
    """Get line count of the training log inside the server container."""
    r = subprocess.run(
        ["docker", "exec", "sycodetrading-server", "sh", "-c",
         "wc -l /app/data/composite_scorer_training.jsonl 2>/dev/null || echo 0"],
        capture_output=True, text=True, timeout=30
    )
    try:
        return int(r.stdout.strip().split()[0])
    except (ValueError, IndexError):
        return 0

def get_post_deploy_samples(count, deployed_at):
    """Estimate how many samples have been collected since deploy."""
    # Simplified: just check if count > previous count
    return count  # Placeholder

def main():
    state = {}
    if STATE_PATH.exists():
        try:
            state = json.loads(STATE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            state = {}
    
    last_count = state.get("last_count", 0)
    last_retrain = state.get("last_retrain", None)
    current_count = get_training_log_count()
    new_samples = current_count - last_count
    
    print(f"[{datetime.now(timezone.utc).isoformat()}]")
    print(f"  Training log: {current_count} samples ({new_samples} new since last check)")
    print(f"  Last retrain: {last_retrain or 'never'}")
    
    state["last_count"] = current_count
    state["last_check"] = datetime.now(timezone.utc).isoformat()
    STATE_PATH.write_text(json.dumps(state, indent=2))
    
    # Threshold: need 5,000 new 28-feature samples for a meaningful retrain
    # Since server was deployed at ~21:32 UTC, most new samples will have 28 features
    if new_samples >= 5000 and current_count > 164648 + 5000:
        print(f"\n  ⚡ {new_samples} new samples — triggering v12 retrain...")
        # Build macro-enriched dataset
        r = subprocess.run(
            ["python3", "/home/frank/sycode-trading/tools/composite-scorer/build_training_v11.py",
             "--output", "/tmp/composite_scorer_v12_raw.jsonl"],
            capture_output=True, text=True, timeout=600
        )
        if r.returncode == 0:
            print(f"  ✓ Dataset built. Training v12...")
            r2 = subprocess.run(
                ["python3", "/home/frank/sycode-trading/tools/composite-scorer/train_composite.py",
                 "--input", "/tmp/composite_scorer_v12_raw.jsonl",
                 "--output", "/home/frank/sycode-trading/server/models/composite_scorer_v12.onnx",
                 "--min-auc", "0.50"],
                capture_output=True, text=True, timeout=900
            )
            if r2.returncode == 0:
                print(f"  ✓ v12 trained! AUC in output above.")
                # Copy to container
                subprocess.run(
                    ["docker", "cp",
                     "/home/frank/sycode-trading/server/models/composite_scorer_v12.onnx",
                     "sycodetrading-server:/app/server/models/composite_scorer_v12.onnx"],
                    capture_output=True, timeout=30
                )
                subprocess.run(
                    ["docker", "cp",
                     "/home/frank/sycode-trading/server/models/composite_scorer_v12.json",
                     "sycodetrading-server:/app/server/models/composite_scorer_v12.json"],
                    capture_output=True, timeout=30
                )
                # Update .env to point to v12
                subprocess.run(
                    ["sed", "-i",
                     "s|COMPOSITE_SCORER_ONNX_PATH=/app/server/models/composite_scorer_v[0-9]*\\.onnx|COMPOSITE_SCORER_ONNX_PATH=/app/server/models/composite_scorer_v12.onnx|",
                     "/home/frank/sycode-trading/server/.env"],
                    capture_output=True, timeout=10
                )
                state["last_retrain"] = datetime.now(timezone.utc).isoformat()
                state["last_retrain_version"] = "v12"
                STATE_PATH.write_text(json.dumps(state, indent=2))
                print(f"  ✓ v12 deployed! Restart server: docker compose --profile prod up -d server")
            else:
                print(f"  ✗ v12 training failed:\n{r2.stderr[:500]}")
        else:
            print(f"  ✗ Dataset build failed:\n{r.stderr[:500]}")
    else:
        remaining = max(0, 5000 - new_samples)
        print(f"  Need {remaining} more samples for next retrain")

if __name__ == "__main__":
    main()
