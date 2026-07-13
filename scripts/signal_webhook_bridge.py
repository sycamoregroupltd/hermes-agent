#!/usr/bin/env python3
"""Signal → Hermes Webhook Bridge + n8n Data Router
Polls signal_journeys and n8n market data, forwards to Hermes + n8n webhooks."""

import subprocess, json, time, os, urllib.request

HERMES_WEBHOOK = "http://localhost:8646/webhooks/signal-router"
# NOTE (2026-07-11): the bridge runs on the HOST. The docker-network name
# "sycodetrading-n8n" is NOT resolvable from the host; n8n is reachable via the
# host loopback on 127.0.0.1:5678 (verified OPEN). Changed from the docker-name
# so the n8n Route-2/4 forwarding actually lands. Hermes :8646 is a separate
# outage (connection refused; :8080 gateway has no /webhooks/signal-router route)
# and is left unchanged pending the signal-router webhook server investigation.
N8N_WEBHOOK = "http://127.0.0.1:5678/webhook/signal-journey"
STATE_DIR = "/home/frank/.hermes/state"
STATE_FILE = os.path.join(STATE_DIR, "signal_webhook_bridge_state.json")
os.makedirs(STATE_DIR, exist_ok=True)

DB = ["docker", "exec", "sycodetrading-supabase-db", "psql",
      "-h", "localhost", "-U", "postgres", "-d", "postgres", "-t", "-A", "-F|"]

N8N_DB = ["docker", "exec", "sycodetrading-n8n-db", "psql",
          "-U", "n8n", "-d", "n8n", "-t", "-A", "-F|"]

def get_signals(since):
    # NOTE: signal_journeys was migrated (see 2026-07-05 out-of-band DDL incident).
    # The columns confidence_score / status / outcome no longer exist. Map to the
    # surviving columns so the bridge stops erroring every poll cycle (which was
    # silently swallowing all signal forwarding to Hermes + n8n since 2026-07-09).
    #   confidence_score -> composite_confidence_score (real numeric confidence)
    #   status           -> final_status
    #   outcome          -> NULL (no surviving equivalent; downstream treats as None)
    q = f"""SELECT correlation_id, symbol, direction, timeframe,
           pnl_percent, bars_held, created_at,
           composite_confidence_score AS confidence_score,
           final_status AS status,
           NULL AS outcome
    FROM signal_journeys
    WHERE created_at > to_timestamp({since})
      AND pnl_percent IS NOT NULL
      ORDER BY created_at ASC;"""
    r = subprocess.run(DB + ["-c", q], capture_output=True, text=True, timeout=10)
    out = r.stdout.strip()
    if not out:
        return []
    signals = []
    for line in out.split('\n'):
        parts = line.split('|')
        if len(parts) >= 5:
            signals.append({
                "correlation_id": parts[0], "symbol": parts[1],
                "direction": parts[2], "timeframe": parts[3],
                "pnl_percent": parts[4], "bars_held": parts[5] if len(parts) > 5 else None,
                "created_at": parts[6] if len(parts) > 6 else None,
                "confidence_score": parts[7] if len(parts) > 7 else None,
                "status": parts[8] if len(parts) > 8 else None,
                "outcome": parts[9] if len(parts) > 9 else None,
            })
    return signals

def get_n8n_market_data():
    """Read latest successful Market Data execution from n8n DB."""
    q = """SELECT ed.data::text FROM execution_entity e
    JOIN execution_data ed ON ed."executionId" = e.id
    JOIN workflow_entity w ON w.id = e."workflowId"
    WHERE w.name = 'Market Data Enrichment' AND e.status = 'success'
    ORDER BY e."startedAt" DESC LIMIT 1;"""
    r = subprocess.run(N8N_DB + ["-c", q], capture_output=True, text=True, timeout=10)
    raw = r.stdout.strip()
    if not raw:
        return None
    try:
        d = json.loads(raw)
        # Extract output data from Merge Data node
        rd = d[6] if len(d) > 6 and isinstance(d[6], dict) else {}
        for node_name, tasks in rd.items():
            for task in tasks:
                data = task.get('data', {}).get('main', [[{}]])[0]
                if data and len(data) > 0:
                    item = data[0].get('json', {})
                    if item.get('fear_greed') or item.get('btc_funding_rate'):
                        return item
        return None
    except:
        return None

