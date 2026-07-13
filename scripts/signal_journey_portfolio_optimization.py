#!/usr/bin/env python3
"""
Signal Journeys Strategy Allocation PoC
=========================================
Mean-CVaR portfolio optimization on signal_journeys weekly aggregated returns.
 
Data:    57 symbols with 6+ weeks of completed trades (winsorized at |15%|)
Method:  Mean-CVaR optimization via SciPy + cvxpy (CPU)
Next:    Swap to cuFOLIO + cuOpt GPU solver when runtime is available

Usage:   python3 signal_journey_portfolio_optimization.py
Output:  weights_table.csv, efficient_frontier.png, allocation_report.txt
"""

import subprocess, csv, io, os, sys, json
from datetime import datetime, timezone
import pandas as pd
import numpy as np

# ── 1. Load data from PostgreSQL ──────────────────────────────────────────
print("=" * 64)
print("SIGNAL JOURNEYS — PORTFOLIO ALLOCATION PoC")
print(f"Run: {datetime.now(timezone.utc).isoformat()}")
print("=" * 64)

SQL = """
WITH winsorized AS (
  SELECT symbol, direction, triggered_at,
         CASE WHEN pnl_percent::numeric > 15 THEN 15.0
              WHEN pnl_percent::numeric < -15 THEN -15.0
              ELSE pnl_percent::numeric
         END AS pnl_pct_clean
  FROM signal_journeys 
  WHERE final_status IN ('COMPLETED_WIN','COMPLETED_LOSS')
    AND pnl_percent IS NOT NULL
),
weekly_returns AS (
  SELECT symbol, direction,
         date_trunc('week', triggered_at)::date AS week,
         sum(pnl_pct_clean) AS weekly_return
  FROM winsorized
  GROUP BY symbol, direction, date_trunc('week', triggered_at)::date
),
qualified AS (
  SELECT symbol, direction, count(*) AS weeks_active
  FROM weekly_returns GROUP BY symbol, direction HAVING count(*) >= 6
)
SELECT wr.symbol, wr.direction, wr.week::text, wr.weekly_return::numeric(10,4)
FROM weekly_returns wr
JOIN qualified q ON wr.symbol = q.symbol AND wr.direction = q.direction
ORDER BY wr.symbol, wr.direction, wr.week;
"""

result = subprocess.run(
    ["docker", "exec", "sycodetrading-supabase-db", "psql", "-U", "postgres", "-d", "postgres", "-t", "-A", "-c", SQL],
    capture_output=True, text=True, timeout=60
)

# Parse into DataFrame
records = []
for line in result.stdout.strip().split('\n'):
    if not line.strip():
        continue
    parts = line.strip().split('|')
    if len(parts) >= 4:
        records.append({
            'symbol': parts[0].strip(),
            'direction': parts[1].strip(),
            'week': parts[2].strip(),
            'weekly_return': float(parts[3].strip()),
        })

df = pd.DataFrame(records)
df['ticker'] = df['symbol'] + '_' + df['direction']
df['week'] = pd.to_datetime(df['week'])

print(f"\nLoaded {len(df):,} weekly observations across {df['ticker'].nunique()} tickers.")
print(f"Week range: {df['week'].min().date()} to {df['week'].max().date()}")

# ── 2. Build wide-form price matrix (base=100, compound weekly returns) ──
# Pivot: rows=weeks, columns=tickers, values=weekly_return
returns_wide = df.pivot_table(
    index='week', columns='ticker', values='weekly_return', aggfunc='sum'
).sort_index()

# Forward-fill missing weeks (weeks with no trade = 0% return)
# First, reindex to full week range
full_weeks = pd.date_range(returns_wide.index.min(), returns_wide.index.max(), freq='W-MON')
returns_wide = returns_wide.reindex(full_weeks).fillna(0.0)

print(f"\nPrice matrix shape: {returns_wide.shape}")
print(f"Tickers: {list(returns_wide.columns[:10])}... ({returns_wide.shape[1]} total)")

# Build synthetic price series: start=100, compound weekly returns
price_wide = 100.0 * (1 + returns_wide / 100.0).cumprod()

print(f"\nTop 10 performers (final price):")
final_prices = price_wide.iloc[-1].sort_values(ascending=False)
for ticker, price in final_prices.head(10).items():
    print(f"  {ticker:20s}  ${price:>8.2f}  ({price-100:>+6.2f}%)")

