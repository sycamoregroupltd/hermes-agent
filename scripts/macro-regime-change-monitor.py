#!/usr/bin/env python3
"""
Macro Regime Change Monitor — Exit Signal Watchdog
Canonical source: ~/.hermes/profiles/trading-devops/scripts/macro-regime-change-monitor.py

Monitors macro_context_daily for regime transitions and fires exit signals
for the Macro Regime Cycle strategy (strategy_pool id=35).

Exit rules:
  - LONG 4h (entry regime: TRANSITIONING):
    Regime change from TRANSITIONING → RISK_ON or NEUTRAL → EXIT signal
  - SHORT 4h (entry regime: RISK_OFF):
    Regime change from RISK_OFF → TRANSITIONING or NEUTRAL → EXIT signal

Strategy config:
  - strategy_pool id=35, catalog=macro_regime_cycle
  - primary_exit = macro_regime_change
  - secondary_exit = trajectory_bar_2

Architecture:
  - MarketRegimeDetector classifies 8 regimes using price + sentiment indicators
  - The 4-class regime used by the strategy maps: TRANSITIONING=RISK_ON_TRANSITION_MID,
    RISK_OFF, RISK_ON, NEUTRAL
  - This script derives regime from macro_context_daily using a simplified rule set
    matching the MarketRegimeDetector's REGIME_RULES

Output:
  - Watchdog stdout: silent unless regime transition detected (watchdog pattern)
  - On transition: structured JSON alert + kanban escalation task for PM
  - State file: /tmp/macro-regime-change-state.json tracks last known regime + date
  - Report: persisted to ~/obsidian/quant-team/monitoring/macro-regime-exit-signals/
"""

import os, re, sys, subprocess, json, time
from datetime import datetime, timezone, timedelta

from second_brain_writer import write_markdown_atomic

# ── Paths ──────────────────────────────────────────────────────────────
STATE_FILE = "/tmp/macro-regime-change-state.json"
OUTPUT_DIR = os.path.expanduser("~/obsidian/quant-team/monitoring/macro-regime-exit-signals")
PARENT_TASK = "t_b6cb0a6b"  # this task id

# ── Regime classification rules matching MarketRegimeDetector ───────────
# These mirror the rules in server/src/domains/market-triggers/services/MarketRegimeDetector.ts
# Apply priority-ordered: first match wins. Transition is the default fallback.

def classify_regime(row):
    """
    Derive 4-class macro regime from macro_context_daily row.
    Uses MarketRegimeDetector's priority-ordered rule set with simplified inputs.
    
    Returns one of: 'TRANSITIONING', 'RISK_OFF', 'RISK_ON', 'NEUTRAL'
    """
    fg = safe_float(row.get('fear_greed'))
    vix = safe_float(row.get('vix'))
    dx = safe_float(row.get('dollar_index'))
    
    # RISK_OFF: fearGreedIndex < 25 (MarketRegimeDetector rule, priority 100)
    if fg < 25:
        return 'RISK_OFF'
    
    # RISK_ON: fearGreedIndex > 60 AND dollarIndex < 105 (risk appetite)
    if fg > 60 and (dx == 0 or dx < 105):
        return 'RISK_ON'
    
    # TRANSITIONING: fearGreed 25-60, VIX < 25 (transition between regimes)
    if 25 <= fg <= 60 and vix < 25:
        return 'TRANSITIONING'
    
    # NEUTRAL: everything else
    return 'NEUTRAL'


# ── PSQL helpers (same pattern as short-1h-low-vol-neutral-watchlist.py) ─
PSQL_CMD = [
    "docker", "exec", "-e", "PGPASSWORD=postgres",
    "sycodetrading-supabase-db", "psql",
    "-h", "localhost", "-U", "postgres", "-d", "postgres",
    "-v", "ON_ERROR_STOP=1", "-t", "-A", "-P", "pager=off", "-c",
]