def post(url, payload):
    try:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(url, data=data,
            headers={"Content-Type": "application/json"})
        resp = urllib.request.urlopen(req, timeout=5)
        return resp.status
    except Exception as e:
        return f"err: {e}"

print("[Bridge] Running...", flush=True)
try:
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        last = float(json.load(f).get("last_signal_epoch", time.time()))
except Exception:
    last = time.time()
n8n_check = 0
arb_check = 0
db_write_check = 0

while True:
    try:
        # Route 1: Sycode signal_journeys -> Hermes + n8n
        for sig in get_signals(last):
            sym = sig['symbol']
            print(f"  -> {sym} {sig['direction']}", flush=True)
            post(HERMES_WEBHOOK, sig)
            post(N8N_WEBHOOK, sig)
        last = time.time()
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"last_signal_epoch": last, "updated_at": time.time()}, f)

        # Route 2: n8n collected market data -> Hermes (every 60s)
        if time.time() - n8n_check > 60:
            n8n_check = time.time()
            market = get_n8n_market_data()
            if market:
                print(f"  -> market data: FG={market.get('fear_greed','?')} funding={market.get('btc_funding_rate','?')}", flush=True)
                post(HERMES_WEBHOOK, {"source": "n8n-market-data", "data": market})

        # Route 3: n8n arb data file -> Hermes (every 60s)
        if time.time() - arb_check > 60:
            arb_check = time.time()
            try:
                # Read arb data from n8n container
                r = subprocess.run(
                    ["docker", "exec", "sycodetrading-n8n", "sh", "-c",
                     "tail -1 /tmp/arb-opportunities.jsonl 2>/dev/null || true"],
                    capture_output=True, text=True, timeout=5)
                line = r.stdout.strip()
                if line:
                    arb = json.loads(line)
                    spread = float(arb.get('spread', 0))
                    if spread >= 0.0005:
                        print(f"  ⚠️ ARB: spread {spread*100:.4f}%", flush=True)
                        post(HERMES_WEBHOOK, {"source": "n8n-arb-detector", "type": "alert", "data": arb})
            except Exception:
                pass

        # Route 4: Write n8n execution results to Sycode DB (every 60s)
        if time.time() - db_write_check > 60:
            db_write_check = time.time()
            try:
                n8n_sql = """SELECT w.name, e.status FROM execution_entity e JOIN workflow_entity w ON w.id=e."workflowId" WHERE e."startedAt">NOW()-INTERVAL'2 minutes' ORDER BY e."startedAt" DESC LIMIT 10;"""
                r = subprocess.run(
                    ["docker", "exec", "sycodetrading-n8n-db", "psql", "-U", "n8n", "-d", "n8n", "-t", "-A",
                     "-c", n8n_sql],
                    capture_output=True, text=True, timeout=10)
                lines = [l for l in r.stdout.strip().split('\n') if l]
                if lines:
                    payload = json.dumps({"source": "n8n-executions", "data": lines})
                    # Use shell-safe quoting for psql
                    db_cmd = ["docker", "exec", "-i", "sycodetrading-supabase-db", "psql", "-U", "postgres", "-d", "postgres"]
                    db_sql = "INSERT INTO n8n_market_data (source, payload) VALUES ('n8n-executions', $TAG${}$TAG$::jsonb);".format(payload)
                    p = subprocess.run(db_cmd, input=db_sql, capture_output=True, text=True, timeout=10)
                    print(f"  -> wrote {len(lines)} n8n execc to DB", flush=True)
            except Exception:
                pass

        time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopped.", flush=True)
        break
    except Exception as e:
        print(f"Error: {e}", flush=True)
        time.sleep(5)