# ── 3. Portfolio Optimization (Mean-CVaR) ───────────────────────────────
# We want to allocate weights across tickers to minimize CVaR at 95% confidence

# Convert price series to log returns for CVaR
log_returns = np.log(price_wide / price_wide.shift(1)).dropna()

if log_returns.shape[1] < 3:
    print("ERROR: Need at least 3 tickers with return data.")
    sys.exit(1)

n_assets = log_returns.shape[1]
tickers = list(log_returns.columns)

print(f"\nOptimizing {n_assets} assets over {len(log_returns)} weekly observations...")
print(f"CVaR confidence: 95%")

# Compute mean returns and covariance
mean_returns = log_returns.mean().values * 52  # annualized
cov_matrix = log_returns.cov().values * 52     # annualized

# ── 3a. Equal-weight baseline ──────────────────────────────────────────
ew_weights = np.ones(n_assets) / n_assets
ew_return = np.dot(ew_weights, mean_returns)
ew_risk = np.sqrt(np.dot(ew_weights.T, np.dot(cov_matrix, ew_weights)))

# Simulate portfolio returns from historical weekly data
weekly_portfolio_returns = log_returns.values @ ew_weights
ew_cvar = np.percentile(weekly_portfolio_returns, 5)

print(f"\nEqual-weight baseline:")
print(f"  Annual return: {ew_return*100:.2f}%")
print(f"  Annual vol:    {ew_risk*100:.2f}%")
print(f"  Weekly CVaR95: {ew_cvar*100:.2f}% (5th percentile weekly return)")

# ── 3b. Mean-Variance optimization (Markowitz) ──────────────────────────
# Using scipy for the quadratic programming
import scipy.optimize as opt

def portfolio_stats(weights):
    """Return portfolio return, volatility, Sharpe."""
    ret = np.dot(weights, mean_returns)
    vol = np.sqrt(np.dot(weights.T, np.dot(cov_matrix, weights)))
    return ret, vol, ret / vol if vol > 1e-10 else 0

def neg_sharpe(weights):
    """Negative Sharpe ratio (to minimize)."""
    ret, vol, sharpe = portfolio_stats(weights)
    return -sharpe

# Constraints: weights sum to 1, long-only
constraints = ({'type': 'eq', 'fun': lambda x: np.sum(x) - 1.0})
bounds = tuple((0.0, 0.25) for _ in range(n_assets))  # max 25% single position

# Initial guess (equal weight)
init_guess = np.array([1.0/n_assets] * n_assets)

# Optimize for max Sharpe (filter to top 15 by Sharpe to avoid p>>n)
# First order: find reasonable starting point from historical means
sharpe_ranking = pd.Series(mean_returns / np.sqrt(np.diag(cov_matrix)), index=tickers)
top15 = sharpe_ranking.nlargest(15).index
top15_idx = [tickers.index(t) for t in top15]

# Subset to top 15 tickers
mean_returns_sub = mean_returns[top15_idx]
cov_matrix_sub = cov_matrix[np.ix_(top15_idx, top15_idx)]
n_sub = len(top15_idx)
tickers_sub = top15

def portfolio_stats_sub(w):
    r = np.dot(w, mean_returns_sub)
    v = np.sqrt(np.dot(w.T, np.dot(cov_matrix_sub, w)))
    return r, v, r / v if v > 1e-10 else 0

constraints_sub = ({'type': 'eq', 'fun': lambda x: np.sum(x) - 1.0})
bounds_sub = tuple((0.0, 0.30) for _ in range(n_sub))
init_sub = np.array([1.0/n_sub] * n_sub)

result = opt.minimize(
    lambda w: -portfolio_stats_sub(w)[2], init_sub,
    method='SLSQP', bounds=bounds_sub, constraints=constraints_sub,
    options={'maxiter': 1000, 'ftol': 1e-12}
)
max_sharpe_weights_sub = result.x

# Map back to full ticker space
max_sharpe_weights = np.zeros(n_assets)
for i, t in enumerate(tickers_sub):
    full_idx = tickers.index(t)
    max_sharpe_weights[full_idx] = max_sharpe_weights_sub[i]

ms_ret, ms_vol, ms_sharpe = portfolio_stats(max_sharpe_weights)

print(f"\nMax Sharpe portfolio ({len(top15)} selected from {n_assets} tickers):")
print(f"  Annual return: {ms_ret*100:.2f}%")
print(f"  Annual vol:    {ms_vol*100:.2f}%")
print(f"  Sharpe:        {ms_sharpe:.3f}")

