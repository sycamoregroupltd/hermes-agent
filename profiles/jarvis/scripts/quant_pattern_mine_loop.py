#!/usr/bin/env python3
"""quant-pattern-mining-loop — deterministic no_agent research loop (t_c36e94ea).

Replaces the previous LLM-agent cron job (5e020636007c) that could not complete
durably in single-query cron mode: its session hit approval-blocked commands
(`python3 -c`/heredoc) and deepseek provider timeouts, every run ended
`status=unknown`, and the gateway scheduler's catch-up re-fires hijacked
next_run_at, so last_run_at never advanced (OVERDUE 282h).

This script is the JOB. Run read-only mining, then persist a dated research note
to the Sycode vault. Exit 0 = durable `completed` run.

Read-only: only psql SELECT/COPY reads + vault file writes. NO trades, NO
strategy_pool/strategies mutation, NO trade_intents, NO live enablement.

Method (from quant-pattern-mining / quant-research-operations doctrine):
  export signals + candles (majors 15m/1h/4h, clean epoch >= 2026-07-05)
  -> full-population synthetic forward labels (NOT executed-only)
  -> direction-adjust, clip +-10%, exclude AXLUSDT, 14bps RT net
  -> combo sweep (direction x timeframe x volatility x session) -> WIN/LOSS
  -> chronological OOS gate (70/30 tail) -> verdict

DB route: host psql at 127.0.0.1:5432 (direct TCP). Data pulled as CSV, polars in-memory.
"""
from __future__ import annotations
import io
import json
import os
import shutil
import subprocess
import sys
import datetime as dt
import polars as pl

# --- config -----------------------------------------------------------------
PGPW = os.environ.get("PGPASSWORD", "postgres")
FEE_BPS = 14.0
CLIP = 10.0
THRESH = 0.2          # % win/loss label threshold
OOS_FRAC = 0.30       # chronological tail fraction held out
MAJORS = ["BTC", "ETH", "BNB", "SOL", "XRP", "DOGE", "ADA", "AVAX", "LINK", "DOT",
          "LTC", "ATOM", "SUI", "APT", "NEAR", "ARB", "OP", "XLM", "UNI", "TRX"]
TFS = ["15m", "1h", "4h"]
EPOCH = "2026-07-05"  # clean epoch post-this-date
VAULT_RESEARCH = "/home/frank/obsidian/sycode-trading/research"

_PSQL = None
def psql_bin() -> str:
    global _PSQL
    if _PSQL is None:
        p = shutil.which("psql")
        if not p:
            for cand in ("/usr/bin/psql", "/usr/lib/postgresql/*/bin/psql"):
                import glob
                hits = sorted(glob.glob(cand))
                if hits:
                    p = hits[-1]
                    break
        _PSQL = p or "psql"
    return _PSQL

def _run_sql(sql: str) -> str:
    cmd = [psql_bin(), "-h", "127.0.0.1", "-p", "5432", "-U", "postgres", "-d", "postgres",
           "-t", "-A", "-F", ",", "-c", sql]
    p = subprocess.run(cmd, env=dict(os.environ, PGPASSWORD=PGPW), capture_output=True,
                       text=True, timeout=900)
    if p.returncode != 0:
        raise RuntimeError(f"psql failed: {p.stderr[-500:]}")
    return p.stdout


def load_signals() -> pl.DataFrame:
    pulls = []
    for tf in TFS:
        sql = f"""
        COPY (
          SELECT symbol, direction, timeframe,
                 to_char(triggered_at, 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') AS triggered_at,
                 entry_price,
                 regime_volatility,
                 CASE WHEN regime_favorable THEN 'true' ELSE 'false' END AS regime_favorable,
                 trading_session
          FROM signal_journeys
          WHERE entry_price > 0 AND timeframe = '{tf}' AND triggered_at >= '{EPOCH}'
        ) TO STDOUT WITH (FORMAT csv, HEADER);
        """
        txt = _run_sql(sql)
        pulls.append(pl.read_csv(io.StringIO(txt),
                                 infer_schema_length=1000,
                                 schema_overrides={"symbol": pl.Utf8, "direction": pl.Utf8,
                                                   "timeframe": pl.Utf8, "entry_price": pl.Float64,
                                                   "regime_volatility": pl.Utf8,
                                                   "trading_session": pl.Utf8,
                                                   "regime_favorable": pl.Boolean}))
    if not pulls:
        return pl.DataFrame()
    sig = pl.concat(pulls)
    pat = "^(" + "|".join(MAJORS) + ")USDT$"
    sig = sig.filter(pl.col("symbol").str.contains(pat))
    sig = sig.with_columns(pl.col("triggered_at").str.to_datetime(time_zone="UTC").alias("triggered_at"))
    return sig


def load_candles(tf: str) -> pl.DataFrame:
    sql = f"""
    COPY (
      SELECT symbol, to_char(timestamp, 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') AS timestamp, close
      FROM candles WHERE timeframe = '{tf}'
    ) TO STDOUT WITH (FORMAT csv, HEADER);
    """
    txt = _run_sql(sql)
    c = pl.read_csv(io.StringIO(txt), schema_overrides={"symbol": pl.Utf8, "close": pl.Float64})
    c = c.with_columns(pl.col("timestamp").str.to_datetime(time_zone="UTC").alias("ts"))
    c = c.rename({"ts": "candle_time", "close": "candle_close"}).select(["symbol", "candle_time", "candle_close"])
    return c


