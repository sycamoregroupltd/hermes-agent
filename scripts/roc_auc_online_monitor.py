#!/usr/bin/env python3
"""
Online ROC-AUC Monitor for compute_conviction fix (c5659838c).

Queries canonical_outcomes_v2 with the FIXED compute_conviction logic,
computes ROC-AUC daily against accumulated Tier-1 realized outcomes.
Alerts when ROC-AUC drops below 0.50 or when N exceeds 500 (power threshold).

Usage (cron-compatible, no args):
    python3 roc_auc_online_monitor.py

Output: human-readable report + structured JSON to stdout.
Side effect: updates weekly Obsidian monitoring note.

Boundary: Read-only DB queries. No trades, credentials, or config changes.
"""

import json
import math
import os
import subprocess
import sys
from datetime import date, datetime, timezone

# ── Constants ──
SENTINEL_CONVICTION = 1.0
SCORE_PRIOR = 0.42
GAIN_SCALE = 0.84

ALERT_AUC_FLOOR = 0.50
POWER_THRESHOLD_N = 500

OBSIDIAN_VAULT = '/home/frank/obsidian/sycode-trading'
MONITORING_DIR = os.path.join(OBSIDIAN_VAULT, 'Reviews', 'monitoring')
STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.state')
STATE_FILE = os.path.join(STATE_DIR, 'roc_auc_monitor_state.json')

# ── DB Connection ──
POSTGRES_HOST = os.environ.get('POSTGRES_HOST', '127.0.0.1')
POSTGRES_PORT = os.environ.get('POSTGRES_PORT', '5432')
POSTGRES_DB = os.environ.get('POSTGRES_DB', 'postgres')
POSTGRES_USER = os.environ.get('POSTGRES_USER', 'postgres')
POSTGRES_PASS = os.environ.get('POSTGRES_PASS', 'postgres')


def run_sql(sql: str, timeout: int = 30) -> list[dict]:
    """Run SQL via docker exec psql, return list of dicts."""
    docker_cmd = [
        'docker', 'exec', '-e', f'PGPASSWORD={POSTGRES_PASS}',
        'sycodetrading-supabase-db',
        'psql', '-h', POSTGRES_HOST, '-U', POSTGRES_USER, '-d', POSTGRES_DB,
        '-X', '-A', '-F', '|', '--pset', 'footer=off', '-q',
        '-c', sql,
    ]
    try:
        result = subprocess.run(
            docker_cmd, capture_output=True, text=True, timeout=timeout
        )
        if result.returncode != 0:
            print(f'SQL failed: {result.stderr[:500]}', file=sys.stderr)
            return []
        lines = [l.strip() for l in result.stdout.strip().split('\n') if l.strip()]
        if not lines:
            return []
        cols = [c.strip() for c in lines[0].split('|')]
        rows = []
        for line in lines[1:]:
            vals = [v.strip() for v in line.split('|')]
            if len(vals) == len(cols):
                rows.append(dict(zip(cols, vals)))
        return rows
    except subprocess.TimeoutExpired:
        print(f'SQL timed out after {timeout}s', file=sys.stderr)
        return []
    except Exception as e:
        print(f'SQL error: {e}', file=sys.stderr)
        return []


def normalize_probability(raw) -> float | None:
    """Normalize mixed 0-1 vs 0-100 score units to 0-1."""
    try:
        value = float(raw)
    except (ValueError, TypeError):
        return None
    if value != value or value < 0:
        return None
    if value > 1:
        value = value / 100
    return max(0.0, min(1.0, value))