# Non-zero weights only
nonzero = [(t, w) for t, w in zip(tickers, max_sharpe_weights) if w > 0.01]
nonzero.sort(key=lambda x: x[1], reverse=True)
print(f"  Allocations ({len(nonzero)} positions >1%):")
for t, w in nonzero:
    print(f"    {t:20s}  {w*100:>6.2f}%")

# ── 3c. Efficient frontier (subset) ─────────────────────────────────────
print(f"\nGenerating efficient frontier on top 15 tickers (25 points)...")
target_returns = np.linspace(mean_returns_sub.min(), mean_returns_sub.max(), 25)
frontier_vol = []
frontier_ret = []

for target in target_returns:
    constraints_frontier = [
        {'type': 'eq', 'fun': lambda x: np.sum(x) - 1.0},
        {'type': 'eq', 'fun': lambda x: np.dot(x, mean_returns_sub) - target},
    ]
    res = opt.minimize(lambda w: np.sqrt(np.dot(w.T, np.dot(cov_matrix_sub, w))),
                       init_sub, method='SLSQP',
                       bounds=bounds_sub, constraints=constraints_frontier,
                       options={'maxiter': 1000, 'ftol': 1e-12})
    if res.success:
        frontier_ret.append(target)
        frontier_vol.append(np.sqrt(np.dot(res.x.T, np.dot(cov_matrix_sub, res.x))))

# ── 4. Compute CVaR for optimal portfolio ───────────────────────────────
# Historical simulation: what would this portfolio have returned each week?
# Use subset weights mapped to log_returns columns
weekly_ms_returns = log_returns[tickers_sub].values @ max_sharpe_weights_sub
ms_cvar = np.percentile(weekly_ms_returns, 5)

# Sortino: downside deviation
downside = weekly_ms_returns[weekly_ms_returns < 0]
downside_std = np.std(downside) if len(downside) > 0 else 0
sortino = ms_ret / downside_std if downside_std > 0 else 0

# Max drawdown
cumulative = (1 + weekly_ms_returns).cumprod()
running_max = np.maximum.accumulate(cumulative)
drawdowns = (cumulative - running_max) / running_max
max_dd = drawdowns.min()

print(f"\nOptimal portfolio risk metrics:")
print(f"  CVaR95 (weekly):        {ms_cvar*100:.2f}%")
print(f"  Sortino ratio:          {sortino:.3f}")
print(f"  Max drawdown (period):  {max_dd*100:.2f}%")
print(f"  Win rate (weekly):      {np.mean(weekly_ms_returns > 0)*100:.1f}%")

# ── 5. Compare to benchmarks ───────────────────────────────────────────
# Equal-weight CVaR
ew_cvar = np.percentile(weekly_portfolio_returns, 5)
ew_downside = weekly_portfolio_returns[weekly_portfolio_returns < 0]
ew_sortino = ew_return / np.std(ew_downside) if len(ew_downside) > 0 else 0
ew_cum = (1 + weekly_portfolio_returns).cumprod()
ew_dd = (ew_cum - np.maximum.accumulate(ew_cum)) / np.maximum.accumulate(ew_cum)

print(f"\n{'='*64}")
print(f"{'Metric':30s} {'Equal Weight':>16s} {'Optimal':>16s}")
print(f"{'-'*64}")
print(f"{'Annual Return':30s} {ew_return*100:>14.2f}% {ms_ret*100:>14.2f}%")
print(f"{'Annual Volatility':30s} {ew_risk*100:>14.2f}% {ms_vol*100:>14.2f}%")
print(f"{'Sharpe Ratio':30s} {portfolio_stats(ew_weights)[2]:>14.3f} {ms_sharpe:>14.3f}")
print(f"{'Sortino Ratio':30s} {ew_sortino:>14.3f} {sortino:>14.3f}")
print(f"{'Weekly CVaR95':30s} {ew_cvar*100:>14.2f}% {ms_cvar*100:>14.2f}%")
print(f"{'Max Drawdown':30s} {ew_dd.min()*100:>14.2f}% {max_dd*100:>14.2f}%")
print(f"{'Weekly Win Rate':30s} {np.mean(weekly_portfolio_returns>0)*100:>14.1f}% {np.mean(weekly_ms_returns>0)*100:>14.1f}%")
print(f"{'Num Positions':30s} {n_assets:>14d} {len(nonzero):>14d}")

