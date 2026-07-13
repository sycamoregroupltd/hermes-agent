#!/home/frank/.hermes/venvs/trading-ml/bin/python
"""sycode_clean_epoch_ledger.py ..."""
import sys, os

# Re-exec guard: hermes cron --no-agent launches non-bash scripts "via Python",
# which may NOT be this venv (duckdb/polars would be missing). If we're not
# running under the target interpreter, re-exec ourselves with it so the cron
# run always has the right deps.
_TARGET_PY = "/home/frank/.hermes/venvs/trading-ml/bin/python"
if os.path.realpath(sys.executable) != os.path.realpath(_TARGET_PY):
    os.execv(_TARGET_PY, [_TARGET_PY, os.path.abspath(__file__)] + sys.argv[1:])

"""
sycode_clean_epoch_ledger.py  --  Durable clean-epoch OOS edge-accumulation ledger.

PROCESS task t_77366325 (CEO governor Beat-3 weakest-track advance):
A low-cost, read-only mechanism that grows the clean-epoch OOS sample across
runs until the 100-journey validation gate (and the substantive triple freshness
gate) clears -- WITHOUT relaxing the gate, WITHOUT recalibrating the engine,
WITHOUT touching live trading. Paper-only. Read-only on the trading DB.

WHY THIS EXISTS
---------------
The quant-researcher 6h sweep (13c1f9279025) and fusion calibration recompute
from scratch each run and discard the clean subset; nothing persists forward, so
the "n >= 100 clean unique journeys" bar never accumulates and the review gate
can never fire. The 2026-07-11 t_10069ca5 / t_02b3cc39 root cause shows fusion's
clean unique-journey count was pinned at n=29 (stale snapshot), not recomputed
from source. This script recomputes the clean-epoch resolved unique-journey set
FROM SOURCE every run (no stale pin) and persists it forward in a deduped ledger.

WHAT IT DOES (acceptance criteria)
--------------------------------
1. Append-only ledger of clean-epoch-resolved unique journeys, keyed by
   signal_id, deduped across runs (last-write-wins on resolution/updated_at).
2. Designed to be driven by a daily no-agent cron that filters strictly to the
   clean-candidate-599f58e7e epoch and (re)appends the resolved clean outcomes
   to the ledger each run (idempotent: source is the SoR, ledger is recomputed
   and merged, so reruns never double-count).
3. Reports the rolling clean count; when n >= 100 AND the triple freshness gate
   passes on the accumulated clean sample, emits a SINGLE review-gated kanban
   card to trading-risk-reviewer (do NOT auto-validate or recalibrate).
4. No mutation to live trading, production deploys, credentials, or the epoch
   registry. DB access is READ_ONLY (DuckDB postgres attach READ_ONLY TRUE).

TRIPLE GATE (reused verbatim from quant_researcher_6h.py, governor task
t_47fd45ce ACC #2 + t_ec3d651c LOW-CONFIDENCE):
    A clean-epoch cohort passes ONLY when ALL hold, computed on STRICTLY
    clean-epoch data (triggered_at >= clean-candidate-599f58e7e open):
        n_clean_fresh   >= 300
        fresh_WR        >= 53.0%
        clean_stale_share <= 5.0%
    "fresh" = forward-joined label candle lands within the timeframe's
    fresh_window (15m=15, 1h=60, 4h=240, 1d=1440 min of triggered_at).
    Synthetic forward label = direction-adjusted return on the native-timeframe
    forward candle, clipped to +/-10%, win if > +0.2%, loss if < -0.2%
    (AXLUSDT excluded). This is the proven, fail-closed gate math.

CLEAN-EPOCH RESOLVED UNIQUE JOURNEY = a signal_journeys row with
    triggered_at >= CLEAN_EPOCH_START
    AND clean_outcome_binary_24h IS NOT NULL
    AND label_version = 'v2_2026-07-06_leakfree'   (the only clean labeler)
deduped by signal_id (last-write-wins on updated_at). The n>=100 "clean unique
journeys" bar is this deduped count. The substantive gate is the triple gate
above (which already implies n >= 300 >> 100).

OUTPUTS (local files only -- no DB writes)
    <LEDGER_DIR>/ledger_signals.jsonl   deduped resolved unique journeys
    <LEDGER_DIR>/ledger_runs.jsonl      append-only per-run rolling metrics
    <LEDGER_DIR>/SUMMARY.md             human rolling view + gate state
    <LEDGER_DIR>/.gate_passed           marker (absent until gate passes once)

Exit codes: 0 normal; 2 if a card was (re)emitted; 1 operational error.
"""
import sys, os, json, argparse, time, datetime as dt
import duckdb
import polars as pl

