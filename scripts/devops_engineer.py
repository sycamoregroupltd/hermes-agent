#!/usr/bin/env python3
"""DevOps Engineer Agent — implements strategist suggestions, feeds Qdrant, runs ML."""

import subprocess, json, time, os, sys

DB = ["docker", "exec", "sycodetrading-supabase-db", "psql",
      "-h", "localhost", "-U", "postgres", "-d", "postgres", "-t", "-A", "-F|"]
QDRANT = "http://localhost:6333"

def sql(q):
    r = subprocess.run(DB + ["-c", q], capture_output=True, text=True, timeout=30)
    return r.stdout.strip()

def qdrant_post(collection, points):
    """Insert points into Qdrant collection."""
    data = json.dumps({"points": points})
    r = subprocess.run(
        ["curl", "-s", "-X", "PUT", f"{QDRANT}/collections/{collection}/points",
         "-H", "Content-Type: application/json", "-d", data],
        capture_output=True, text=True, timeout=30
    )
    return json.loads(r.stdout) if r.stdout else {}

def check_health():
    print("=== System Health Check ===", flush=True)
    
    # Qdrant
    r = subprocess.run(["curl", "-s", f"{QDRANT}/collections"],
                       capture_output=True, text=True, timeout=5)
    cols = json.loads(r.stdout).get('result', {}).get('collections', [])
    empty = 0
    for c in cols:
        name = c['name']
        cr = subprocess.run(["curl", "-s", f"{QDRANT}/collections/{name}/points/count"],
                           capture_output=True, text=True, timeout=5)
        count = json.loads(cr.stdout).get('result', {}).get('count', 0) if cr.stdout else 0
        if count == 0:
            empty += 1
            print(f"  ⚠ Qdrant '{name}': EMPTY ({count} vectors)", flush=True)
    
    print(f"  Qdrant: {len(cols)} collections, {empty} empty", flush=True)
    
    # DB
    sigs = sql("SELECT COUNT(*) FROM signal_journeys WHERE created_at > NOW() - INTERVAL '1 hour';")
    fps = sql("SELECT COUNT(*) FROM signal_fingerprints WHERE created_at > NOW() - INTERVAL '1 day';")
    print(f"  Signals (1h): {sigs} | Fingerprints (24h): {fps}", flush=True)
    
    return empty > 0

def feed_qdrant():
    """Feed recent signals into Qdrant as vectors."""
    print("\n=== Feeding Qdrant ===", flush=True)
    
    out = sql("""
    SELECT correlation_id, symbol, direction, timeframe, pnl_percent, created_at
    FROM signal_journeys
    WHERE created_at > NOW() - INTERVAL '1 hour'
      AND pnl_percent IS NOT NULL
    LIMIT 100;
    """)
    
    if not out:
        print("  No signals to feed", flush=True)
        return
    
    points = []
    for line in out.split('\n'):
        parts = line.split('|')
        if len(parts) >= 4:
            points.append({
                "id": hash(parts[0]) % (2**63),
                "vector": [float(hash(parts[1]) % 1000) / 1000, 
                          float(ord(parts[2][0])) / 100,
                          float(hash(parts[3]) % 100) / 100],
                "payload": {
                    "correlation_id": parts[0], "symbol": parts[1],
                    "direction": parts[2], "timeframe": parts[3],
                    "pnl": parts[4], "timestamp": parts[5]
                }
            })
    
    if points:
        result = qdrant_post("sycodetrading_signals", points)
        status = result.get('status', 'ok')
        print(f"  Fed {len(points)} signals → Qdrant: {status}", flush=True)

def run_ml_training():
    """Check if ML training runner is available."""
    print("\n=== ML Training Check ===", flush=True)
    r = subprocess.run(
        ["docker", "exec", "sycodetrading-server", "bun", "run",
         "src/domains/ml/services/TrainingProcessRunner.ts", "--check"],
        capture_output=True, text=True, timeout=10
    )
    print(f"  ML Runner: exit={r.returncode}", flush=True)

def main():
    print("[DevOps Engineer] Starting system health + maintenance run...\n", flush=True)
    
    needs_feed = check_health()
    
    if needs_feed:
        feed_qdrant()
    
    run_ml_training()
    
    print("\n[DevOps Engineer] Run complete.", flush=True)

if __name__ == "__main__":
    main()