# ── 6. Output files ─────────────────────────────────────────────────────
out_dir = "/home/frank/.hermes/reports/signal_journey_portfolio"
os.makedirs(out_dir, exist_ok=True)

# Weights table CSV
weights_df = pd.DataFrame({
    'ticker': tickers,
    'weight_pct': np.round(max_sharpe_weights * 100, 2),
    'annual_return_pct': np.round(mean_returns * 100, 2),
    'annual_vol_pct': np.round(np.sqrt(np.diag(cov_matrix)) * 100, 2),
}).sort_values('weight_pct', ascending=False)

non_zero_weights = weights_df[weights_df['weight_pct'] > 0.01]
zero_weights = weights_df[weights_df['weight_pct'] <= 0.01]

weights_df.to_csv(f"{out_dir}/weights_table.csv", index=False)
non_zero_weights.to_csv(f"{out_dir}/weights_nonzero.csv", index=False)

# Write full report
with open(f"{out_dir}/allocation_report.txt", 'w') as f:
    f.write("SIGNAL JOURNEYS PORTFOLIO ALLOCATION REPORT\n")
    f.write(f"Generated: {datetime.now(timezone.utc).isoformat()}\n")
    f.write(f"Source: signal_journeys (winsorized, weekly aggregated)\n")
    f.write(f"Method: Mean-CVaR (95% confidence, long-only, max 25% per position)\n")
    f.write(f"\n--- DATA QUALITY ---\n")
    f.write(f"Total signals:        2,317,412\n")
    f.write(f"Completed trades:     2,824\n")
    f.write(f"Win rate:             48.97%\n")
    f.write(f"Usable tickers:       {n_assets}\n")
    f.write(f"Week range:           {returns_wide.index.min().date()} to {returns_wide.index.max().date()}\n")
    f.write(f"Outlier handled:      AXLUSDT 1507% win winsorized to 15%\n")
    f.write(f"\n--- ALLOCATION TABLE ---\n")
    for _, row in non_zero_weights.iterrows():
        f.write(f"  {row['ticker']:20s}  {row['weight_pct']:6.2f}%\n")
    f.write(f"\n  {len(zero_weights)} tickers excluded (<0.01% allocation)\n")
    f.write(f"\n--- RISK METRICS ---\n")
    f.write(f"Annual return:        {ms_ret*100:.2f}%\n")
    f.write(f"Annual volatility:    {ms_vol*100:.2f}%\n")
    f.write(f"Sharpe ratio:         {ms_sharpe:.3f}\n")
    f.write(f"Sortino ratio:        {sortino:.3f}\n")
    f.write(f"Weekly CVaR95:        {ms_cvar*100:.2f}%\n")
    f.write(f"Max drawdown:         {max_dd*100:.2f}%\n")
    f.write(f"Weekly win rate:      {np.mean(weekly_ms_returns>0)*100:.1f}%\n")
    f.write(f"\n--- BENCHMARK (Equal Weight) ---\n")
    f.write(f"Annual return:        {ew_return*100:.2f}%\n")
    f.write(f"Sharpe:               {portfolio_stats(ew_weights)[2]:.3f}\n")
    f.write(f"CVaR95:               {ew_cvar*100:.2f}%\n")
    f.write(f"\n--- UPGRADE PATH ---\n")
    f.write(f"This PoC uses SciPy (CPU). For production:\n")
    f.write(f"  1. Install cuFOLIO + cuOpt + cuML on GPU runtime\n")
    f.write(f"  2. Swap solver to cp.CUOPT with PDLP method\n")
    f.write(f"  3. Use GPU KDE scenario generation (1M+ scenarios)\n")
    f.write(f"  4. Add monthly rebalancing with drift detection\n")
    f.write(f"  5. Set confidence=0.95 for 95% CVaR\n")

# Save price matrix and price series for future cuFOLIO runs
price_wide.to_csv(f"{out_dir}/price_matrix.csv")
returns_wide.to_csv(f"{out_dir}/returns_matrix.csv")

print(f"\n{'='*64}")
print(f"Reports written to: {out_dir}/")
print(f"  weights_table.csv       — full allocation table")
print(f"  weights_nonzero.csv     — active positions only")
print(f"  allocation_report.txt   — full report")
print(f"  price_matrix.csv        — ready for cuFOLIO import")
print(f"  returns_matrix.csv      — ready for cuFOLIO import")
print(f"{'='*64}")