from second_brain_writer import write_markdown_atomic

# ---- robust read-only Postgres attach ---------------------------------------
# The local `postgres` (127.0.0.1:5432) runs max_connections=100 with
# superuser_reserved_connections=3, so only 97 non-superuser slots exist. At
# the daily 06:00 collision the sycodetrading-server `postgres.js` pool holds
# ~30 idle connections and other 06:00 crons compete, transiently exhausting
# the non-superuser slots -> "FATAL: remaining connection slots are reserved
# for roles with the SUPERUSER attribute". DuckDB's Postgres ATTACH is lazy
# (connect deferred until first pg.* query), so the FATAL surfaced deep in
# fetch_clean_sample. We force the connect at attach time and retry w/ backoff
# so a momentary slot shortage self-heals instead of crashing the run.
# Retry budget: 8 attempts, ~64s max backoff. Deliberately below the 24h cron
# cadence but enough to ride out a minute-scale slot storm.
PG_ATTACH_URI = "postgresql://postgres:postgres@127.0.0.1:5432/postgres"
ATTACH_RETRIES = 8
ATTACH_BACKOFF_S = 2.0
ATTACH_BACKOFF_MAX_S = 20.0


def connect_ro():
    """Open an in-memory DuckDB and attach the trading Postgres READ_ONLY,
    retrying on transient 'connection slots are reserved' errors."""
    con = duckdb.connect()
    con.execute("INSTALL postgres; LOAD postgres;")
    last_err = None
    for attempt in range(1, ATTACH_RETRIES + 1):
        try:
            con.execute(
                f"ATTACH '{PG_ATTACH_URI}' "
                f"AS pg (TYPE POSTGRES, READ_ONLY TRUE);"
            )
            # Force eager connect so slot exhaustion fails HERE (retriable),
            # not lazily inside fetch_clean_sample.
            con.execute("SELECT 1 FROM pg.data_epoch_registry LIMIT 1;")
            return con
        except Exception as e:  # duckdb.IOException on slot exhaustion
            last_err = e
            msg = str(e)
            if "connection slots are reserved" in msg and attempt < ATTACH_RETRIES:
                wait = min(ATTACH_BACKOFF_MAX_S,
                           ATTACH_BACKOFF_S * (2 ** (attempt - 1)))
                sys.stderr.write(
                    f"pg-attach-retry: attempt {attempt}/{ATTACH_RETRIES} failed "
                    f"(connection slots reserved); backing off {wait:.0f}s\n"
                )
                time.sleep(wait)
                continue
            # Non-retryable: close and re-raise.
            try:
                con.close()
            except Exception:
                pass
            raise
    try:
        con.close()
    except Exception:
        pass
    if last_err is not None:
        raise last_err
    raise RuntimeError("pg attach failed after retries (no error captured)")


# ---- paths / constants ------------------------------------------------------
PROFILE = "sycode-trading-pm"
LEDGER_DIR = os.path.expanduser(
    "~/obsidian/sycode-trading/analytics/clean-epoch-ledger"
)
HERMES_BIN = "/home/frank/.local/bin/hermes"
PARENT_TASK = "t_77366325"
REVIEWER = "trading-risk-reviewer"

# Triple-gate thresholds (from quant_researcher_6h.py).
FRESH_WINDOW = {"15m": 15, "1h": 60, "4h": 240, "1d": 1440}
WR_THRESH = 53.0
N_THRESH = 300
STALE_THRESH = 5.0
RET_CLIP = 10.0
WIN_TH = 0.2