def db_query(sql):
    """Run SQL via psql, return stdout or None on failure."""
    try:
        r = subprocess.run(PSQL_CMD + [sql], capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return None

def safe_float(v, default=0.0):
    if v is None or v == "":
        return default
    try:
        return float(v)
    except (ValueError, TypeError):
        return default

def safe_int(v, default=0):
    if v is None or v == "":
        return default
    return int(re.sub(r"[^0-9\-]", "", v))


# ── State file management ──────────────────────────────────────────────

def load_state():
    """Load last known regime + date from state file."""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {"last_regime": None, "last_date": None, "last_exit_fired": None, "transitions": []}

def save_state(state):
    """Persist state to file."""
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
        return True
    except OSError:
        return False


# ── DB Queries ─────────────────────────────────────────────────────────

def fetch_macro_context_latest(limit=3):
    """
    Fetch latest N rows from macro_context_daily.
    Returns list of dicts with {date, fear_greed, vix, dollar_index, macro_regimes}.
    """
    sql = """
    SELECT date,
           COALESCE(fear_greed, 0)::float AS fear_greed,
           COALESCE(vix, 0)::float AS vix,
           COALESCE(dollar_index, 0)::float AS dollar_index,
           COALESCE(macro_regimes::text, '{}') AS macro_regimes_json
    FROM macro_context_daily
    ORDER BY date DESC
    LIMIT %d
    """ % limit
    
    raw = db_query(sql)
    if not raw or raw == "":
        return []
    
    rows = []
    for line in raw.split("\n"):
        if not line.strip():
            continue
        parts = line.split("|")
        if len(parts) < 5:
            continue
        
        macro_regimes_str = parts[4] if parts[4] else "{}"
        try:
            macro_regimes = json.loads(macro_regimes_str)
        except (json.JSONDecodeError, ValueError):
            macro_regimes = {}
        
        rows.append({
            "date": parts[0],
            "fear_greed": safe_float(parts[1]),
            "vix": safe_float(parts[2]),
            "dollar_index": safe_float(parts[3]),
            "macro_regimes": macro_regimes,
        })
    
    return rows


def fetch_active_long_4h_transitioning_positions():
    """
    Query signal_journeys for active LONG 4h positions that entered
    during TRANSITIONING regime.
    """
    sql = """
    SELECT sj.id, sj.correlation_id, sj.symbol, sj.entry_price, sj.triggered_at,
           sj.macro_regime
    FROM signal_journeys sj
    WHERE sj.direction = 'LONG'
      AND sj.timeframe = '4h'
      AND sj.is_active = true
      AND sj.macro_regime = 'TRANSITIONING'
      AND sj.executed_at IS NOT NULL
    ORDER BY sj.triggered_at DESC
    """
    raw = db_query(sql)
    if not raw or raw == "":
        return []
    
    rows = []
    for line in raw.split("\n"):
        if not line.strip():
            continue
        parts = line.split("|")
        if len(parts) < 6:
            continue
        rows.append({
            "id": parts[0],
            "correlation_id": parts[1],
            "symbol": parts[2],
            "entry_price": parts[3],
            "triggered_at": parts[4],
            "entry_regime": parts[5],
        })
    return rows


def fetch_active_short_4h_risk_off_positions():
    """
    Query signal_journeys for active SHORT 4h positions that entered
    during RISK_OFF regime.
    """
    sql = """
    SELECT sj.id, sj.correlation_id, sj.symbol, sj.entry_price, sj.triggered_at,
           sj.macro_regime
    FROM signal_journeys sj
    WHERE sj.direction = 'SHORT'
      AND sj.timeframe = '4h'
      AND sj.is_active = true
      AND sj.macro_regime = 'RISK_OFF'
      AND sj.executed_at IS NOT NULL
    ORDER BY sj.triggered_at DESC
    """
    raw = db_query(sql)
    if not raw or raw == "":
        return []
    
    rows = []
    for line in raw.split("\n"):
        if not line.strip():
            continue
        parts = line.split("|")
        if len(parts) < 6:
            continue
        rows.append({
            "id": parts[0],
            "correlation_id": parts[1],
            "symbol": parts[2],
            "entry_price": parts[3],
            "triggered_at": parts[4],
            "entry_regime": parts[5],
        })
    return rows


# ── Exit signal generation ─────────────────────────────────────────────

def should_fire_exit(previous_regime, current_regime):
    """
    Determine if regime transition should fire an exit signal.
    
    Returns (should_exit: bool, direction: str|None, reason: str|None)
    """
    if previous_regime is None or current_regime is None:
        return False, None, None
    
    # No change — no signal
    if previous_regime == current_regime:
        return False, None, None
    
    # LONG 4h: exited during TRANSITIONING, exit on transition away
    if previous_regime == 'TRANSITIONING' and current_regime in ('RISK_ON', 'NEUTRAL'):
        return True, 'LONG', f"Regime changed from {previous_regime} to {current_regime} — exit LONG 4h positions"
    
    # SHORT 4h: entered during RISK_OFF, exit on transition away
    if previous_regime == 'RISK_OFF' and current_regime in ('TRANSITIONING', 'NEUTRAL'):
        return True, 'SHORT', f"Regime changed from {previous_regime} to {current_regime} — exit SHORT 4h positions"
    
    # Regime flipping to the opposite entry regime (TRANSITIONING → RISK_OFF or vice versa)
    # This is a regime change but not an exit signal for existing positions
    # (LONG exits TRANSITIONING→RISK_OFF, SHORT exits RISK_OFF→RISK_ON not explicitly triggered)
    # These are edge cases — log but don't fire exit
    return False, None, None


def create_exit_signal_ticket(direction, reason, current_regime, previous_regime, positions_affected):
    """
    Create a kanban escalation task for the PM when an exit signal fires.
    """
    now = datetime.now(timezone.utc)
    title = f"REGIME EXIT SIGNAL: {direction} 4h — {previous_regime} → {current_regime} ({now.strftime('%Y-%m-%d')})"
    
    body_lines = [
        f"## Automated Regime-Change Exit Signal",
        "",
        f"**Triggered at:** {now.strftime('%Y-%m-%d %H:%M UTC')}",
        f"**Direction:** {direction}",
        f"**Transition:** {previous_regime} → {current_regime}",
        f"**Reason:** {reason}",
        "",
        "### Affected Positions",
    ]
    
    if positions_affected:
        body_lines.append("")
        body_lines.append("| Symbol | Entry Price | Triggered At | Entry Regime |")
        body_lines.append("|--------|-------------|--------------|--------------|")
        for p in positions_affected:
            body_lines.append(f"| {p['symbol']} | {p['entry_price']} | {p['triggered_at']} | {p['entry_regime']} |")
    else:
        body_lines.append("")
        body_lines.append("No active positions found in signal_journeys. This is a predictive exit signal.")
    
    body_lines.append("")
    body_lines.append("### Required Action")
    body_lines.append("- [ ] Verify regime transition is confirmed (check 2+ consecutive days)")
    body_lines.append("- [ ] Close affected positions if confirmed")
    body_lines.append(f"- [ ] Update strategy_pool id=35 exit_rules status")
    body_lines.append("- [ ] Log the exit signal to Obsidian")
    
    body = "\n".join(body_lines)
    
    try:
        r = subprocess.run(
            [
                "/home/frank/.local/bin/hermes", "kanban", "create",
                title,
                "--assignee", "sycode-trading-pm",
                "--body", body,
                "--parent", PARENT_TASK,
                "--priority", "1",
            ],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0:
            return r.stdout.strip()
        else:
            return f"ERROR creating ticket: {r.stderr.strip()}"
    except Exception as e:
        return f"ERROR: {e}"


# ── Report building ────────────────────────────────────────────────────

def build_report(now, rows, regimes, transition_fired, state):
    """Build markdown report for Obsidian persistence."""
    today_str = now.strftime("%Y-%m-%d")
    
    lines = []
    lines.append(f"# Macro Regime Change Monitor — {today_str}")
    lines.append("")
    lines.append(f"_Generated: {now.strftime('%Y-%m-%d %H:%M UTC')}_")
    lines.append("")
    lines.append("**Strategy:** Macro Regime Cycle (strategy_pool id=35)")
    lines.append("**Monitor:** Regime-change exit signal watchdog (primary_exit=macro_regime_change)")
    lines.append("")
    
    # Regime history (last N days)
    lines.append("## Latest Macro Context")
    lines.append("")
    lines.append("| Date | Fear & Greed | VIX | Dollar Index | Liquidity | Sentiment | Derived Regime |")
    lines.append("|------|-------------|-----|-------------|-----------|-----------|---------------|")
    
    for r, regime in zip(rows, regimes):
        mr = r['macro_regimes']
        liq = mr.get('liquidity_regime', '-')
        sent = mr.get('sentiment_regime', '-')
        lines.append(f"| {r['date']} | {r['fear_greed']} | {r['vix']} | {r['dollar_index']} | {liq} | {sent} | **{regime}** |")
    
    lines.append("")
    
    # Transition tracking
    lines.append("## Regime Transition History")
    lines.append("")
    if state.get('transitions'):
        lines.append("| Date | From | To | Direction |")
        lines.append("|------|------|----|-----------|")
        for t in state['transitions'][-10:]:  # last 10
            lines.append(f"| {t.get('detected_at', '?')} | {t.get('from','?')} | {t.get('to','?')} | {t.get('direction','?')} |")
    else:
        lines.append("No transitions recorded yet.")
    lines.append("")
    
    # Exit signal status
    lines.append("## Exit Signal Status")
    lines.append("")
    if transition_fired:
        lines.append("⚠️ **REGIME EXIT SIGNAL FIRED**")
        lines.append(f"- Transition: {transition_fired.get('from')} → {transition_fired.get('to')}")
        lines.append(f"- Direction: {transition_fired.get('direction')}")
        lines.append(f"- Reason: {transition_fired.get('reason')}")
        lines.append(f"- Ticket: {transition_fired.get('ticket_result', 'N/A')}")
    else:
        lines.append("✅ No exit signal — regime stable or not in entry regime.")
    
    lines.append("")
    lines.append(f"**Last known regime:** {state.get('last_regime', 'N/A')} (date: {state.get('last_date', 'N/A')})")
    lines.append("")
    
    return "\n".join(lines) + "\n"


# ── Main ───────────────────────────────────────────────────────────────

def main():
    now = datetime.now(timezone.utc)
    today_str = now.strftime("%Y-%m-%d")
    
    # ── 1. Load state ──────────────────────────────────────────────────
    state = load_state()
    
    # ── 2. Fetch macro_context_daily latest rows ───────────────────────
    rows = fetch_macro_context_latest(limit=3)
    if len(rows) < 2:
        # Not enough data — silent exit (watchdog pattern)
        return 0
    
    # ── 3. Classify regimes for each row ───────────────────────────────
    regimes = [classify_regime(r) for r in rows]
    
    latest_regime = regimes[0]
    latest_date = rows[0]['date']
    previous_regime = regimes[1] if len(regimes) > 1 else None
    previous_date = rows[1]['date'] if len(rows) > 1 else None
    
    # ── 4. Detect transition ───────────────────────────────────────────
    stdout_lines = []
    transition_fired = None
    
    # Check if this is a NEW date (don't re-fire on same date)
    is_new_data = (latest_date != state.get('last_date'))
    
    if is_new_data and previous_regime is not None:
        exit_signal, exit_direction, exit_reason = should_fire_exit(previous_regime, latest_regime)
        
        if exit_signal:
            # Fire exit signal
            affected_positions = []
            if exit_direction == 'LONG':
                affected_positions = fetch_active_long_4h_transitioning_positions()
            elif exit_direction == 'SHORT':
                affected_positions = fetch_active_short_4h_risk_off_positions()
            
            ticket_result = create_exit_signal_ticket(
                exit_direction, exit_reason, latest_regime, previous_regime, affected_positions
            )
            
            transition_fired = {
                "from": previous_regime,
                "to": latest_regime,
                "direction": exit_direction,
                "reason": exit_reason,
                "detected_at": now.strftime('%Y-%m-%d %H:%M UTC'),
                "ticket_result": ticket_result,
                "positions_affected": len(affected_positions),
            }
            
            # Add to state transitions
            state.setdefault('transitions', []).append(transition_fired)
            
            # Stdout output for cron delivery
            stdout_lines.append(f"[REGIME EXIT SIGNAL] {exit_direction} 4h — {previous_regime} → {latest_regime}")
            stdout_lines.append(f"  Reason: {exit_reason}")
            stdout_lines.append(f"  Affected positions: {len(affected_positions)}")
            stdout_lines.append(f"  Kanban ticket: {ticket_result}")
            stdout_lines.append("")
    
    # ── 5. Record regime change (even non-exit) for logging ────────────
    if is_new_data and previous_regime is not None and latest_regime != previous_regime:
        transition = {
            "from": previous_regime,
            "to": latest_regime,
            "direction": None,  # Not an exit signal
            "detected_at": now.strftime('%Y-%m-%d %H:%M UTC'),
            "note": f"Regime changed: {previous_regime} → {latest_regime} (non-exit transition)",
        }
        state.setdefault('transitions', []).append(transition)
        stdout_lines.append(f"[REGIME CHANGE] {previous_regime} → {latest_regime} (monitoring)")
        stdout_lines.append("")
    
    # ── 6. Update state ────────────────────────────────────────────────
    state['last_regime'] = latest_regime
    state['last_date'] = latest_date
    state['last_check'] = now.strftime('%Y-%m-%d %H:%M UTC')
    
    # Keep only last 100 transitions
    if len(state.get('transitions', [])) > 100:
        state['transitions'] = state['transitions'][-100:]
    
    save_state(state)
    
    # ── 7. Build report ────────────────────────────────────────────────
    report = build_report(now, rows, regimes, transition_fired, state)
    report_path = os.path.join(OUTPUT_DIR, f"macro-regime-change-{today_str}.md")
    write_markdown_atomic(
        report_path,
        report,
        title=f"Macro Regime Change Monitor — {today_str}",
        type="task-evidence",
        status="active",
        created=today_str,
        updated=today_str,
        confidence="high",
        tags=["sycode", "macro-regime", "exit-signal", "monitoring"],
        sources=["sycodetrading-supabase-db:macro_context_daily"],
        project="sycode-trading",
        owners=["trading-devops"],
        knowledge_tier="evidence",
        generated=True,
        generator="macro-regime-change-monitor.py",
        operational_status="transition-fired" if transition_fired else "stable",
        kanban_task=PARENT_TASK,
    )
    
    # ── 8. Watchdog: stdout on exit signal only ────────────────────────
    if stdout_lines:
        sys.stdout.write("\n".join(stdout_lines) + "\n")
    # else: silent (watchdog pattern)
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