def synth_label(sig: pl.DataFrame, candles: pl.DataFrame) -> pl.DataFrame:
    c = candles.sort(["symbol", "candle_time"])
    s = sig.sort("triggered_at")
    j = s.join_asof(
        c, by="symbol", left_on="triggered_at", right_on="candle_time",
        strategy="forward",
    ).filter(pl.col("candle_close").is_not_null())
    j = j.with_columns(
        ((pl.col("candle_close") - pl.col("entry_price")) / pl.col("entry_price") * 100.0).alias("fwd_return")
    )
    fee = FEE_BPS / 100.0
    j = j.with_columns(
        pl.when(pl.col("direction") == "SHORT").then(-pl.col("fwd_return")).otherwise(pl.col("fwd_return")).alias("fwd_dir")
    ).filter(pl.col("symbol") != "AXLUSDT").with_columns(pl.col("fwd_dir").clip(-CLIP, CLIP).alias("fwd_clip"))
    return j.with_columns(
        (pl.col("fwd_clip") - fee).alias("net"),
        pl.when(pl.col("fwd_clip") > THRESH).then(1)
        .when(pl.col("fwd_clip") < -THRESH).then(0)
        .otherwise(-1).alias("label"),
    )


VALID = ["LONG", "SHORT"]


def combo_sweep(lab: pl.DataFrame) -> pl.DataFrame:
    lab = lab.filter(pl.col("label") >= 0)
    if lab.height == 0:
        return pl.DataFrame()
    out = []
    for d in VALID:
        for tf in TFS:
            for vol in sorted(set(lab["regime_volatility"].fill_null("ALL").to_list())):
                for sess in sorted(set(lab["trading_session"].fill_null("ALL").to_list())):
                    sub = lab.filter(
                        (pl.col("direction") == d) &
                        (pl.col("timeframe") == tf) &
                        (pl.col("regime_volatility").fill_null("ALL") == vol) &
                        (pl.col("trading_session").fill_null("ALL") == sess)
                    )
                    if sub.height < 200:
                        continue
                    wr = (sub["label"] == 1).mean() * 100
                    out.append({
                        "direction": d, "timeframe": tf, "volatility": vol, "session": sess,
                        "n": sub.height, "win_rate": round(wr, 1), "avg_net": round(sub["net"].mean(), 4),
                    })
    return pl.DataFrame(out, schema={
        "direction": pl.Utf8, "timeframe": pl.Utf8, "volatility": pl.Utf8,
        "session": pl.Utf8, "n": pl.Int64, "win_rate": pl.Float64, "avg_net": pl.Float64,
    })


def oos_gate(lab: pl.DataFrame, top: dict) -> dict:
    lab = lab.filter(pl.col("label") >= 0).sort("triggered_at")
    if lab.height == 0:
        return {"oos_n": 0, "pass": False}
    cut = int(lab.height * (1 - OOS_FRAC))
    train = lab.slice(0, cut)
    test = lab.slice(cut, lab.height - cut)
    flt = ((pl.col("direction") == top["direction"]) &
           (pl.col("timeframe") == top["timeframe"]) &
           (pl.col("regime_volatility").fill_null("ALL") == top["volatility"]))
    tr = train.filter(flt)
    te = test.filter(flt)
    res = {"train_n": tr.height, "oos_n": te.height}
    if te.height >= 50:
        res.update({
            "train_wr": round((tr["label"] == 1).mean() * 100, 1) if tr.height else None,
            "oos_wr": round((te["label"] == 1).mean() * 100, 1),
            "oos_avg_net": round(te["net"].mean(), 4),
            "pass": ((te["label"] == 1).mean() * 100) > 55 and te["net"].mean() > 0,
        })
    else:
        res.update({"oos_wr": None, "oos_avg_net": None, "pass": False, "reason": f"oos_n<50 ({te.height})"})
    return res


# --- Obsidian persistence ----------------------------------------------------
def _frontmatter(created_iso: str, confidence: str) -> str:
    return (
        "---\n"
        f'title: "{created_iso} quant-pattern-mining loop"\n'
        "type: research\n"
        "status: active\n"
        f"created: {created_iso}\n"
        f"updated: {created_iso}\n"
        f"confidence: {confidence}\n"
        "tags:\n"
        "- quant-pattern-mining\n"
        "- sycode-trading\n"
        "- research-loop\n"
        "sources:\n"
        '- "postgres signal_journeys/candles via host psql 127.0.0.1:5432"\n'
        "owners:\n"
        "- jarvis\n"
        "---\n"
    )