def compute_conviction_fixed(row: dict) -> float:
    """
    Re-implement the FIXED compute_conviction from commit c5659838c.
    Uses stored signal_journeys fields as point-in-time context proxies.
    """
    score = SCORE_PRIOR
    signals_considered = 0

    # 1. Base conviction from signal_journeys (weight: 50%), with sentinel guard
    eng_conv = normalize_probability(row.get('conviction_score'))
    if eng_conv is not None and 0 < eng_conv < SENTINEL_CONVICTION:
        score = score * (1 - 0.5) + eng_conv * 0.5
        signals_considered += 1

    # 2. Confluence log parsing
    conf_raw = row.get('confluence_json', '{}')
    conf = {}
    if isinstance(conf_raw, str) and conf_raw:
        try:
            conf = json.loads(conf_raw)
        except (json.JSONDecodeError, TypeError):
            pass
    if isinstance(conf, dict):
        total_score = conf.get('total', 0)
        if isinstance(total_score, (int, float)) and total_score > 0:
            normalized = min(total_score / 100, 1.0)
            score = score * (1 - 0.3) + normalized * 0.3
            signals_considered += 1

    # 3. Regime favorable adjustment (gain scaled by 0.84)
    regime_str = str(row.get('regime_favorable', 'false')).lower()
    if regime_str in ('true', 't', '1', 'yes'):
        score += 0.063
        signals_considered += 1
    elif regime_str in ('false', 'f', '0', 'no'):
        score -= 0.063

    # 4. Funding rate alignment (gains scaled by 0.84)
    direction = row.get('direction', '')
    funding_rate_raw = row.get('market_funding_rate_annualized') or row.get('market_funding_rate')
    if funding_rate_raw is not None and funding_rate_raw:
        try:
            annualized_rate = float(funding_rate_raw)
            if annualized_rate > 0.50:
                funding_regime = 'EXTREME_POSITIVE'
            elif annualized_rate > 0.30:
                funding_regime = 'ELEVATED_POSITIVE'
            elif annualized_rate < -0.30:
                funding_regime = 'ELEVATED_NEGATIVE'
            elif annualized_rate < -0.50:
                funding_regime = 'EXTREME_NEGATIVE'
            else:
                funding_regime = 'NEUTRAL'

            if funding_regime == 'EXTREME_POSITIVE' and direction == 'SHORT':
                score += 0.101
            elif funding_regime == 'EXTREME_NEGATIVE' and direction == 'LONG':
                score += 0.101
            elif funding_regime in ('EXTREME_POSITIVE', 'ELEVATED_POSITIVE'):
                score -= 0.050
            elif funding_regime in ('EXTREME_NEGATIVE', 'ELEVATED_NEGATIVE'):
                score -= 0.050
            signals_considered += 1
        except (ValueError, TypeError):
            pass

    # 5. News sentiment alignment (FIXED: direction-aware, proxied via fear/greed)
    fear_greed_class = str(row.get('market_fear_greed_class', '')).upper()
    if fear_greed_class:
        news_aligned_gain = 0.067
        news_misaligned_penalty = -0.042

        if fear_greed_class == 'GREED' and direction == 'LONG':
            score += news_aligned_gain
        elif fear_greed_class == 'FEAR' and direction == 'SHORT':
            score += news_aligned_gain
        elif fear_greed_class in ('GREED', 'EXTREME_GREED'):
            score += news_misaligned_penalty
        elif fear_greed_class in ('FEAR', 'EXTREME_FEAR'):
            score += news_misaligned_penalty
        signals_considered += 1

    # 6. Macro-regime adjustment (gains scaled by 0.84)
    macro_regime = row.get('macro_regime', '')
    if macro_regime == 'TRANSITIONING':
        score -= 0.042
        signals_considered += 1
    timeframe = row.get('timeframe', '')
    if timeframe == '4h' and macro_regime == 'RISK_ON':
        score += 0.050
    elif timeframe == '4h' and macro_regime == 'RISK_OFF':
        score -= 0.034

    # 7. OI divergence signal (gain scaled by 0.84)
    oi_delta = row.get('market_oi_delta_percent')
    if oi_delta is not None:
        try:
            oi_delta_val = float(oi_delta) if oi_delta else 0
            if abs(oi_delta_val) > 2:
                score += 0.034
                signals_considered += 1
        except (ValueError, TypeError):
            pass

    # 8. Volume confirmation (gain scaled by 0.84)
    vol_ratio = row.get('volume_ratio_at_entry')
    if vol_ratio is not None:
        try:
            vol_ratio_v = float(vol_ratio)
            if vol_ratio_v > 1.5:
                score += 0.050
                signals_considered += 1
        except (ValueError, TypeError):
            pass

    score = max(0.0, min(1.0, score))
    score = round(score, 3)
    return score