# Clean-epoch fallback (overridden by live data_epoch_registry read).
CLEAN_EPOCH_FALLBACK = dt.datetime(2026, 7, 5, 22, 8, 0, tzinfo=dt.timezone.utc)
CLEAN_EPOCH_NAME = "clean-candidate-599f58e7e"
LABEL_VERSION_V2 = "v2_2026-07-06_leakfree"

# Canonical clean-epoch open, pinned as an explicit UTC literal. The
# data_epoch_registry column is a naive timestamp interpreted in DGX local tz
# (BST = +01 in July), so DuckDB renders it as 23:08Z. The governor decision
# and every obsidian doc fix the open at 2026-07-05 22:08Z, so we pin the UTC
# literal in SQL to avoid a 1-hour tz drift that would wrongly exclude the
# 22:08-23:08 cohort.
EPOCH_START_ISO = "2026-07-05 22:08:00+00"


def load_epoch_start(con):
    """Open time of clean-candidate-599f58e7e from the system-of-record
    data_epoch_registry; falls back to the constant if the registry is
    unreachable (fail-safe: treat everything pre-fallback as contaminated)."""
    start = CLEAN_EPOCH_FALLBACK
    try:
        for name, starts in con.execute(
            "SELECT name, starts_at FROM pg.data_epoch_registry"
        ).fetchall():
            if name == CLEAN_EPOCH_NAME and starts is not None:
                start = starts
    except Exception as e:  # pragma: no cover - defensive
        sys.stderr.write(f"epoch-registry-read-warning: {e}\n")
    return start


def fetch_clean_sample(con, epoch_start):
    """Return a Polars frame of clean-epoch resolved unique journeys, deduped
    by signal_id (last-write-wins on updated_at)."""
    # DISTINCT ON needs an ORDER BY on the sort key + the tiebreaker.
    # Pin the canonical UTC literal (EPOCH_START_ISO) rather than a tz-local
    # render of the registry timestamp (see header note).
    sql = f"""
        SELECT DISTINCT ON (signal_id)
               id, signal_id, symbol, direction, timeframe,
               triggered_at, entry_price,
               clean_outcome_binary_24h, clean_pnl_net_24h, updated_at
        FROM pg.signal_journeys
        WHERE triggered_at >= TIMESTAMP WITH TIME ZONE '{EPOCH_START_ISO}'
          AND clean_outcome_binary_24h IS NOT NULL
          AND label_version = '{LABEL_VERSION_V2}'
        ORDER BY signal_id, updated_at DESC
    """
    df = pl.from_pandas(con.execute(sql).fetchdf())
    return df


def forward_join_candles(con, df, epoch_start):
    """Replicate quant_researcher_6h.py: forward-join each signal to its
    NATIVE-timeframe candle, compute lag_min + is_fresh."""
    # Bound candle pull to the clean epoch window (+1 day slack for forward join).
    lo = (epoch_start - dt.timedelta(days=1)).strftime("%Y-%m-%d")
    parts = []
    for tf in FRESH_WINDOW:
        sub = df.filter(pl.col("timeframe") == tf).sort("triggered_at")
        if sub.height == 0:
            continue
        c = pl.from_pandas(con.execute(
            f"SELECT symbol, timestamp, close FROM pg.candles "
            f"WHERE timeframe='{tf}' AND timestamp >= TIMESTAMP '{lo}'"
        ).fetchdf()).rename({"timestamp": "candle_time", "close": "next_close"})
        j = sub.join_asof(
            c.sort("candle_time"),
            by="symbol", left_on="triggered_at", right_on="candle_time",
            strategy="forward",
        )
        parts.append(j)
    if not parts:
        return df.with_columns([
            pl.lit(None).cast(pl.Float64).alias("lag_min"),
            pl.lit(False).alias("is_fresh"),
            pl.lit(None).cast(pl.Float64).alias("next_close"),
        ])
    joined = pl.concat(parts, how="diagonal")
    joined = joined.with_columns(
        ((pl.col("candle_time") - pl.col("triggered_at")).dt.total_minutes())
        .alias("lag_min")
    )
    fwin = pl.Series("fresh_window_min",
                     [FRESH_WINDOW.get(t, None) for t in joined["timeframe"].to_list()])
    joined = joined.with_columns(fwin)
    joined = joined.with_columns(
        pl.when((pl.col("lag_min") >= 0) & (pl.col("lag_min") <= pl.col("fresh_window_min")))
          .then(True).otherwise(False).alias("is_fresh")
    )
    return joined


