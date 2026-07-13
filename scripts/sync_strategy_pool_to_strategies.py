#!/usr/bin/env python3
"""
strategy_pool → strategies Sync Pipeline
"""
import json
import subprocess
import sys
import uuid
from datetime import datetime, timezone

SYSTEM_USER_ID = "00000000-0000-0000-0000-000000000000"
DEFAULT_RISK_PROFILE = { "maxPortfolioRiskPct": 2.0, "maxLeverage": 3, "maxConcurrentPositions": 3, "preferHedgeMode": False }
DEFAULT_EXIT = { "takeProfitTargets": [{"rr": 2.0, "percentToClose": 50},{"rr": 3.0, "percentToClose": 100}], "stopLossType": "atr", "maxHoldTimeMinutes": 480, "breakevenThresholdPercent": 2.0, "trailingActivationPercent": 2.0 }
MIN_CONFIDENCE = 42

def psql(query):
    cmd = ["docker","exec","-i","sycodetrading-supabase-db","psql","-U","postgres","-d","postgres","-c",query]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        print(f"  [ERROR] {r.stderr.strip()[:200]}", file=sys.stderr)
        return ""
    return r.stdout.strip()

def pg_array_to_list(s):
    if not s or s.strip() == "":
        return []
    s = s.strip()
    if s.startswith("{") and s.endswith("}"):
        inner = s[1:-1]
        if not inner.strip():
            return []
        return [x.strip().strip('"') for x in inner.split(",")]
    return []

def extract_direction(name, entry_rules):
    d = entry_rules.get("direction","")
    if d:
        return [d.lower()]
    u = name.upper()
    if "_SHORT" in u or u.startswith("SHORT"):
        return ["short"]
    if "_LONG" in u or u.startswith("LONG"):
        return ["long"]
    dirs = []
    if "LONG" in u: dirs.append("long")
    if "SHORT" in u: dirs.append("short")
    return dirs if dirs else ["long","short"]

def extract_timeframes(pg_tf, entry_rules):
    if pg_tf and len(pg_tf) > 0:
        return list(pg_tf)
    tf = entry_rules.get("timeframe","")
    if tf: return [tf]
    tf2 = entry_rules.get("timeframes",[])
    if isinstance(tf2,list) and len(tf2)>0: return list(tf2)
    return ["15m","1h"]

def build_exit(exit_raw):
    ex = {}
    if exit_raw:
        try: ex = json.loads(exit_raw)
        except: ex = {}
    if not ex: return dict(DEFAULT_EXIT)
    g = dict(DEFAULT_EXIT)
    sa = ex.get("stop_atr") or ex.get("stop_loss_atr_multiple")
    if sa:
        g["stopLossType"] = "atr"
        g["atrMultiple"] = float(sa)
    sl = ex.get("stop_loss")
    if sl and not sa:
        g["stopLossType"] = "fixed"
        g["stopLossPct"] = float(sl)*100
    ta = ex.get("target_atr") or ex.get("take_profit_atr_multiple")
    tp = ex.get("take_profit")
    if ta:
        v=float(ta); g["takeProfitTargets"]=[{"rr":v*0.5,"percentToClose":50},{"rr":v,"percentToClose":100}]
    elif tp:
        v=float(tp); g["takeProfitTargets"]=[{"rr":v*2,"percentToClose":50},{"rr":v*4,"percentToClose":100}]
    bh = ex.get("bars_to_hold"); mh = ex.get("max_hold_bars")
    if bh or mh:
        g["maxHoldTimeMinutes"] = (int(bh or 0)+int(mh or 0))*60
    tr = ex.get("trailing_activation_pct")
    if tr: g["trailingActivationPercent"] = float(tr)*100
    br = ex.get("breakeven")
    if br: g["breakevenThresholdPercent"] = float(br)*100
    pp = ex.get("partial_exit_pct"); pa = ex.get("partial_exit_at")
    if pp and pa: g["partialProfitConfig"] = {"enabled":True,"tiers":[{"closePct":float(pp),"profitPct":float(pa)}]}
    g["useSystemDefaults"] = False
    return g

def build_filter(name, entry_raw, pg_tf_raw):
    er = {}
    if entry_raw:
        try: er = json.loads(entry_raw)
        except: er = {}
    tfs = pg_array_to_list(pg_tf_raw)
    dirs = extract_direction(name, er)
    tfs = extract_timeframes(tfs, er)
    return {"symbols":[],"directions":dirs,"timeframes":tfs,"tags":[],"minConfidence":MIN_CONFIDENCE,"priority":5}