def compute_roc_auc(scores: list[float], outcomes: list[bool]) -> float:
    """Compute ROC-AUC using the Mann-Whitney U statistic (trapezoidal)."""
    if len(scores) != len(outcomes) or len(scores) < 2:
        return 0.5
    pairs = sorted(zip(scores, outcomes), key=lambda p: -p[0])
    n_pos = sum(1 for _, o in pairs if o)
    n_neg = len(pairs) - n_pos
    if n_pos == 0 or n_neg == 0:
        return 0.5
    rank_sum = 0
    for i, (_, outcome) in enumerate(pairs):
        if outcome:
            rank_sum += (i + 1)
    u_stat = rank_sum - (n_pos * (n_pos + 1)) / 2
    auc = u_stat / (n_pos * n_neg)
    return auc


def compute_auprc(scores: list[float], outcomes: list[bool]) -> float:
    """Compute precision-recall AUC (trapezoidal)."""
    pairs = sorted(zip(scores, outcomes), key=lambda p: -p[0])
    n_pos = sum(1 for _, o in pairs if o)
    if n_pos == 0:
        return 0.0
    thresholds = sorted(set(scores), reverse=True)
    precisions = []
    recalls = []
    tp = 0
    fp = 0
    fn = n_pos
    prev_score = None
    for score, outcome in pairs:
        if prev_score is not None and score != prev_score:
            prec = tp / (tp + fp) if (tp + fp) > 0 else 1.0
            rec = tp / n_pos if n_pos > 0 else 0.0
            precisions.append(prec)
            recalls.append(rec)
        if outcome:
            tp += 1
            fn -= 1
        else:
            fp += 1
        prev_score = score
    prec = tp / (tp + fp) if (tp + fp) > 0 else 1.0
    rec = tp / n_pos if n_pos > 0 else 0.0
    precisions.append(prec)
    recalls.append(rec)
    auprc = 0.0
    for i in range(1, len(recalls)):
        auprc += (recalls[i] - recalls[i - 1]) * precisions[i]
    return auprc


def compute_expected_calibration_error(scores: list[float], outcomes: list[bool], n_bins: int = 10) -> float:
    """Compute Expected Calibration Error (ECE)."""
    min_s, max_s = min(scores), max(scores)
    if max_s == min_s:
        return 0.0
    bin_edges = [min_s + (max_s - min_s) * i / n_bins for i in range(n_bins + 1)]
    bin_edges[-1] += 1e-10
    total_ece = 0.0
    for i in range(n_bins):
        lo, hi = bin_edges[i], bin_edges[i + 1]
        bin_scores = [s for s, o in zip(scores, outcomes) if lo <= s < hi]
        bin_outcomes = [o for s, o in zip(scores, outcomes) if lo <= s < hi]
        if not bin_scores:
            continue
        avg_pred = sum(bin_scores) / len(bin_scores)
        avg_actual = sum(bin_outcomes) / len(bin_outcomes)
        total_ece += len(bin_scores) * abs(avg_pred - avg_actual)
    return total_ece / len(scores)


def score_fisher_pvalue(n_pos: int, n_neg: int, auc: float) -> float:
    """Approximate p-value for ROC-AUC using Mann-Whitney U normal approximation."""
    if n_pos == 0 or n_neg == 0:
        return 1.0
    u = auc * n_pos * n_neg
    mu = n_pos * n_neg / 2.0
    sigma = math.sqrt(n_pos * n_neg * (n_pos + n_neg + 1) / 12.0)
    if sigma == 0:
        return 1.0
    z = (u - mu) / sigma
    return 2 * (1 - 0.5 * (1 + math.erf(abs(z) / math.sqrt(2))))