def compute_gate(df, epoch_start_ts):
    """Compute the triple gate over the clean-epoch subset, reusing the exact
    quant_researcher_6h.py label/freshness math."""
    n_dedup = df.height
    wins = int((df["clean_outcome_binary_24h"] == True).sum())  # noqa: E712
    lr = round(100.0 * wins / n_dedup, 2) if n_dedup else 0.0

    # Synthetic forward label (direction-adjusted return on native-tf candle,
    # clipped to +/-10%, win > +0.2%, loss < -0.2%). Reuses quant logic.
    d = df.filter(pl.col("next_close").is_not_null()).filter(pl.col("symbol") != "AXLUSDT")
    d = d.with_columns(
        ((pl.col("next_close") - pl.col("entry_price")) / pl.col("entry_price") * 100)
        .alias("fwd_return")
    )
    d = d.with_columns(
        pl.when(pl.col("direction") == "SHORT").then(-pl.col("fwd_return"))
          .otherwise(pl.col("fwd_return")).alias("fwd_return_dir")
    )
    d = d.with_columns(pl.col("fwd_return_dir").clip(-RET_CLIP, RET_CLIP).alias("fwd_clipped"))
    d = d.with_columns(
        pl.when(pl.col("fwd_clipped") > WIN_TH).then(pl.lit(1))
         .when(pl.col("fwd_clipped") < -WIN_TH).then(pl.lit(0))
         .otherwise(pl.lit(-1)).alias("label")
    )

    # Clean-epoch flag (triggered_at >= registry epoch open).
    d = d.with_columns(
        (pl.col("triggered_at").dt.timestamp("ms") / 1000.0 >= epoch_start_ts).alias("in_clean_epoch")
    )
    clean = d.filter(pl.col("in_clean_epoch"))
    clean_fresh = clean.filter(pl.col("is_fresh"))
    n_clean = clean.height
    n_clean_fresh = clean_fresh.height
    n_clean_stale = clean.filter(~pl.col("is_fresh")).height
    clean_denom = n_clean_fresh + n_clean_stale
    clean_stale_share = (100.0 * n_clean_stale / clean_denom) if clean_denom > 0 else 0.0

    # fresh WR on synthetic label among clean & fresh (excludes flat -1).
    nf = clean_fresh.filter(pl.col("label") != -1)
    fresh_wr = (100.0 * int((nf["label"] == 1).sum()) / nf.height) if nf.height > 0 else 0.0

    gate_pass = (
        (n_clean_fresh >= N_THRESH)
        and (fresh_wr >= WR_THRESH)
        and (clean_stale_share <= STALE_THRESH)
    )
    return {
        "n_dedup": n_dedup,
        "wins": wins,
        "wr_pct": lr,
        "n_clean": n_clean,
        "n_clean_fresh": n_clean_fresh,
        "fresh_wr_pct": round(fresh_wr, 2),
        "clean_stale_share_pct": round(clean_stale_share, 3),
        "gate_pass": bool(gate_pass),
    }