def build_meta(pid, name):
    return {"strategyPoolId":pid,"source":"strategy_pool_sync_pipeline","syncedAt":datetime.now(timezone.utc).isoformat(),"notes":f"Auto-synced from strategy_pool id={pid} ({name})"}

def esc(s): return s.replace("'","''")

def main():
    print("="*60)
    print("strategy_pool -> strategies Sync Pipeline")
    print(f"Started: {datetime.now(timezone.utc).isoformat()}")
    # Step 1
    print("\n[1/5] Querying strategy_pool...")
    query = """SELECT id, name, COALESCE(description,''), COALESCE(entry_rules::text,''), COALESCE(exit_rules::text,''), COALESCE(preferred_timeframes::text,'') FROM strategy_pool WHERE status='paper' ORDER BY id;"""
    raw = psql(query)
    if not raw:
        print("  No entries found"); return
    entries = []
    lines = raw.strip().splitlines()
    found_data = False
    for line in lines:
        if not line.strip():
            continue
        if not found_data:
            if "---" in line or "----" in line:
                found_data = True
            continue
        parts = line.split(" | ")
        if len(parts)<6: continue
        entries.append({"id":int(parts[0].strip()),"name":parts[1].strip(),"description":parts[2].strip(),"entry_rules":parts[3].strip(),"exit_rules":parts[4].strip(),"preferred_timeframes":parts[5].strip()})
    print(f"  Found {len(entries)} paper entries")
    # Step 2
    print("\n[2/5] Checking existing strategies...")
    existing = set()
    ex_raw = psql("SELECT name FROM strategies;")
    if ex_raw:
        found_data = False
        for line in ex_raw.strip().splitlines():
            if not line.strip():
                continue
            if not found_data:
                if "---" in line or "----" in line:
                    found_data = True
                continue
            line = line.strip()
            if line:
                existing.add(line)
    print(f"  Existing strategies: {len(existing)}")
    # Step 3
    print("\n[3/5] Determining actions...")
    to_create, to_enable = [], []
    for e in entries:
        if e["name"] in existing:
            to_enable.append(e)
            print(f"  [ENABLE] {e['name']}")
        else:
            to_create.append(e)
            print(f"  [CREATE] {e['name']}")
    # Step 4
    print(f"\n[4/5] Creating {len(to_create)} entries...")
    for e in to_create:
        sf = build_filter(e["name"],e["entry_rules"],e["preferred_timeframes"])
        ex = build_exit(e["exit_rules"])
        meta = build_meta(e["id"],e["name"])
        now = datetime.now(timezone.utc).isoformat()
        nid = str(uuid.uuid4())
        sql = f"""INSERT INTO strategies (id,user_id,name,description,engine,enabled,trading_mode,signal_filter,risk_profile,exit_guidelines,meta,total_trades,winning_trades,total_pnl,version,created_at,updated_at) VALUES ('{nid}'::uuid,'{SYSTEM_USER_ID}'::uuid,'{esc(e['name'])}',{json.dumps(e['description']) if e['description'] else 'NULL'}::text,'custom',true,'paper','{esc(json.dumps(sf))}'::jsonb,'{esc(json.dumps(DEFAULT_RISK_PROFILE))}'::jsonb,'{esc(json.dumps(ex))}'::jsonb,'{esc(json.dumps(meta))}'::jsonb,0,0,'0',1,'{now}'::timestamptz,'{now}'::timestamptz);"""
        print(f"  {e['name']}...", end=" ")
        r = psql(sql)
        print(f"OK ({nid[:8]}...)" if "0 1" in r else f"FAIL: {r[:80]}")
    # Step 5
    print(f"\n[5/5] Enabling {len(to_enable)} existing entries...")
    for e in to_enable:
        r = psql(f"SELECT id FROM strategies WHERE name='{esc(e['name'])}' LIMIT 1;")
        sid = None
        if r:
            for line in r.strip().splitlines():
                line=line.strip()
                if "-" in line and len(line)>20: sid=line; break
        if not sid:
            print(f"  [WARN] {e['name']}: not found"); continue
        r = psql(f"UPDATE strategies SET enabled=true,updated_at='{datetime.now(timezone.utc).isoformat()}'::timestamptz WHERE id='{sid}'::uuid;")
        print(f"  {e['name']}: OK" if "UPDATE 1" in r else f"  {e['name']}: {r[:80]}")
    print(f"\nSummary: Created={len(to_create)} Enabled={len(to_enable)}")

if __name__=="__main__":
    main()