# ── State Persistence ──

def load_state() -> dict:
    """Load persistent state from JSON file."""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {
        'history': [],
        'peak_auc': 0.0,
        'low_auc_alerted': False,
        'n_500_alerted': False,
        'first_run': datetime.now(timezone.utc).isoformat(),
        'last_run': None,
    }


def save_state(state: dict) -> None:
    """Save persistent state."""
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)


# ── Weekly Obsidian Note ──

def get_week_label(d: date) -> str:
    """Get ISO week label like 2026-W31."""
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def write_weekly_note(run_data: dict, sample_metrics) -> str:
    """Update or create a weekly Obsidian monitoring note.

    Returns the note path.
    """
    today = date.today()
    week_label = get_week_label(today)
    note_path = os.path.join(MONITORING_DIR, f"{today}-roc-auc-monitor-{week_label}.md")

    # Build the run log entry as a table row
    run_entry = (
        f"| {run_data['timestamp']} "
        f"| {run_data['n']} "
        f"| {run_data['wins']} "
        f"| {run_data['win_rate_pct']:.2f}% "
        f"| {run_data['roc_auc']:.4f} "
        f"| {run_data['p_value']:.4f} "
        f"| {run_data['auprc']:.4f} "
        f"| {run_data['ece']:.4f} "
        f"| {'⚠️' if run_data['alerts'] else '✓'} "
        f"|"
    )

    # Build alert summary
    alert_lines = []
    if run_data['alerts']:
        for alert in run_data['alerts']:
            alert_lines.append(f"- {alert}")

    alerts_section = "\n".join(alert_lines) if alert_lines else "None"

    # Check if note exists
    if os.path.exists(note_path):
        # Read existing content, append new run log
        with open(note_path) as f:
            existing = f.read()

        # Find the table body insertion point (after header)
        body_marker = '| --- | --- | --- | --- | --- | --- | --- | --- | --- |'

        if body_marker in existing:
            # Append after the last table row; find end of table section
            lines = existing.split('\n')
            # Find a section marker like ## or end of file
            insert_idx = len(lines)
            for i, line in enumerate(lines):
                if line.startswith('## ') and i > lines.index(body_marker) if body_marker in lines else 0:
                    insert_idx = i
                    break
            lines.insert(insert_idx, run_entry)
            # Update the note's updated date and alerts
            for i, line in enumerate(lines):
                if line.startswith('updated:'):
                    lines[i] = f"updated: '{today}'"
            new_content = '\n'.join(lines)
        else:
            # Unexpected format — append at end
            new_content = existing.rstrip() + '\n' + run_entry + '\n'
    else:
        # Create new weekly note with full frontmatter
        os.makedirs(MONITORING_DIR, exist_ok=True)
        new_content = f"""---
title: "ROC-AUC Online Monitor — Week {week_label}"
type: task-evidence
status: active
created: '{today}'
updated: '{today}'
confidence: medium
tags:
  - sycode-trading
  - compute-conviction
  - roc-auc
  - monitoring
  - c5659838c
  - t_7cbf281c
sources:
  - "postgres:canonical_outcomes_v2"
  - "postgres:signal_journeys"
  - "commit:c5659838c"
project: sycode-trading
kanban_task: t_7cbf281c
kanban_board: jarvis-os
owners:
  - research-trading
review_after: {today.isoformat()}
---

# ROC-AUC Online Monitor — Week {week_label}

**Rolling ROC-AUC monitor for compute_conviction fix (c5659838c).**

- Script: `/home/frank/.hermes/scripts/roc_auc_online_monitor.py`
- Cron: daily, ~/.hermes board cron
- Methodology: same as [[../../Reviews/task-evidence/2026-07-28-t_23a0aaa2-roc-auc-conviction-fix]]
- Fix code: c5659838c — NOT deployed (monitors *intended* fix behavior)
- News sentiment proxied via market_fear_greed_class (same limitation as offline verification)

## Run Log

| Run Timestamp | N | Wins | WR | ROC-AUC | p-value | AUPRC | ECE | Alerts |
|---|---|---|---|---|---|---|---|---|
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
{run_entry}

## Alerts This Week

{alerts_section}

## Cumulative Statistics

- **Peak AUC:** {sample_metrics.get('peak_auc', run_data['roc_auc']):.4f}
- **Current N:** {run_data['n']}
- **Tier-1 realized outcomes accumulating**
- **Power threshold (N=500):** {'REACHED' if run_data['n'] >= POWER_THRESHOLD_N else 'Not yet reached'}
"""

    with open(note_path, 'w') as f:
        f.write(new_content)

    return note_path


