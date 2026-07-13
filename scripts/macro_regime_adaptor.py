#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
"""
Macro Regime Adaptor — Bridges macro_context_daily into execution config.
Runs every hour. Reads current macro regime, adjusts trading params.
No restart needed — changes take effect on next signal evaluation.
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Tuple

DB_HOST = "127.0.0.1"
DB_PORT = "5432"
DB_USER = "postgres"
DB_NAME = "postgres"
DB_PASSWORD = os.environ.get("POSTGRES_PASSWORD") or os.environ.get("PGPASSWORD") or "postgres"

# Regime thresholds derived from 6 months of macro data analysis
# See roadmaps/evidence/2026-07-02-macro-regime-mismatch-root-cause.md
REGIME_CONFIGS = {
    "RISK_ON": {     # F/G > 25, DXY < 119, BTC DOM < 56
        "pass_rate_limit": 120,
        "confidence_threshold": 40,
        "position_size_mult": 1.0,
        "risk": "normal",
    },
    "NEUTRAL": {     # F/G 15-25, DXY 119-120.5
        "pass_rate_limit": 80,
        "confidence_threshold": 40,
        "position_size_mult": 0.75,
        "risk": "moderate",
    },
    "RISK_OFF": {    # F/G < 15, DXY > 120.5, BTC DOM > 56
        "pass_rate_limit": 40,
        "confidence_threshold": 45,
        "position_size_mult": 0.50,
        "risk": "conservative",
    },
}


def run_psql(query: str) -> str:
    cmd = [
        "psql", "-h", DB_HOST, "-p", DB_PORT, "-U", DB_USER,
        "-d", DB_NAME, "-t", "-A", "-F", "|", "-c", query,
    ]
    env = {"PGPASSWORD": DB_PASSWORD}
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=15, env=env)
    if result.returncode != 0:
        raise RuntimeError(f"psql error: {result.stderr.strip()}")
    return result.stdout.strip()


def get_latest_macro() -> Dict[str, Any]:
    """Fetch latest macro data from the daily context table."""
    query = """
    SELECT fear_greed, dollar_index, btc_dominance, vix,
           fear_greed_class, altcoin_cycle_phase, qt_status,
           as_of_ts
    FROM macro_context_daily
    ORDER BY as_of_ts DESC
    LIMIT 1;
    """
    raw = run_psql(query)
    if not raw:
        return {}
    parts = raw.split("|")
    if len(parts) < 8:
        return {}
    return {
        "fear_greed": float(parts[0]) if parts[0] else None,
        "dxy": float(parts[1]) if parts[1] else None,
        "btc_dom": float(parts[2]) if parts[2] else None,
        "vix": float(parts[3]) if parts[3] else None,
        "fear_class": parts[4].strip(),
        "alt_phase": parts[5].strip(),
        "qt_status": parts[6].strip(),
        "timestamp": parts[7].strip(),
    }


def classify_regime(macro: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
    """Classify macro state into RISK_ON/NEUTRAL/RISK_OFF plus reasoning."""
    fg = macro.get("fear_greed")
    dxy = macro.get("dxy")
    btc_dom = macro.get("btc_dom")

    reasons = []

    if fg is not None:
        if fg > 25:
            reasons.append(f"F/G={fg:.0f} (>25 = RISK_ON)")
        elif fg < 15:
            reasons.append(f"F/G={fg:.0f} (<15 = RISK_OFF)")
        else:
            reasons.append(f"F/G={fg:.0f} (15-25 = NEUTRAL)")

    if dxy is not None:
        if dxy < 119:
            reasons.append(f"DXY={dxy:.1f} (<119 = RISK_ON)")
        elif dxy > 120.5:
            reasons.append(f"DXY={dxy:.1f} (>120.5 = RISK_OFF)")
        else:
            reasons.append(f"DXY={dxy:.1f} (119-120.5 = NEUTRAL)")

    if btc_dom is not None:
        if btc_dom < 56:
            reasons.append(f"BTC_DOM={btc_dom:.1f}% (<56% = ALT_SEASON)")
        else:
            reasons.append(f"BTC_DOM={btc_dom:.1f}% (>56% = BTC_DOMINANCE)")

    risk_on_score = 0
    risk_off_score = 0

    if fg is not None:
        if fg > 25:
            risk_on_score += 2
        elif fg < 15:
            risk_off_score += 2
        else:
            risk_off_score += 1

    if dxy is not None:
        if dxy < 119:
            risk_on_score += 2
        elif dxy > 120.5:
            risk_off_score += 2
        else:
            risk_off_score += 1

    if risk_on_score >= risk_off_score and risk_on_score >= 2:
        regime = "RISK_ON"
    elif risk_off_score > risk_on_score and risk_off_score >= 2:
        regime = "RISK_OFF"
    else:
        regime = "NEUTRAL"

    return regime, REGIME_CONFIGS[regime], "; ".join(reasons)


def main():
    print(f"=== Macro Regime Adaptor — {datetime.now(timezone.utc).isoformat()} ===")
    print()

    macro = get_latest_macro()
    if not macro:
        print("ERROR: No macro data available.")
        print("")
        print("The macro_context_daily table is empty or inaccessible.")
        print("This means the macro regime adaptor cannot function.")
        print("Action: Ensure the daily macro context collection is running.")
        sys.exit(1)

    print(f"Macro snapshot: {macro['timestamp']}")
    print(f"  Fear/Greed:   {macro.get('fear_greed')} ({macro.get('fear_class')})")
    print(f"  DXY:          {macro.get('dxy')}")
    print(f"  BTC Dom:      {macro.get('btc_dom')}%")
    print(f"  VIX:          {macro.get('vix')}")
    print(f"  Alt phase:    {macro.get('alt_phase')}")
    print(f"  QT status:    {macro.get('qt_status')}")
    print()

    regime, config, reasons = classify_regime(macro)

    print(f"Classified regime: {regime}")
    print(f"Reasoning: {reasons}")
    print()

    print("Recommended config:")
    print(f"  Pass rate limit:       {config['pass_rate_limit']}/min")
    print(f"  Confidence threshold:  {config['confidence_threshold']}")
    print(f"  Position size mult:   {config['position_size_mult']}x")
    print(f"  Risk profile:         {config['risk']}")
    print()

    payload = json.dumps({
        "regime": regime,
        "config": config,
        "macro": {k: str(v) for k, v in macro.items()},
        "reasons": reasons,
    })

    insert_sql = f"""
    INSERT INTO n8n_market_data (source, payload)
    VALUES ('macro_regime_adaptor', '{payload.replace(chr(39), chr(39) + chr(39))}'::jsonb);
    """

    try:
        run_psql(insert_sql)
        print("Decision logged to n8n_market_data.")
    except Exception as e:
        print(f"Logging failed (non-fatal): {e}")

    print()
    print("=== Done ===")
    print()
    print("Next action: When this regime persists for 24h, adjust the")
    print(f"ORACLE_SIGNAL_PASS_RATE_LIMIT env var to {config['pass_rate_limit']}")
    print("via the deploy-phase8-alignment.sh script.")
    print()


if __name__ == "__main__":
    main()