def persist_note(run_ts: dt.datetime, run_sec: str) -> str:
    """Write/append the daily research note. Returns the note path.
    Preserves opening frontmatter when appending; initializes canonical
    frontmatter if the file does not exist. Never fails the run on vault error."""
    day = run_ts.strftime("%Y-%m-%d")
    path = os.path.join(VAULT_RESEARCH, f"{day}-quant-pattern-mining-loop.md")
    os.makedirs(VAULT_RESEARCH, exist_ok=True)
    heading = f"\n## Run {run_ts.strftime('%Y-%m-%dT%H:%MZ')} (no_agent loop)\n\n{run_sec}\n"
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            body = f.read()
        # preserve frontmatter; refresh `updated`
        if body.lstrip().startswith("---"):
            fm_end = body.find("---", 3)
            fm = body[:fm_end + 3]
            rest = body[fm_end + 3:]
            import re
            fm = re.sub(r"^updated:.*$", f"updated: {day}", fm, count=1, flags=re.M)
            with open(path, "w", encoding="utf-8") as f:
                f.write(fm + rest.rstrip() + "\n" + heading)
        else:  # no frontmatter — initialize it first
            with open(path, "w", encoding="utf-8") as f:
                f.write(_frontmatter(day, "low") + "\n# " + day + " quant-pattern-mining loop\n" + heading)
    else:
        with open(path, "w", encoding="utf-8") as f:
            f.write(_frontmatter(day, "low") + "\n# " + day + " quant-pattern-mining loop\n" + heading)
    return path


def main() -> int:
    run_ts = dt.datetime.now(dt.UTC)
    print(f"quant-pattern-mining-loop (read-only, no_agent) - start {run_ts.isoformat()}", flush=True)
    verdict = "NO_CURRENT_ENABLED_PROFITABLE_STRATEGY_PROVEN"
    run_lines = []
    try:
        sig = load_signals()
        n_sig = sig.height
        print(f"signals (majors, {TFS}, >= {EPOCH}): {n_sig}", flush=True)
        run_lines.append(f"- signals: {n_sig} (majors, {TFS}, >= {EPOCH})")
        if sig.height == 0:
            run_lines.append(f"- verdict: {verdict}")
            print("verdict:", verdict, flush=True)
            _persist(run_ts, run_lines, verdict)
            return 0
        frames = []
        for tf in TFS:
            can = load_candles(tf)
            frames.append(synth_label(sig.filter(pl.col("timeframe") == tf), can))
        lab = pl.concat(frames)
        lab = lab.filter(pl.col("candle_close").is_not_null() & pl.col("entry_price").is_not_null())
        n_lab = lab.height
        print(f"labelled full-population signals: {n_lab}", flush=True)
        run_lines.append(f"- labelled (full-population, synthetic forward labels): {n_lab}")
        combos = combo_sweep(lab)
        if combos.height == 0:
            run_lines.append("- no combo >= n=200. " + verdict)
            print("no combo >= n=200. verdict:", verdict, flush=True)
            _persist(run_ts, run_lines, verdict)
            return 0
        combos = combos.sort("avg_net", descending=True)
        print("TOP COMBOS (full-sample, by avg_net):", flush=True)
        print(combos.head(10), flush=True)
        top = combos.row(0, named=True)
        oos = oos_gate(lab, top)
        print("OOS GATE top combo:", json.dumps({**top, "oos": oos}, default=str), flush=True)
        if oos.get("pass"):
            verdict = (f"POCKET<{top['direction']}|{top['timeframe']}|{top['volatility']}|{top['session']}> "
                       f"n={top['n']} WR={top['win_rate']}% net={top['avg_net']}% "
                       f"OOS={oos['oos_n']}:{oos['oos_wr']}% net={oos['oos_avg_net']}%")
        print("verdict:", verdict, flush=True)
        run_lines.append(f"- verdict: {verdict}")
        # top-5 pocket summary
        run_lines.append("- top pockets (n>=200, by full-sample net):")
        for r in combos.head(5).to_dicts():
            run_lines.append(f"  - {r['direction']}|{r['timeframe']}|{r['volatility']}|{r['session']} "
                             f"n={r['n']} WR={r['win_rate']}% net={r['avg_net']}%")
        run_lines.append(f"- OOS gate (top combo): {json.dumps(oos, default=str)}")
        run_lines.append("- paper-only; no trades, no strategy_pool/strategies mutation, no trade_intents.")
        _persist(run_ts, run_lines, verdict)
    except Exception as e:
        import traceback
        traceback.print_exc()
        print("ERROR:", type(e).__name__, str(e)[:300], flush=True)
        run_lines.append(f"- ERROR: {type(e).__name__}: {str(e)[:200]}")
        _persist(run_ts, run_lines, verdict)
        return 2
    return 0


def _persist(run_ts: dt.datetime, run_lines: list, verdict: str):
    """Persist research note; vault failure must NOT fail the mining run."""
    conf = "high" if ("POCKET<" in verdict and "OOS=" in verdict) else "low"
    sec = "\n".join(run_lines)
    try:
        path = persist_note(run_ts, sec)
        print(f"NOTE_PERSISTED: {path}", flush=True)
    except Exception as e:
        print(f"NOTE_WRITE_FAILED (non-fatal): {type(e).__name__}: {str(e)[:200]}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