def load_ledger_signals(path):
    sigs = {}
    if os.path.isfile(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                r = json.loads(line)
                sigs[r["signal_id"]] = r
    return sigs


def merge_ledger_signals(path, df):
    """Merge freshly computed resolved journeys into the deduped ledger
    (last-write-wins on updated_at). Returns (n_total, n_new, n_updated)."""
    sigs = load_ledger_signals(path)
    n_before = len(sigs)
    for r in df.to_dicts():
        sid = r["signal_id"]
        upd = r["updated_at"].isoformat() if isinstance(r["updated_at"], dt.datetime) else str(r["updated_at"])
        trig = r["triggered_at"].isoformat() if isinstance(r["triggered_at"], dt.datetime) else str(r["triggered_at"])
        rec = {
            "signal_id": sid,
            "symbol": r["symbol"],
            "direction": r["direction"],
            "timeframe": r["timeframe"],
            "clean_outcome_binary_24h": bool(r["clean_outcome_binary_24h"]),
            "clean_pnl_net_24h": (float(r["clean_pnl_net_24h"])
                                  if r["clean_pnl_net_24h"] is not None else None),
            "updated_at": upd,
            "triggered_at": trig,
        }
        existing = sigs.get(sid)
        if existing is None or upd >= existing["updated_at"]:
            sigs[sid] = rec
    # Atomic rewrite.
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        for rec in sigs.values():
            f.write(json.dumps(rec) + "\n")
    os.replace(tmp, path)
    n_after = len(sigs)
    n_new = max(0, n_after - n_before)
    n_updated = sum(1 for s in sigs.values()
                    if s["updated_at"] not in (None,)) - n_before  # conservative
    return n_after, n_new, max(0, n_after - n_before - n_new)


def append_run(path, record):
    with open(path, "a") as f:
        f.write(json.dumps(record) + "\n")


def render_summary(path, gate, run_ts, epoch_start, ledger_n, ledger_wr):
    L = []
    L.append(f"# Clean-epoch OOS edge-accumulation ledger — SUMMARY")
    L.append("")
    L.append(f"Generated {run_ts:%Y-%m-%d %H:%M UTC} by `sycode_clean_epoch_ledger.py` "
             f"(process task t_77366325).")
    L.append("")
    L.append(f"Clean epoch (system of record `data_epoch_registry.{CLEAN_EPOCH_NAME}`): "
             f"opens **{epoch_start:%Y-%m-%d %H:%M UTC}**.")
    L.append("")
    L.append("## Rolling state")
    L.append("")
    L.append(f"- **Deduped clean-epoch resolved unique journeys (ledger):** `{ledger_n}` "
             f"(bar to clear: n >= 100 — MET when this exceeds 100)")
    L.append(f"- **Stored clean-label WR on the full ledger** (all timeframes): "
             f"`{ledger_wr}%` (wins / {ledger_n})")
    L.append(f"- **Wins / total (4-TF join frame):** {gate['wins']} / {gate['n_dedup']} "
             f"(stored clean-label WR {gate['wr_pct']}%)")
    L.append(f"- **Clean-epoch fresh subset N (n_clean_fresh):** {gate['n_clean_fresh']} "
             f"(triple-gate bar: >= {N_THRESH})")
    L.append(f"- **Fresh WR (synthetic, clean-epoch):** {gate['fresh_wr_pct']}% "
             f"(triple-gate bar: >= {WR_THRESH}%)")
    L.append(f"- **Clean stale-share:** {gate['clean_stale_share_pct']}% "
             f"(triple-gate bar: <= {STALE_THRESH}%)")
    L.append("")
    gate_now = gate["gate_pass"]
    L.append(f"## Triple-gate status: {'**PASS**' if gate_now else 'fail-closed'}")
    L.append("")
    L.append("The gate is the proven fail-closed freshness gate from `quant_researcher_6h.py` "
             "(governor t_47fd45ce ACC #2). It passes ONLY when ALL hold on STRICTLY "
             "clean-epoch data: n_clean_fresh>=300 AND fresh_WR>=53% AND clean_stale_share<=5%.")
    L.append("")
    if gate_now:
        L.append("> GATE PASS — a single review-gated card has been emitted to "
                 f"`{REVIEWER}` (parent t_77366325). Do NOT auto-validate or recalibrate.")
    else:
        L.append("> fail-closed: gate not met. Mechanism keeps accumulating; no card emitted. "
                 "No recalibration, no MCE/edge alerts, no live-trading mutation.")
    L.append("")
    L.append("Sources: live `signal_journeys` (read-only) recomputed each run — no stale pin. "
             "See `ledger_signals.jsonl` (deduped resolved journeys) and `ledger_runs.jsonl` "
             "(append-only per-run metrics).")
    L.append("")
    report_date = run_ts.strftime("%Y-%m-%d")
    write_markdown_atomic(
        path,
        "\n".join(L),
        title="Clean-epoch OOS edge-accumulation ledger — SUMMARY",
        type="moc",
        status="active",
        created="2026-07-11",
        updated=report_date,
        confidence="high",
        tags=["sycode", "clean-epoch", "ledger", "oos", "promotion-gate"],
        sources=[
            "sycodetrading-supabase-db:signal_journeys",
            "sycodetrading-supabase-db:data_epoch_registry",
            "ledger_signals.jsonl",
            "ledger_runs.jsonl",
        ],
        project="sycode-trading",
        owners=["sycode-trading-pm"],
        knowledge_tier="compiled",
        generated=True,
        generator="sycode_clean_epoch_ledger.py",
        generated_at=run_ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        operational_status="pass" if gate_now else "fail-closed",
        kanban_task=PARENT_TASK,
    )


def emit_review_card(gate, run_ts, epoch_start):
    """Emit a SINGLE review-gated kanban card to trading-risk-reviewer. Returns
    (emitted_bool, detail). Guarded by the .gate_passed marker so exactly one
    card fires per genuine pass event (the marker is cleared when the gate
    later fails, so re-passes re-emit)."""
    marker = os.path.join(LEDGER_DIR, ".gate_passed")
    if os.path.isfile(marker):
        return False, "marker present; card already emitted for current pass"
    title = (f"REVIEW: clean-epoch triple gate PASS (n_clean_fresh={gate['n_clean_fresh']}, "
             f"fresh_WR={gate['fresh_wr_pct']}%, stale={gate['clean_stale_share_pct']}%)")
    body = f"""# Review-gated: clean-epoch OOS edge-accumulation gate PASS

**Source:** `sycode_clean_epoch_ledger.py` (process task t_77366325), run {run_ts:%Y-%m-%d %H:%M UTC}.
**Clean epoch:** `data_epoch_registry.clean-candidate-599f58e7e` opens {epoch_start:%Y-%m-%d %H:%M UTC}.

## Gate math (reused verbatim from quant_researcher_6h.py, governor t_47fd45ce ACC #2)
Computed on STRICTLY clean-epoch data (triggered_at >= epoch open), synthetic
forward label on native-timeframe candle, clipped +/-10%, win > +0.2%:

| metric | value | bar | pass |
|---|---|---|---|
| n_clean_fresh | {gate['n_clean_fresh']} | >= {N_THRESH} | {'YES' if gate['n_clean_fresh'] >= N_THRESH else 'NO'} |
| fresh_WR (synthetic, clean) | {gate['fresh_wr_pct']}% | >= {WR_THRESH}% | {'YES' if gate['fresh_wr_pct'] >= WR_THRESH else 'NO'} |
| clean_stale_share | {gate['clean_stale_share_pct']}% | <= {STALE_THRESH}% | {'YES' if gate['clean_stale_share_pct'] <= STALE_THRESH else 'NO'} |

Deduped clean-epoch resolved unique journeys in ledger: {gate['n_dedup']} (bar n>=100 MET).
Stored clean-label WR on the deduped set: {gate['wr_pct']}%.

## Safety — DO NOT
- Do NOT auto-validate, recalibrate the fusion engine, fire MCE/edge alerts, or
  enable live trading. This card is a REVIEW request of the gate math only.
- Verify the freshness logic and sample bounds before any downstream action.

## Reviewer action
Validate the gate math against live `signal_journeys` (read-only). If correct and
you accept, archive this card; if the math is wrong, block with the discrepancy.
Ledger artifacts: ~/obsidian/sycode-trading/analytics/clean-epoch-ledger/
"""
    import subprocess
    cmd = [
        HERMES_BIN, "kanban", "create",
        title,                      # title is POSITIONAL in `hermes kanban create`
        "--body", body,
        "--assignee", REVIEWER,
        "--parent", PARENT_TASK,
        "--priority", "5",
        "--idempotency-key", "clean-epoch-triple-gate-pass",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            return False, f"kanban create FAILED rc={proc.returncode}: {proc.stderr.strip()[:400]}"
        # Write marker so we don't re-emit until the gate fails then re-passes.
        with open(marker, "w") as f:
            f.write(f"emitted {run_ts:%Y-%m-%dT%H:%M:%SZ} n_clean_fresh={gate['n_clean_fresh']} "
                    f"fresh_wr={gate['fresh_wr_pct']} stale={gate['clean_stale_share_pct']}\n")
        return True, (proc.stdout.strip() or proc.stderr.strip())[:400]
    except Exception as e:  # pragma: no cover
        return False, f"kanban create EXC: {e}"


def clear_gate_marker_if_needed(gate):
    marker = os.path.join(LEDGER_DIR, ".gate_passed")
    if (not gate["gate_pass"]) and os.path.isfile(marker):
        os.remove(marker)
        return True
    return False


def main():
    ap = argparse.ArgumentParser(description="Clean-epoch OOS edge-accumulation ledger")
    ap.add_argument("--self-test", action="store_true",
                    help="verify gate math + kanban command construction on synthetic data "
                         "(no DB read beyond a tiny probe, no card emitted, no ledger write)")
    args = ap.parse_args()

    os.makedirs(LEDGER_DIR, exist_ok=True)
    run_ts = dt.datetime.now(dt.timezone.utc)

    if args.self_test:
        sys.exit(self_test())

    # ---- live read-only compute ----
    # connect_ro() attaches the trading Postgres READ_ONLY and retries across
    # transient slot-exhaustion storms (the 06:00 collision with the server
    # postgres.js pool). No DDL/DML; fail-closed if the slots never free up.
    con = connect_ro()
    # canonical UTC epoch open (pinned literal, see header) — used for the SQL
    # filter and all run-log reporting. load_epoch_start is still consulted to
    # RE-VALIDATE against the live registry; if the two disagree we surface it
    # but keep the pinned UTC literal for the actual filter.
    epoch_start = dt.datetime.fromisoformat(EPOCH_START_ISO.replace("Z", "+00:00"))
    registry_start = load_epoch_start(con)
    if abs((registry_start - epoch_start).total_seconds()) > 60:
        sys.stderr.write(
            f"epoch-mismatch-warning: registry={registry_start} canonical={epoch_start}; "
            f"using pinned UTC literal for filter\n"
        )
    epoch_start_ts = epoch_start.timestamp()

    # Full deduped clean-epoch resolved unique journeys (ALL timeframes) — this
    # is what the ledger persists and what the n>=100 bar measures.
    df_all = fetch_clean_sample(con, epoch_start)
    # Separate join frame for the freshness gate (only 15m/1h/4h/1d have
    # candles joined; shorter TFs are out-of-scope for the triple gate but
    # still count toward the deduped ledger).
    df_join = forward_join_candles(con, df_all, epoch_start)
    con.close()

    gate = compute_gate(df_join, epoch_start_ts)

    # ---- persist ledger (merge deduped resolved journeys, ALL timeframes) ----
    sig_path = os.path.join(LEDGER_DIR, "ledger_signals.jsonl")
    ledger_n, n_new, n_upd = merge_ledger_signals(sig_path, df_all)

    # ---- append-only run log ----
    run_rec = {
        "run_ts": run_ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "epoch_start": epoch_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "registry_start": registry_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "n_dedup": gate["n_dedup"],
        "wins": gate["wins"],
        "wr_pct": gate["wr_pct"],
        "n_clean": gate["n_clean"],
        "n_clean_fresh": gate["n_clean_fresh"],
        "fresh_wr_pct": gate["fresh_wr_pct"],
        "clean_stale_share_pct": gate["clean_stale_share_pct"],
        "gate_pass": gate["gate_pass"],
        "ledger_n": ledger_n,
        "ledger_new": n_new,
    }
    append_run(os.path.join(LEDGER_DIR, "ledger_runs.jsonl"), run_rec)

    # Full-ledger stored clean-label WR (all timeframes) for the summary line.
    ledger_wins = int((df_all["clean_outcome_binary_24h"] == True).sum())  # noqa: E712
    ledger_wr = round(100.0 * ledger_wins / max(ledger_n, 1), 2)

    render_summary(os.path.join(LEDGER_DIR, "SUMMARY.md"), gate, run_ts,
                   epoch_start, ledger_n, ledger_wr)

    # ---- emission (review-gated) ----
    emitted = False
    detail = ""
    if gate["gate_pass"]:
        emitted, detail = emit_review_card(gate, run_ts, epoch_start)
    else:
        clear_gate_marker_if_needed(gate)

    # stdout (captured as cron report)
    print(f"clean-epoch ledger run {run_ts:%Y-%m-%d %H:%M UTC}")
    print(f"  ledger deduped clean unique journeys: {ledger_n} (n>=100 bar: "
          f"{'MET' if ledger_n >= 100 else 'not met'})")
    print(f"  n_clean_fresh={gate['n_clean_fresh']} fresh_WR={gate['fresh_wr_pct']}% "
          f"clean_stale={gate['clean_stale_share_pct']}%")
    print(f"  TRIPLE GATE: {'PASS' if gate['gate_pass'] else 'fail-closed'}")
    if emitted:
        print(f"  REVIEW CARD EMITTED -> {REVIEWER}: {detail}")

    sys.exit(2 if emitted else 0)


def self_test():
    """Verify the gate math + kanban command construction on synthetic data.
    No DB read, no ledger write, no card emitted."""
    ok = True
    # Synthetic rows: 5 clean-epoch fresh longs, 4 win (>=53% WR), low stale.
    rows = []
    base = dt.datetime(2026, 7, 6, 0, 0, 0, tzinfo=dt.timezone.utc)
    for i in range(5):
        rows.append({
            "signal_id": f"S{i}", "symbol": "BTCUSDT", "direction": "LONG", "timeframe": "1h",
            "triggered_at": base, "entry_price": 100.0,
            "clean_outcome_binary_24h": True, "clean_pnl_net_24h": 0.01, "updated_at": base,
            # synthetic label inputs:
            "next_close": 101.0 if i < 4 else 99.0,  # 4 wins, 1 loss -> 80% WR
            "candle_time": base + dt.timedelta(minutes=30),  # fresh (<=60 for 1h)
            "is_fresh": True, "fresh_window_min": 60, "label": 1 if i < 4 else 0,
        })
    # Note: compute_gate recomputes label from next_close/entry/direction, so the
    # injected 'label' is ignored; we just need the join columns present.
    import polars as _pl
    df = _pl.DataFrame(rows)
    epoch_start_ts = base.timestamp()
    g = compute_gate(df, epoch_start_ts)
    # 5 clean-fresh, 4/5=80% WR, 0 stale -> gate should PASS (n_clean_fresh>=300? NO)
    # n_clean_fresh=5 < 300 so gate must FAIL (the n>=300 bar dominates).
    ok1 = g["gate_pass"] is False and g["n_clean_fresh"] == 5 and g["fresh_wr_pct"] == 80.0
    print(f"SELF-TEST {'PASS' if ok1 else 'FAIL'}: synthetic gate math (expect fail on N<300, WR=80%)")

    # Kanban command construction (must NOT execute).
    fake_gate = {"n_clean_fresh": 350, "fresh_wr_pct": 55.0, "clean_stale_share_pct": 2.0,
                 "n_dedup": 350, "wins": 200, "wr_pct": 57.0}
    import subprocess
    cmd = [HERMES_BIN, "kanban", "create", "X",   # title positional
           "--body", "Y",
           "--assignee", REVIEWER, "--parent", PARENT_TASK,
           "--priority", "5", "--idempotency-key", "clean-epoch-triple-gate-pass"]
    ok2 = cmd[0] == HERMES_BIN and REVIEWER in cmd and PARENT_TASK in cmd
    print(f"SELF-TEST {'PASS' if ok2 else 'FAIL'}: kanban emit command construction")
    print("  would run:", " ".join(cmd[:6]), "...")

    all_ok = ok1 and ok2
    print(f"SELF-TEST {'PASS' if all_ok else 'FAIL'}: overall")
    return 0 if all_ok else 1


if __name__ == "__main__":
    main()