# ── Main ──

def main():
    today = date.today()
    run_ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    state = load_state()
    alerts = []

    print(f"ROC-AUC Online Monitor — compute_conviction fix (c5659838c)")
    print(f"Run at: {run_ts}")
    print()

    # ── Query DB ──
    sql = """
        SELECT
            sj.correlation_id,
            sj.symbol,
            sj.direction,
            sj.timeframe,
            sj.triggered_at::text,
            sj.conviction_score,
            sj.confluence_log::text AS confluence_json,
            sj.regime_favorable,
            sj.market_funding_rate,
            sj.market_funding_rate_annualized,
            sj.market_fear_greed,
            sj.market_fear_greed_class,
            sj.macro_regime,
            sj.market_oi_delta_percent,
            sj.volume_ratio_at_entry,
            sj.conviction_model_version,
            c.is_win_net,
            c.net_pnl_percent,
            c.signal_time::text,
            c.realized_closed_at::text
        FROM canonical_outcomes_v2 c
        JOIN signal_journeys sj ON sj.correlation_id = c.correlation_id
        WHERE c.is_win_net IS NOT NULL
          AND sj.conviction_score IS NOT NULL
    """
    rows = run_sql(sql)

    if not rows:
        msg = "FATAL: No data returned. DB query failed or no matching rows."
        print(msg, file=sys.stderr)
        run_data = {
            'timestamp': run_ts,
            'n': 0, 'wins': 0, 'losses': 0, 'win_rate_pct': 0.0,
            'roc_auc': 0.5, 'p_value': 1.0, 'auprc': 0.0, 'ece': 0.0,
            'alerts': ['CRITICAL: No data from DB query'],
            'score_mean': 0.5, 'score_median': 0.5,
        }
        # Still write note to log the failure
        note_path = write_weekly_note(run_data, {'peak_auc': state.get('peak_auc', 0.5)})
        state['last_run'] = run_ts
        save_state(state)
        print(json.dumps({'error': 'No data', 'run_timestamp': run_ts}))
        sys.exit(1)

    n = len(rows)
    outcomes = [r.get('is_win_net', '').lower() in ('true', 't', '1') for r in rows]
    wins = sum(1 for o in outcomes if o)
    losses = n - wins

    # Compute fixed conviction scores
    conviction_scores = []
    for row in rows:
        score = compute_conviction_fixed(row)
        conviction_scores.append(score)

    # ── Metrics ──
    auc = compute_roc_auc(conviction_scores, outcomes)
    p_value = score_fisher_pvalue(wins, losses, auc)
    auprc = compute_auprc(conviction_scores, outcomes)
    ece = compute_expected_calibration_error(conviction_scores, outcomes)
    baseline_precision = wins / n

    score_mean = sum(conviction_scores) / len(conviction_scores)
    score_median = sorted(conviction_scores)[len(conviction_scores) // 2]

    # ── Alerts ──
    # Update peak AUC
    if auc > state.get('peak_auc', 0.0):
        state['peak_auc'] = auc

    # Alert: ROC-AUC below 0.50
    if auc < ALERT_AUC_FLOOR and not state.get('low_auc_alerted', False):
        alerts.append(f"🔴 ROC-AUC dropped below {ALERT_AUC_FLOOR}: current={auc:.4f}")
        state['low_auc_alerted'] = True
    elif auc >= ALERT_AUC_FLOOR and state.get('low_auc_alerted', False):
        # Reset alert flag if recovered
        state['low_auc_alerted'] = False

    # Alert: N exceeds power threshold
    if n >= POWER_THRESHOLD_N and not state.get('n_500_alerted', False):
        alerts.append(f"🟡 POWER THRESHOLD REACHED: N={n} >= {POWER_THRESHOLD_N}. Sufficient for significance testing.")
        state['n_500_alerted'] = True

    # Also alert on fresh significance
    if n >= POWER_THRESHOLD_N and p_value < 0.05:
        alerts.append(f"✅ STATISTICALLY SIGNIFICANT: ROC-AUC={auc:.4f}, p={p_value:.4f}, N={n}")

    # ── Print Report ──
    print("═" * 68)
    print(f"  Sample: {n} Tier-1 realized outcomes ({wins} wins, {losses} losses)")
    print(f"  Win rate: {100*wins/n:.2f}%")
    print()
    print(f"  ROC-AUC:       {auc:.4f}  (p={p_value:.6f})")
    print(f"  AUPRC:         {auprc:.4f}  (baseline PR={baseline_precision:.4f})")
    print(f"  AUPRC/baseline: {auprc/baseline_precision:.2f}x")
    print(f"  ECE:           {ece:.4f}")
    print(f"  Score range:   {min(conviction_scores):.4f}–{max(conviction_scores):.4f}")
    print(f"  Score mean:    {score_mean:.4f}  median: {score_median:.4f}")
    print(f"  Peak AUC:      {state['peak_auc']:.4f}")
    print()

    if alerts:
        print("═" * 68)
        print("  ALERTS:")
        for alert in alerts:
            print(f"    {alert}")
        print()

    if n >= POWER_THRESHOLD_N:
        print(f"  ⚡ Power threshold N={POWER_THRESHOLD_N} REACHED — significance testing is now meaningful.")
    else:
        print(f"  📊 Collecting more data: {n}/{POWER_THRESHOLD_N} — {(n/POWER_THRESHOLD_N)*100:.1f}%")

    # ── Structured JSON for downstream ──
    run_data = {
        'timestamp': run_ts,
        'n': n,
        'wins': wins,
        'losses': losses,
        'win_rate_pct': round(100 * wins / n, 2),
        'roc_auc': round(auc, 4),
        'p_value': round(p_value, 6),
        'auprc': round(auprc, 4),
        'ece': round(ece, 4),
        'baseline_precision': round(baseline_precision, 4),
        'score_mean': round(score_mean, 4),
        'score_median': round(score_median, 4),
        'peak_auc': round(state['peak_auc'], 4),
        'alerts': alerts,
        'power_threshold_reached': n >= POWER_THRESHOLD_N,
        'fix_commit': 'c5659838c',
        'fix_deployed': False,
    }

    print()
    print("═" * 68)
    print("  JSON RESULT:")
    print(json.dumps(run_data, indent=2))

    # ── Persist state ──
    state_entry = {
        'timestamp': run_ts,
        'n': n,
        'wins': wins,
        'roc_auc': round(auc, 4),
        'p_value': round(p_value, 6),
        'alerts': alerts,
    }
    state['history'].append(state_entry)
    # Keep last 100 entries
    if len(state['history']) > 100:
        state['history'] = state['history'][-100:]
    state['last_run'] = run_ts
    save_state(state)

    # ── Write weekly Obsidian note ──
    note_path = write_weekly_note(run_data, {
        'peak_auc': state.get('peak_auc', auc),
    })
    print()
    print(f"  Weekly note: {note_path}")

    # ── Exit with alert code ──
    if alerts:
        print(f"  Exit: ALERTS TRIGGERED ({len(alerts)})")
    else:
        print(f"  Exit: NO ALERTS — nominal monitoring run")


if __name__ == '__main__':
    main()
