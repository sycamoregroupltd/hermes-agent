#!/usr/bin/env python3
"""
PIT training-time feature-distribution baseline builder
=======================================================
Builds the *persisted* reference distribution for the per-feature PSI drift
check that `~/.hermes/scripts/drift_monitor.py` needs and does not have.

Why this file exists
--------------------
`fetch_training_features()` in drift_monitor.py is dead code: nothing calls it,
so `compute_feature_psi()` always takes its *self-split* branch and the report
compares the live window against itself ("newest 60% vs older 40%") while
calling the result a train-vs-live drift.  Decision card t_94d36612 (trading-
ml-ensemble) ruled that the honest fix is a **persisted PIT training-time
per-feature distribution, per model version, produced from signal-time fields
only** — this script produces that artifact.

PIT (point-in-time) discipline
------------------------------
* Every feature is projected from a `signal_journeys` field that is written at
  trigger time.  `signal_journeys.indicators` is an **immutable root column**
  (`server/src/infra/persistence/signalJourneysImmutableColumns.ts`,
  `persistentWriteQueue.processing.immutableScope.test.ts`), i.e. a signal-time
  snapshot that post-trigger writes cannot overwrite.
* Derived features are computed with the *exact* formulas of the trainer
  (`tools/mfe-first-model/train_gpu.py::_engineer_features_non_autoregressive`),
  never re-invented.
* Distribution-dependent derived features (`bb_squeeze`) freeze their threshold
  from the reference window and persist it, so the live side is compared
  against the training-time threshold rather than a moving one.
* Features with no signal-time projection (autoregressive / rolling /
  trigger-pattern aggregates) are recorded under `unresolvable` with a reason.
  They are NEVER silently dropped: an unmonitored key must not read as
  "no drift".

Trainer-side reference (dataset-info/1)
---------------------------------------
Reconstruction can only recover what the *current* warehouse schema still
exposes.  A feature the trainer built inside its own pipeline (rolling win
rates, lags, pattern aggregates) has no such projection and used to land in
`unresolvable` permanently.  Trainers now persist the per-feature distribution
of the matrix they actually consumed (`tools/training-telemetry/dataset_info.py`,
written next to the model metadata).  When that file is present this builder
folds the gap into `trainer_reference`:

* only feature names the reconstruction could NOT reach are filled;
* a file whose declared model version disagrees with the metadata is refused,
  never merged;
* a filled entry is recorded with `live_projection: null` and
  `monitorable: false`, so it can never enter `features`, the PSI denominator,
  or the fail-closed verdict.  A persisted reference is a reference, not a
  monitoring capability — the live side is still missing and is still reported
  as missing.

Usage
-----
    # build baselines for every model metadata JSON in the deployed model dir
    python3 build_training_baseline.py --models-dir /home/frank/sycode-trading/server/models

    # one model, declared window
    python3 build_training_baseline.py --model-json .../mfe_first_v10_catboost.json \
        --since 2025-01-01 --until 2026-02-18 --window-basis declared

    # DB-free logic test (PSI, coverage gate, determinism)
    python3 build_training_baseline.py --self-test

    # honest train-vs-live read against the persisted artifact
    python3 build_training_baseline.py --check-live 30 --model-json .../max_drawdown_catboost.json

PAPER-MODE / READ-ONLY: every SQL statement runs in a read-only transaction
(`default_transaction_read_only=on`) with a statement timeout.  Nothing here
writes to the trading database, and nothing here touches the live trading path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA_VERSION = "drift-baseline/1"
DEFAULT_MODELS_DIR = "/home/frank/sycode-trading/server/models"
DEFAULT_ENV_FILE = "/home/frank/sycode-trading/server/.env"
DEFAULT_OUT_DIR = str(Path(__file__).resolve().parent / "baselines")
PSI_THRESHOLD = 0.25
DEFAULT_COVERAGE_GATE = 0.95
DEFAULT_BINS = 10

# --------------------------------------------------------------------------
# signal-time projection: model feature name -> (canonical key, projection sql)
# The projection strings are the *audit trail*; live values are computed in
# Python from the raw envelope by `compute_features()` so the mapping is
# testable without a database.
# --------------------------------------------------------------------------
JSONB = "sj.indicators"

# canonical raw key -> how to read it out of the row envelope
RAW_SOURCES = {
    "trigger_score": "trigger_score",
    "rsi14": "ind.rsi14|ind.rsi|ind.RSI",
    "macd_value": "ind.macd.value",
    "macd_signal": "ind.macd.signal",
    "macd_histogram": "ind.macd.histogram",
    "atr_percent": "ind.atrPercent|ind.atr_percent|ind.core.atrPercent",
    "atr14": "ind.atr14|ind.core.atr",
    "adx": "ind.adx|ind.adx14|ind.core.adx",
    "plus_di": "ind.plusDI|ind.core.plusDI",
    "minus_di": "ind.minusDI|ind.core.minusDI",
    "volume_z20": "ind.volumeZ20|ind.volume_z20|ind.core.volumeZ20",
    "trend_strength": "norm100(ind.trendStrength)",
    "signal_quality": "norm100(ind.signalQuality)",
    "ema34_slope": "ind.ema34Slope",
    "ema_spread": "ind.emaSpread",
    "ema12": "ind.ema12",
    "ema26": "ind.ema26",
    "bollinger_width": "ind.bollingerBands.width|ind.bollingerWidth",
    "bb_upper": "ind.bollingerBands.upper",
    "bb_lower": "ind.bollingerBands.lower",
    "confluence_score_total": "ind.confluenceScore|ind.confluenceScoreTotal|ind.confluence_score_total",
    "entry_price": "entry_price",
    "suggested_stop_loss": "suggested_stop_loss",
    "suggested_take_profit": "suggested_take_profit",
    "hour_utc": "hour_utc",
    "day_of_week": "day_of_week",
}

SQL_EXPR = {
    "trigger_score": "sj.trigger_score::float",
    "rsi14": "COALESCE((sj.indicators->>'rsi14')::float, (sj.indicators->>'rsi')::float, (sj.indicators->>'RSI')::float)",
    "macd_value": "(sj.indicators->'macd'->>'value')::float",
    "macd_signal": "(sj.indicators->'macd'->>'signal')::float",
    "macd_histogram": "(sj.indicators->'macd'->>'histogram')::float",
    "atr_percent": "COALESCE((sj.indicators->>'atrPercent')::float, (sj.indicators->>'atr_percent')::float)",
    "atr14": "(sj.indicators->>'atr14')::float",
    "adx": "(sj.indicators->>'adx')::float",
    "plus_di": "(sj.indicators->>'plusDI')::float",
    "minus_di": "(sj.indicators->>'minusDI')::float",
    "volume_z20": "(sj.indicators->>'volumeZ20')::float",
    "trend_strength": "CASE WHEN (sj.indicators->>'trendStrength')::float <= 1 THEN (sj.indicators->>'trendStrength')::float * 100 ELSE (sj.indicators->>'trendStrength')::float END",
    "signal_quality": "CASE WHEN (sj.indicators->>'signalQuality')::float <= 1 THEN (sj.indicators->>'signalQuality')::float * 100 ELSE (sj.indicators->>'signalQuality')::float END",
    "ema34_slope": "(sj.indicators->>'ema34Slope')::float",
    "ema_spread": "(sj.indicators->>'emaSpread')::float",
    "ema12": "(sj.indicators->>'ema12')::float",
    "ema26": "(sj.indicators->>'ema26')::float",
    "bollinger_width": "COALESCE((sj.indicators->'bollingerBands'->>'width')::float, (sj.indicators->>'bollingerWidth')::float)",
    "bb_upper": "(sj.indicators->'bollingerBands'->>'upper')::float",
    "bb_lower": "(sj.indicators->'bollingerBands'->>'lower')::float",
    "confluence_score_total": "COALESCE((sj.indicators->>'confluenceScore')::float, (sj.indicators->>'confluenceScoreTotal')::float)",
    "entry_price": "sj.entry_price::float",
    "suggested_stop_loss": "sj.suggested_stop_loss::float",
    "suggested_take_profit": "sj.suggested_take_profit::float",
    "hour_utc": "sj.hour_utc::float",
    "day_of_week": "sj.day_of_week::float",
}

# model feature name -> canonical raw key (aliases only; no new maths)
FEATURE_ALIASES = {
    "rsi_14": "rsi14",
    "rsi": "rsi14",
    "volume_z_20": "volume_z20",
    "volumez20": "volume_z20",
    "atrpercent": "atr_percent",
    "trendstrength": "trend_strength",
    "signalquality": "signal_quality",
    "confluence_score": "confluence_score_total",
    "confluencescoretotal": "confluence_score_total",
    "bb_width": "bollinger_width",
    "bollinger_band_width": "bollinger_width",
    "ema_12": "ema12",
    "ema_26": "ema26",
    "plusdi": "plus_di",
    "minusdi": "minus_di",
    "ema34slope": "ema34_slope",
    "emaspread": "ema_spread",
}

# derived features whose formula is copied from the trainer
DERIVED = {
    "rsi_distance_50": ("rsi14", "rsi - 50  (train_gpu.py L179)"),
    "rsi_overbought": ("rsi14", "(rsi > 70) as float  (train_gpu.py L180)"),
    "rsi_oversold": ("rsi14", "(rsi < 30) as float  (train_gpu.py L181)"),
    "macd_histogram_sign": ("macd_histogram", "sign(histogram)  (train_gpu.py L187)"),
    "macd_cross_strength": ("macd_value,macd_signal", "|value - signal|  (train_gpu.py L194)"),
    "bb_position": ("bb_upper,bb_lower,entry_price", "(entry - lower) / (upper - lower)  (train_gpu.py L208)"),
    "bb_squeeze": ("bollinger_width", "width < reference p20 (frozen)  (train_gpu.py L214)"),
    "atr_x_trend": ("atr_percent,trend_strength", "atr * trend  (train_gpu.py L232)"),
    "rsi_x_trend": ("rsi14,trend_strength", "rsi * trend  (train_gpu.py L233)"),
    "volume_x_trend": ("volume_z20,trend_strength", "volume_z20 * trend  (train_gpu.py L234)"),
    "trigger_x_confluence": ("trigger_score,confluence_score_total", "trigger * confluence  (train_gpu.py L235)"),
    "quality_x_confidence": ("signal_quality,confluence_score_total", "quality * confluence  (train_gpu.py L236)"),
    "hour_sin": ("hour_utc", "sin(2*pi*hour/24)  (train_gpu.py L246)"),
    "hour_cos": ("hour_utc", "cos(2*pi*hour/24)  (train_gpu.py L247)"),
    "day_sin": ("day_of_week", "sin(2*pi*day/7)  (train_gpu.py L248)"),
    "day_cos": ("day_of_week", "cos(2*pi*day/7)  (train_gpu.py L249)"),
    "rr_ratio": ("entry_price,suggested_stop_loss,suggested_take_profit", "reward/risk  (train_gpu.py L261)"),
    "sl_distance_pct": ("entry_price,suggested_stop_loss", "risk/entry*100  (train_gpu.py L262)"),
    "tp_distance_pct": ("entry_price,suggested_take_profit", "reward/entry*100  (train_gpu.py L263)"),
}

# categorical features encoded by the model metadata's ordinal_encoders
ORDINAL_FEATURES = ("direction", "timeframe", "trading_session")

UNRESOLVABLE_PREFIXES = (
    ("mfe_rate_", "autoregressive rolling win-rate over prior journey outcomes"),
    ("mfe_ewm_", "autoregressive exponentially-weighted outcome rate"),
    ("mfe_lag_", "autoregressive lagged outcome (prior journeys)"),
    ("mfe_streak", "autoregressive outcome streak state"),
    ("mfe_momentum", "autoregressive momentum of outcome rates"),
    ("mfe_std_", "autoregressive rolling std of outcomes"),
    ("mfe_roc", "autoregressive rate-of-change of outcomes"),
    ("mfe_first_rate", "autoregressive rolling target rate"),
    ("mfe_duration", "post-outcome realised excursion (label-side, not signal-time)"),
    ("pattern_", "aggregate over sj.trigger_patterns; no persisted signal-time projection"),
    ("trigger_pattern_", "aggregate over sj.trigger_patterns; no persisted signal-time projection"),
)
UNRESOLVABLE_EXACT = {
    "fast_validation_confidence": "removed from the trainer as a future-data leak (validation runs after prediction, train_gpu.py L229)",
    "symbol": "target-encoded categorical; encoder state not persisted in metadata",
    "trend": "derived regime categorical; regime labelling is not a signal-time field on the journey",
    "volatility_level": "derived regime categorical; regime labelling is not a signal-time field on the journey",
    "volatility_regime": "derived regime categorical; regime labelling is not a signal-time field on the journey",
    "mfe_first": "the training target itself, not a feature",
    "win_rate": "target-side statistic",
    "symbol_win_rate_50": "autoregressive per-symbol target statistic",
    "regime_score": "derived regime score; not a signal-time journey field",
    "market_fear_greed": "market-context feature joined by calendar day in the exporter, not on the journey",
    "hour": "raw hour is encoded as hour_sin/hour_cos by the trainer",
    "day_of_week": "raw day is encoded as day_sin/day_cos by the trainer",
    "month_of_year": "not in the model contract exports used to train the deployed artefacts",
    "bars_to_mfe": "post-outcome realised excursion (label-side, not signal-time)",
    "bars_to_mae": "post-outcome realised excursion (label-side, not signal-time)",
    "mfe_percent": "post-outcome realised excursion (label-side, not signal-time)",
    "mae_percent": "post-outcome realised excursion (label-side, not signal-time)",
    "pnl_percent": "post-outcome realised PnL (label-side, not signal-time)",
    "bars_held": "post-outcome realised holding time (label-side, not signal-time)",
    "realized_pnl_percent": "post-outcome realised PnL (label-side, not signal-time)",
    "max_drawdown": "post-outcome realised excursion (label-side, not signal-time)",
    "max_profit": "post-outcome realised excursion (label-side, not signal-time)",
    "mfe_mae_ratio": "post-outcome realised excursion ratio (label-side, not signal-time)",
    "excursion_efficiency": "post-outcome realised excursion statistic (label-side, not signal-time)",
    "mfe_duration_ms": "post-outcome realised duration (label-side, not signal-time)",
    "mae_duration_ms": "post-outcome realised duration (label-side, not signal-time)",
}


# --------------------------------------------------------------------------
# pure logic (no DB, no IO) — this is what --self-test exercises
# --------------------------------------------------------------------------
def _f(x):
    """Coerce to a finite float or None.

    Must accept psycopg2's Decimal for numeric columns and numpy scalars for
    JSON-derived values: rejecting them silently emptied whole features
    (e.g. every row of the `trigger_score` column read as None).
    """
    if x is None or isinstance(x, bool):
        return None
    if isinstance(x, str):
        s = x.strip()
        if not s or s.lower() in ("null", "none", "nan", "undefined", ""):
            return None
        try:
            x = float(s)
        except ValueError:
            return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _dig(d, path):
    cur = d
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def norm100(value):
    """Trainer normalisation: a 0-1 ratio is expressed as a percentage."""
    v = _f(value)
    if v is None:
        return None
    return v * 100.0 if v <= 1 else v


def _read_spec(envelope: dict, spec: str):
    """Resolve one atom: 'norm100(ind.x)' | 'ind.a.b' | 'column_name'."""
    spec = spec.strip()
    if spec.startswith("norm100("):
        return norm100(_read_spec(envelope, spec[len("norm100("):-1]))
    if spec.startswith("ind."):
        return _f(_dig(envelope.get("ind") or {}, spec[4:]))
    return _f(envelope.get(spec))


def read_raw(envelope: dict, key: str):
    """Resolve a canonical raw key from a row envelope (first non-null atom wins)."""
    spec = RAW_SOURCES.get(key)
    if spec is None:
        return None
    for alt in spec.split("|"):
        v = _read_spec(envelope, alt)
        if v is not None:
            return v
    return None


def canonicalize(name: str) -> str:
    low = name.strip().lower()
    return FEATURE_ALIASES.get(low, low)


def classify(name: str):
    """Return ('raw'|'derived'|'ordinal'|'unresolvable', payload)."""
    c = canonicalize(name)
    if c in RAW_SOURCES:
        return "raw", c
    if c in DERIVED:
        return "derived", c
    if c in ORDINAL_FEATURES:
        return "ordinal", c
    for prefix, why in UNRESOLVABLE_PREFIXES:
        if c.startswith(prefix):
            return "unresolvable", why
    if c in UNRESOLVABLE_EXACT:
        return "unresolvable", UNRESOLVABLE_EXACT[c]
    return "unresolvable", "no persisted signal-time projection is known for this feature name"


def projection_sql(name: str):
    kind, payload = classify(name)
    if kind == "raw":
        return SQL_EXPR.get(payload)
    if kind == "derived":
        deps = [d.strip() for d in payload.split(",")] if payload in ("x",) else None
        if payload == "bb_squeeze":
            return "CASE WHEN (sj.indicators->'bollingerBands'->>'width')::float < %(bb_p20)s THEN 1 ELSE 0 END"
        if payload == "bb_position":
            return "CASE WHEN ((sj.indicators->'bollingerBands'->>'upper')::float - (sj.indicators->'bollingerBands'->>'lower')::float) > 0 THEN (sj.entry_price - (sj.indicators->'bollingerBands'->>'lower')::float) / ((sj.indicators->'bollingerBands'->>'upper')::float - (sj.indicators->'bollingerBands'->>'lower')::float) ELSE 0.5 END"
        if payload == "macd_cross_strength":
            return "ABS(COALESCE((sj.indicators->'macd'->>'value')::float,0) - COALESCE((sj.indicators->'macd'->>'signal')::float,0))"
        if payload == "macd_histogram_sign":
            return "SIGN((sj.indicators->'macd'->>'histogram')::float)"
        if payload == "rsi_distance_50":
            return "COALESCE((sj.indicators->>'rsi14')::float, (sj.indicators->>'rsi')::float) - 50"
        if payload == "rsi_overbought":
            return "CASE WHEN COALESCE((sj.indicators->>'rsi14')::float, (sj.indicators->>'rsi')::float) > 70 THEN 1 ELSE 0 END"
        if payload == "rsi_oversold":
            return "CASE WHEN COALESCE((sj.indicators->>'rsi14')::float, (sj.indicators->>'rsi')::float) < 30 THEN 1 ELSE 0 END"
        if payload in ("hour_sin", "hour_cos", "day_sin", "day_cos"):
            col = "sj.hour_utc" if payload.startswith("hour") else "sj.day_of_week"
            period = 24.0 if payload.startswith("hour") else 7.0
            fn = "SIN" if payload.endswith("sin") else "COS"
            return "%s(2 * PI() * %s / %s)" % (fn, col, period)
        if payload == "atr_x_trend":
            return "COALESCE((sj.indicators->>'atrPercent')::float,0) * COALESCE((sj.indicators->>'trendStrength')::float,0)"
        if payload == "rsi_x_trend":
            return "COALESCE((sj.indicators->>'rsi14')::float,50) * COALESCE((sj.indicators->>'trendStrength')::float,0)"
        if payload == "volume_x_trend":
            return "COALESCE((sj.indicators->>'volumeZ20')::float,0) * COALESCE((sj.indicators->>'trendStrength')::float,0)"
        if payload == "trigger_x_confluence":
            return "sj.trigger_score::float * COALESCE((sj.indicators->>'confluenceScore')::float,0)"
        if payload == "quality_x_confidence":
            return "COALESCE((sj.indicators->>'signalQuality')::float,0) * COALESCE((sj.indicators->>'confluenceScore')::float,0)"
        if payload == "rr_ratio":
            return "CASE WHEN ABS(sj.entry_price - sj.suggested_stop_loss) > 0 THEN ABS(sj.suggested_take_profit - sj.entry_price) / ABS(sj.entry_price - sj.suggested_stop_loss) ELSE 1.0 END"
        if payload == "sl_distance_pct":
            return "CASE WHEN sj.entry_price > 0 THEN ABS(sj.entry_price - sj.suggested_stop_loss) / sj.entry_price * 100 ELSE 0 END"
        if payload == "tp_distance_pct":
            return "CASE WHEN sj.entry_price > 0 THEN ABS(sj.suggested_take_profit - sj.entry_price) / sj.entry_price * 100 ELSE 0 END"
        return None
    if kind == "ordinal":
        return "sj.%s (ordinal-encoded by the model metadata's ordinal_encoders)" % payload
    return None


def _ordinal(encoders: dict, key: str, value):
    order = (encoders or {}).get(key)
    if not isinstance(order, list) or value is None:
        return None
    try:
        return float(order.index(str(value)))
    except ValueError:
        return None


def compute_features(envelope: dict, names, frozen: dict, encoders: dict) -> dict:
    """PIT feature vector for one journey. `frozen` carries reference-window
    thresholds (bb_squeeze p20)."""
    raw_cache: dict = {}

    def raw(key):
        if key not in raw_cache:
            raw_cache[key] = read_raw(envelope, key)
        return raw_cache[key]

    out = {}
    for name in names:
        kind, payload = classify(name)
        if kind == "unresolvable":
            continue
        if kind == "ordinal":
            out[name] = _ordinal(encoders, payload, envelope.get(payload))
            continue
        if kind == "raw":
            out[name] = raw(payload)
            continue

        # derived
        rsi = raw("rsi14")
        e = {
            "bb_squeeze": (lambda: None if raw("bollinger_width") is None or frozen.get("bb_squeeze_p20") is None
                           else (1.0 if raw("bollinger_width") < frozen["bb_squeeze_p20"] else 0.0)),
            "bb_position": (lambda: None if None in (raw("bb_upper"), raw("bb_lower"), raw("entry_price"))
                            else ((raw("entry_price") - raw("bb_lower")) / (raw("bb_upper") - raw("bb_lower"))
                                  if (raw("bb_upper") - raw("bb_lower")) > 0 else 0.5)),
            "macd_histogram_sign": (lambda: None if raw("macd_histogram") is None
                                    else float((raw("macd_histogram") > 0) - (raw("macd_histogram") < 0))),
            "macd_cross_strength": (lambda: None if None in (raw("macd_value"), raw("macd_signal"))
                                    else abs(raw("macd_value") - raw("macd_signal"))),
            "rsi_distance_50": (lambda: None if rsi is None else rsi - 50.0),
            "rsi_overbought": (lambda: None if rsi is None else (1.0 if rsi > 70 else 0.0)),
            "rsi_oversold": (lambda: None if rsi is None else (1.0 if rsi < 30 else 0.0)),
            "hour_sin": (lambda: None if raw("hour_utc") is None else math.sin(2 * math.pi * raw("hour_utc") / 24)),
            "hour_cos": (lambda: None if raw("hour_utc") is None else math.cos(2 * math.pi * raw("hour_utc") / 24)),
            "day_sin": (lambda: None if raw("day_of_week") is None else math.sin(2 * math.pi * raw("day_of_week") / 7)),
            "day_cos": (lambda: None if raw("day_of_week") is None else math.cos(2 * math.pi * raw("day_of_week") / 7)),
            "atr_x_trend": (lambda: None if None in (raw("atr_percent"), raw("trend_strength"))
                            else raw("atr_percent") * raw("trend_strength")),
            "rsi_x_trend": (lambda: None if None in (rsi, raw("trend_strength"))
                            else (rsi if rsi is not None else 50.0) * raw("trend_strength")),
            "volume_x_trend": (lambda: None if None in (raw("volume_z20"), raw("trend_strength"))
                               else raw("volume_z20") * raw("trend_strength")),
            "trigger_x_confluence": (lambda: None if None in (raw("trigger_score"), raw("confluence_score_total"))
                                     else raw("trigger_score") * raw("confluence_score_total")),
            "quality_x_confidence": (lambda: None if None in (raw("signal_quality"), raw("confluence_score_total"))
                                     else raw("signal_quality") * raw("confluence_score_total")),
            "rr_ratio": (lambda: None if None in (raw("entry_price"), raw("suggested_stop_loss"),
                                                  raw("suggested_take_profit"))
                         else (abs(raw("suggested_take_profit") - raw("entry_price"))
                               / abs(raw("entry_price") - raw("suggested_stop_loss"))
                               if abs(raw("entry_price") - raw("suggested_stop_loss")) > 0 else 1.0)),
            "sl_distance_pct": (lambda: None if None in (raw("entry_price"), raw("suggested_stop_loss"))
                                else (abs(raw("entry_price") - raw("suggested_stop_loss")) / raw("entry_price") * 100
                                      if raw("entry_price") > 0 else 0.0)),
            "tp_distance_pct": (lambda: None if None in (raw("entry_price"), raw("suggested_take_profit"))
                                else (abs(raw("suggested_take_profit") - raw("entry_price")) / raw("entry_price") * 100
                                      if raw("entry_price") > 0 else 0.0)),
        }
        fn = e.get(payload)
        out[name] = fn() if fn else None
    return out


def percentile(sorted_vals, p):
    """Linear-interpolation percentile (matches numpy's default)."""
    n = len(sorted_vals)
    if n == 0:
        return None
    if n == 1:
        return sorted_vals[0]
    k = (n - 1) * (p / 100.0)
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return sorted_vals[int(k)]
    return sorted_vals[lo] * (hi - k) + sorted_vals[hi] * (k - lo)


def distribution(values, bins: int = DEFAULT_BINS) -> dict | None:
    clean = sorted(v for v in (_f(v) for v in values) if v is not None)
    n = len(clean)
    if n == 0:
        return None
    mean = sum(clean) / n
    var = sum((x - mean) ** 2 for x in clean) / n
    qs = {}
    for p in (1, 5, 10, 25, 50, 75, 90, 95, 99):
        qs["p%02d" % p] = percentile(clean, p)
    edges = []
    for i in range(bins + 1):
        edges.append(percentile(clean, i * 100.0 / bins))
    boundaries = list(edges)
    boundaries[0] = float("-inf")
    boundaries[-1] = float("inf")
    counts = [0] * bins
    for x in clean:
        placed = False
        for b in range(bins):
            lo, hi = boundaries[b], boundaries[b + 1]
            if (x >= lo and x < hi) or (b == bins - 1 and x <= hi):
                counts[b] += 1
                placed = True
                break
        if not placed:
            counts[-1] += 1
    fractions = [c / n for c in counts]
    return {
        "n": n,
        "mean": mean,
        "std": math.sqrt(var),
        "min": clean[0],
        "max": clean[-1],
        "quantiles": qs,
        "psi_reference": {
            "bins": bins,
            "boundaries": boundaries,
            "expected_fractions": fractions,
        },
    }


def compute_psi(expected_fractions, boundaries, actual_values, eps: float = 1e-6) -> float:
    """Same statistic as drift_monitor.compute_psi: sum((a-e)*ln(a/e)) over the
    reference deciles.  An empty bucket on the reference side cannot drive the
    statistic, so fractions are clamped with epsilon (documented behaviour)."""
    actual = [v for v in (_f(v) for v in actual_values) if v is not None]
    n = len(actual)
    if n == 0:
        return 0.0
    counts = [0] * (len(boundaries) - 1)
    for x in actual:
        for b in range(len(counts)):
            lo, hi = boundaries[b], boundaries[b + 1]
            if (x >= lo and x < hi) or (b == len(counts) - 1 and x <= hi):
                counts[b] += 1
                break
    psi = 0.0
    for e, c in zip(expected_fractions, counts):
        a = c / n
        e = min(max(e, eps), 1.0)
        a = min(max(a, eps), 1.0)
        psi += (a - e) * math.log(a / e)
    return float(psi)


# --------------------------------------------------------------------------
# database (read-only)
# --------------------------------------------------------------------------
def db_url(env_file: str = DEFAULT_ENV_FILE) -> str:
    url = None
    if os.path.exists(env_file):
        for line in open(env_file, errors="ignore"):
            line = line.strip()
            if line.startswith("DATABASE_URL="):
                url = line.split("=", 1)[1].strip().strip('"').strip("'")
                break
    url = url or os.environ.get("DATABASE_URL", "")
    if not url:
        raise SystemExit("no DATABASE_URL (env or %s)" % env_file)
    # host-side sessions cannot resolve the compose hostname
    url = re.sub(r"@[^/@:]+:", "@127.0.0.1:", url, count=1)
    return url


def fetch_envelopes(since_iso: str, until_iso: str, population: str, max_rows: int, url: str,
                    exclude_months=None):
    import psycopg2
    import psycopg2.extras

    where = {
        "mfe_first": "sj.mfe_first IS NOT NULL",
        "pnl": "sj.pnl_percent IS NOT NULL",
        "resolved": "sj.final_status IS NOT NULL",
    }[population]
    excl = ""
    params = {"since": since_iso, "until": until_iso, "lim": max_rows}
    if exclude_months:
        excl = "AND to_char(sj.triggered_at AT TIME ZONE 'UTC', 'YYYY-MM') <> ALL(%(exmonths)s)"
        params["exmonths"] = list(exclude_months)
    sql = """
        SELECT sj.triggered_at, sj.symbol, sj.direction, sj.timeframe, sj.final_status, sj.trading_session,
               sj.hour_utc, sj.day_of_week, sj.trigger_score, sj.entry_price,
               sj.suggested_stop_loss, sj.suggested_take_profit,
               EXTRACT(hour FROM sj.triggered_at AT TIME ZONE 'UTC')::int AS hour_from_ts,
               EXTRACT(dow  FROM sj.triggered_at AT TIME ZONE 'UTC')::int AS dow_from_ts,
               sj.indicators::text AS indicators_json
        FROM signal_journeys sj
        WHERE sj.triggered_at >= %(since)s AND sj.triggered_at < %(until)s
          AND {where}
          {excl}
          AND sj.indicators IS NOT NULL
        ORDER BY md5(sj.correlation_id || '{salt}')
        LIMIT %(lim)s
    """.format(where=where, excl=excl, salt=SAMPLE_SALT)
    conn = psycopg2.connect(url, connect_timeout=15,
                            options="-c default_transaction_read_only=on -c statement_timeout=900000")
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params)
        rows = cur.fetchall()
        cur.close()
    finally:
        conn.close()

    out = []
    for r in rows:
        try:
            ind = json.loads(r["indicators_json"]) if r["indicators_json"] else {}
        except json.JSONDecodeError:
            ind = {}
        if not isinstance(ind, dict):
            ind = {}
        out.append({
            "triggered_at": r["triggered_at"].isoformat() if r["triggered_at"] else None,
            "symbol": r["symbol"],
            "direction": r["direction"],
            "timeframe": r["timeframe"],
            "trading_session": r["trading_session"],
            "final_status": r["final_status"],
            "hour_utc": r["hour_utc"] if r["hour_utc"] is not None else r.get("hour_from_ts"),
            "day_of_week": r["day_of_week"] if r["day_of_week"] is not None else r.get("dow_from_ts"),
            "trigger_score": r["trigger_score"],
            "entry_price": r["entry_price"],
            "suggested_stop_loss": r["suggested_stop_loss"],
            "suggested_take_profit": r["suggested_take_profit"],
            "ind": ind,
        })
    return out


# --------------------------------------------------------------------------
# artifact construction
# --------------------------------------------------------------------------
# PSI reference adequacy floors: a decile reference over too few rows, or a
# single feature with too few values, cannot support a PSI reading at all.
PSI_MIN_SAMPLES = 300        # minimum rows backing the whole reference
PSI_MIN_FEATURE_N = 300      # minimum usable values for one feature
PSI_THRESHOLD = 0.25
PSI_MIN_MONITORED = 3        # fewer monitored features => thin, reported as such

# Deterministic uniform sampling salt: a truncated "ORDER BY triggered_at ASC"
# prefix is a biased reference (the earliest slice of the window, never the whole
# window).  A stable hash order gives a repeatable uniform sample and keeps the
# artifact byte-identical across runs.  Change the salt == change the reference;
# record it in the artifact so a sample is always reproducible.
SAMPLE_SALT = "sycode-drift-baseline-v1"


def _verdict(features: dict) -> dict:
    """Fail-closed usability verdict for a baseline artifact."""
    n_feat = len(features)
    if n_feat == 0:
        return {"state": "no_monitored_features", "usable": False,
                "why": "no feature in the model contract has a coverage-passing "
                       "signal-time projection in its training window; a training "
                       "baseline CANNOT be established (this is not 'no drift')"}
    thin = [f for f, d in features.items() if d["n"] < PSI_MIN_FEATURE_N]
    if n_feat < PSI_MIN_MONITORED or thin:
        return {"state": "thin_baseline", "usable": False,
                "why": "fewer than %d monitored features or too few values" % PSI_MIN_MONITORED,
                "features_below_min_n": sorted(thin)}
    return {"state": "usable", "usable": True,
            "why": "%d monitored features, all with n >= %d" % (n_feat, PSI_MIN_FEATURE_N)}


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical_hash(obj: dict) -> str:
    payload = dict(obj)
    payload.pop("generated_at", None)
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                     default=str).encode()).hexdigest()


def load_model_meta(path: str) -> dict:
    meta = json.load(open(path))
    if not isinstance(meta, dict):
        raise ValueError("metadata %s is not an object" % path)
    return meta


# --------------------------------------------------------------------------
# trainer-persisted reference distributions (dataset_info.json, dataset-info/1)
# --------------------------------------------------------------------------
# Trainers emit this next to the model metadata (tools/training-telemetry/
# dataset_info.py).  It is the ONE thing the reconstruction below can never
# rebuild: the per-feature distribution of the matrix the trainer actually
# consumed.  Consuming it removes the need to re-derive a reference from a
# later warehouse schema — but it does NOT create a live projection, so a
# feature rescued from `unresolvable` lands in `trainer_reference`, never in
# `features`, and can never move the fail-closed verdict.
DATASET_INFO_SCHEMA = "dataset-info/1"


def dataset_info_candidates(model_path: str, explicit=None) -> list:
    root, _ext = os.path.splitext(os.path.abspath(model_path))
    cands = []
    for p in (explicit or []):
        if p not in cands:
            cands.append(p)
    cands.append(root + ".dataset_info.json")
    cands.append(os.path.join(os.path.dirname(root), "dataset_info.json"))
    return cands


def load_dataset_info(path: str) -> dict:
    payload = json.load(open(path))
    if not isinstance(payload, dict):
        raise ValueError("dataset_info %s is not an object" % path)
    if payload.get("schema_version") != DATASET_INFO_SCHEMA:
        raise ValueError("dataset_info %s declares schema_version %r, expected %r"
                         % (path, payload.get("schema_version"), DATASET_INFO_SCHEMA))
    if not isinstance(payload.get("feature_distributions"), dict):
        raise ValueError("dataset_info %s carries no feature_distributions map" % path)
    return payload


def resolve_dataset_info(model_path: str, meta: dict, args) -> tuple:
    """(path, payload) for one model, or (None, None).

    A file whose declared model version disagrees with the metadata is REJECTED,
    not merged: silently attaching another model's reference is exactly the
    failure mode this whole exercise exists to prevent.
    """
    if getattr(args, "ignore_dataset_info", False):
        return None, None
    for cand in dataset_info_candidates(model_path, getattr(args, "dataset_info", None)):
        if not os.path.exists(cand):
            continue
        try:
            payload = load_dataset_info(cand)
        except Exception as exc:
            print("  [warn] dataset_info %s rejected: %s" % (cand, exc))
            continue
        declared = (payload.get("model") or {}).get("version")
        if declared and str(declared) != str(meta.get("version")):
            print("  [warn] dataset_info %s rejected: declares model %r, metadata is %r"
                  % (cand, declared, meta.get("version")))
            continue
        return cand, payload
    return None, None


def trainer_reference_usable(dist) -> bool:
    """A reference is usable only if it can actually back a PSI reading."""
    if not isinstance(dist, dict):
        return False
    ref = dist.get("psi_reference") or {}
    boundaries = ref.get("boundaries")
    fractions = ref.get("expected_fractions")
    if not isinstance(boundaries, list) or not isinstance(fractions, list):
        return False
    if len(boundaries) != len(fractions) + 1 or len(fractions) < 2:
        return False
    n = dist.get("n")
    if not isinstance(n, (int, float)) or n < PSI_MIN_FEATURE_N:
        return False
    return True


def trainer_reference_entry(dist: dict, payload: dict, path: str) -> dict:
    """One feature's persisted training-time reference.

    Carries NO live_projection on purpose: a training reference is half of a
    PSI comparison, and half is not a monitor.
    """
    return {
        "reference_source": os.path.abspath(path),
        "reference_schema_version": payload.get("schema_version"),
        "reference_artifact_hash": payload.get("artifact_hash"),
        "reference_model_version": (payload.get("model") or {}).get("version"),
        "reference_fit_basis": (payload.get("split") or {}).get("method"),
        "reference_rows": (payload.get("data") or {}).get("rows_reference"),
        "n": dist.get("n"),
        "coverage": dist.get("coverage"),
        "mean": dist.get("mean"),
        "std": dist.get("std"),
        "min": dist.get("min"),
        "max": dist.get("max"),
        "quantiles": dist.get("quantiles"),
        "bins": dist.get("bins"),
        "psi_reference": dist.get("psi_reference"),
        "live_projection": None,
        "live_projection_status": "unavailable_from_signal_journeys",
        "monitorable": False,
        "why": ("trainer-persisted reference only: the training-time distribution "
                "is known, but no signal-time projection exists for this feature "
                "name, so a live PSI can still not be computed"),
    }


def window_for(meta: dict, args) -> dict:
    """Resolve the PIT training window for a model version.

    Precedence: explicit --since/--until, then a declared entry in --window-map
    (a repo-documented training window, e.g. the mfe-first exporter), then an
    inferred trailing window before `trained_at`.  The basis is always recorded
    so an inferred window can never be read as a declared one.
    """
    if args.since and args.until:
        return {"since": args.since, "until": args.until,
                "basis": args.window_basis or "declared",
                "declared_by": args.window_source or "cli"}
    wmap = {}
    if getattr(args, "window_map", None):
        wmap = json.load(open(args.window_map))
    entry = wmap.get(str(meta.get("version"))) or {}
    if entry.get("since") and entry.get("until"):
        return {"since": entry["since"], "until": entry["until"],
                "basis": entry.get("basis", "declared"),
                "declared_by": entry.get("declared_by") or args.window_map,
                "exclude_months": entry.get("exclude_months") or []}
    trained_at = meta.get("trained_at")
    if not trained_at:
        return {"since": None, "until": None, "basis": "unknown", "declared_by": None}
    t = datetime.fromisoformat(str(trained_at).replace("Z", "+00:00"))
    # A training cut is a date in practice: models trained the same day share the
    # same PIT window (and the same scan).  Granularity is recorded so a
    # day-quantised cut is never mistaken for second-level precision.
    cut = t.replace(hour=0, minute=0, second=0, microsecond=0)
    since = (cut - timedelta(days=args.window_days)).isoformat()
    return {"since": since, "until": cut.isoformat(),
            "basis": "inferred_trailing_%dd_before_trained_at" % args.window_days,
            "until_granularity": "day", "trained_at_exact": t.isoformat(),
            "declared_by": None, "exclude_months": []}


def build_artifact(meta: dict, envelope: list, window: dict, args, model_path: str,
                   dataset_info=None) -> dict:
    names = list(meta.get("feature_names") or [])
    encoders = meta.get("ordinal_encoders") or {}

    # reference-window frozen threshold for bb_squeeze (trainer uses dataset p20)
    bw = [read_raw(e, "bollinger_width") for e in envelope]
    bw = sorted(v for v in bw if v is not None)
    frozen = {"bb_squeeze_p20": percentile(bw, 20) if len(bw) > 100 else None}

    computed = {n: [] for n in names}
    for e in envelope:
        for k, v in compute_features(e, names, frozen, encoders).items():
            computed.setdefault(k, []).append(v)

    features, excluded, unresolvable = {}, {}, {}
    for n in names:
        kind, payload = classify(n)
        if kind == "unresolvable":
            unresolvable[n] = {"reason": payload}
            continue
        vals = computed.get(n, [])
        present = sum(1 for v in vals if _f(v) is not None)
        coverage = (present / len(envelope)) if envelope else 0.0
        d = distribution(vals)
        if d is None or coverage < args.coverage_gate:
            excluded[n] = {
                "reason": "coverage_below_gate" if d is not None else "no_values_in_window",
                "coverage": coverage,
                "n": present,
            }
            continue
        features[n] = {
            "live_projection": projection_sql(n),
            "derivation": payload if kind == "derived" else ("raw column/jsonb" if kind == "raw" else "ordinal encoder"),
            "coverage": coverage,
            **d,
        }

    used = [e["triggered_at"] for e in envelope if e.get("triggered_at")]

    # ------------------------------------------------------------------
    # Gap fill from the trainer's own persisted reference.  Only names the
    # reconstruction could not reach are filled, and filled entries stay OUT of
    # `features`: a reference without a live projection is not a monitor, so it
    # must never feed the verdict or the PSI denominator.
    # ------------------------------------------------------------------
    di_path, di_payload = dataset_info if dataset_info else (None, None)
    trainer_reference = {}
    if di_payload:
        dists = di_payload.get("feature_distributions") or {}
        for n in names:
            if n in features or n in excluded or n not in unresolvable:
                continue
            dist = dists.get(n)
            if not trainer_reference_usable(dist):
                continue
            trainer_reference[n] = trainer_reference_entry(dist, di_payload, di_path)
            unresolvable.pop(n, None)

    di_evidence = None
    if di_payload:
        declared_names = set(names)
        di_names = set((di_payload.get("feature_distributions") or {}).keys())
        di_evidence = {
            "path": os.path.abspath(di_path),
            "sha256": sha256_file(di_path),
            "schema_version": di_payload.get("schema_version"),
            "artifact_hash": di_payload.get("artifact_hash"),
            "model_version": (di_payload.get("model") or {}).get("version"),
            "metadata_path": (di_payload.get("model") or {}).get("metadata_path"),
            "metadata_sha256": (di_payload.get("model") or {}).get("metadata_sha256"),
            "rows": di_payload.get("rows"),
            "rows_reference": (di_payload.get("data") or {}).get("rows_reference"),
            "window": di_payload.get("window"),
            "split": di_payload.get("split"),
            "population": di_payload.get("population"),
            "label_source": di_payload.get("label_source"),
            "schema_hash": di_payload.get("schema_hash"),
            "features_covered": sorted(trainer_reference),
            "features_in_file_not_in_contract": sorted(di_names - declared_names),
            "features_in_contract_not_in_file": sorted(declared_names - di_names),
            "consumer_rule": ("this reference never enters `features`; it records "
                              "what the trainer measured, not what live data can "
                              "still measure"),
        }

    reference_coverage = {
        "declared": len(names),
        "monitored": len(features),
        "excluded": len(excluded),
        "trainer_reference_only": len(trainer_reference),
        "no_reference": len(unresolvable),
        "reference_known_fraction": (
            round((len(features) + len(excluded) + len(trainer_reference)) / len(names), 4)
            if names else None),
        "monitorable_fraction": round(len(features) / len(names), 4) if names else None,
    }

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": {
            "model_type": meta.get("model_type"),
            "version": meta.get("version"),
            "trained_at": meta.get("trained_at"),
            "metadata_path": model_path,
            "metadata_sha256": sha256_file(model_path),
            "n_features_declared": len(names),
        },
        "pit": {
            "source": "signal_journeys (postgres, read-only transaction)",
            "population": args.population,
            "population_predicate": {
                "mfe_first": "sj.mfe_first IS NOT NULL",
                "pnl": "sj.pnl_percent IS NOT NULL",
                "resolved": "sj.final_status IS NOT NULL",
            }[args.population],
            "immutability_basis": "signal_journeys.indicators is an immutable root column "
                                  "(server/src/infra/persistence/signalJourneysImmutableColumns.ts) — signal-time snapshot",
            "window": window,
            "effective_window": {
                "min_triggered_at": min(used) if used else None,
                "max_triggered_at": max(used) if used else None,
            },
            "rows_used": len(envelope),
            "max_rows_cap": args.max_rows,
            "sample_mode": "uniform_hash",
            "sample_salt": SAMPLE_SALT,
            "sample_note": ("rows selected by md5(correlation_id||salt) order, so a "
                            "capped sample is uniform over the whole window, not the "
                            "earliest prefix"),
            "truncated": len(envelope) >= args.max_rows,
            "frozen_thresholds": frozen,
        },
        "coverage_gate": args.coverage_gate,
        "monitored_features": sorted(features),
        "features": features,
        "excluded": excluded,
        "unresolvable": unresolvable,
        "trainer_reference": trainer_reference,
        "trainer_reference_evidence": di_evidence,
        "reference_coverage": reference_coverage,
        "counts": {
            "declared": len(names),
            "monitored": len(features),
            "excluded": len(excluded),
            "unresolvable": len(unresolvable),
            "trainer_reference": len(trainer_reference),
        },
        # Fail-closed verdict, so a consumer never has to infer usability from
        # an empty/mostly-empty feature map.  A baseline with no monitored
        # features is NOT "no drift" — it is "cannot be measured".
        "verdict": _verdict(features),
        "psi_min_samples": PSI_MIN_SAMPLES,
        "psi_min_feature_n": PSI_MIN_FEATURE_N,
        "generator": {
            "path": os.path.abspath(__file__),
            "sha256": sha256_file(os.path.abspath(__file__)),
            "python": sys.version.split()[0],
        },
    }


def safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(s))


# --------------------------------------------------------------------------
# self-test (no DB)
# --------------------------------------------------------------------------
def _synth_envelope(i: int, shift: float = 0.0) -> dict:
    return {
        "triggered_at": "2026-01-01T00:00:%02d+00:00" % (i % 60),
        "symbol": "BTCUSDT", "direction": "LONG", "timeframe": "1h",
        "hour_utc": i % 24, "day_of_week": i % 7, "trigger_score": 50 + (i % 10),
        "entry_price": 100.0, "suggested_stop_loss": 98.0, "suggested_take_profit": 104.0,
        "ind": {
            "rsi14": 40.0 + (i % 30) + shift, "adx": 20.0 + (i % 15),
            "atrPercent": 0.02 + (i % 5) / 1000.0, "trendStrength": 0.5 + (i % 7) / 100.0,
            "signalQuality": 0.7, "volumeZ20": float(i % 5) - 1.0,
            "macd": {"value": 0.5 + (i % 4) * 0.1, "signal": 0.3, "histogram": 0.1 * (i % 3)},
            "bollingerBands": {"width": 0.01 + (i % 6) / 1000.0, "upper": 103.0, "lower": 97.0},
            "confluenceScore": 60.0 + (i % 20),
            "ema34Slope": 0.001 * (i % 5), "emaSpread": 0.002 * (i % 5),
            "ema12": 100.5, "ema26": 100.2,
        },
    }


def self_test(args) -> int:
    failures = []

    # ---- dataset_info: schema/version guard + gap fill that must NOT monitor ----
    import tempfile

    def _di_check(name, ok, detail=""):
        print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name, (" — " + detail) if detail else ""))
        if not ok:
            failures.append(name)

    _di_names = ["rsi14", "adx", "atr_percent", "trend_strength", "rsi_distance_50",
                 "macd_histogram_sign", "bb_position", "hour_sin", "rr_ratio",
                 "mfe_rate_50", "pnl_percent"]
    _di_meta = {
        "version": "selftest_di_v1", "trained_at": "2026-02-18T08:18:06Z",
        "model_type": "selftest", "feature_names": _di_names,
        "ordinal_encoders": {"direction": ["LONG", "SHORT"], "timeframe": ["15m", "1h"]},
    }
    _di_sample = [_synth_envelope(i) for i in range(400)]
    _di_win = {"since": "2026-01-01T00:00:00Z", "until": "2026-02-18T00:00:00Z",
               "basis": "selftest", "declared_by": "self_test"}
    _di_args = argparse.Namespace(
        model_json=[], models_dir=".", out_dir=".", env_file=".", population="mfe_first",
        coverage_gate=0.95, bins=10, max_rows=200000, window_days=365, since=None, until=None,
        window_basis=None, window_source=None, window_map=None, self_test=True,
        check_live=0, dry_run=True, rebuild_manifest=False, all_deployed=False,
        dataset_info=None, ignore_dataset_info=False)
    _di_base = build_artifact(_di_meta, _di_sample, _di_win, _di_args, __file__)

    def _di_payload(model_version, features):
        return {
            "schema_version": "dataset-info/1",
            "artifact_hash": "deadbeef",
            "model": {"version": model_version},
            "data": {"rows_reference": 1000},
            "split": {"method": "chronological_positional"},
            "rows": 1000,
            "feature_distributions": features,
        }

    def _di_dist(n):
        return {"n": n, "coverage": 1.0, "mean": 0.0, "std": 1.0, "min": -1.0, "max": 1.0,
                "quantiles": {"p50": 0.0},
                "bins": {"boundaries": [-1.0, 0.0, 1.0], "fractions": [0.5, 0.5]},
                "psi_reference": {"bins": 2, "boundaries": [-1.0, 0.0, 1.0],
                                  "expected_fractions": [0.5, 0.5]}}

    _di_path = os.path.join(tempfile.mkdtemp(prefix="drift-baseline-selftest-"),
                            "selftest_di_v1.dataset_info.json")
    with open(_di_path, "w") as fh:
        json.dump(_di_payload("selftest_di_v1", {"mfe_rate_50": _di_dist(400)}), fh)
    _di_found = resolve_dataset_info(_di_path[:-len(".dataset_info.json")] + ".json",
                                     _di_meta, _di_args)
    _di_mismatch = resolve_dataset_info(
        "--nope--", dict(_di_meta, version="some_other_model_v9"),
        argparse.Namespace(dataset_info=[_di_path], ignore_dataset_info=False))
    _di_check("sibling dataset_info is discovered next to the metadata",
              _di_found[0] is not None and _di_found[1] is not None)
    _di_check("a dataset_info declaring another model version is refused, not merged",
              _di_mismatch == (None, None), "match=%s" % (_di_mismatch[0],))
    _di_check("--ignore-dataset-info disables consumption",
              resolve_dataset_info("--nope--", _di_meta,
                                   argparse.Namespace(dataset_info=[_di_path],
                                                      ignore_dataset_info=True)) == (None, None))
    _di_check("nothing is consumed when no candidate file exists",
              resolve_dataset_info(os.path.join(os.path.dirname(_di_path), "absent.json"),
                                   _di_meta,
                                   argparse.Namespace(dataset_info=None,
                                                      ignore_dataset_info=False)) == (None, None))

    _di_a = build_artifact(_di_meta, _di_sample, _di_win, _di_args, __file__,
                           dataset_info=(_di_path,
                                         _di_payload("selftest_di_v1", {"mfe_rate_50": _di_dist(400)})))
    _di_check("trainer reference removes the name from `unresolvable`",
              "mfe_rate_50" not in _di_a["unresolvable"]
              and "mfe_rate_50" in _di_a["trainer_reference"],
              "unresolvable=%d trainer_reference=%d" % (len(_di_a["unresolvable"]),
                                                        len(_di_a["trainer_reference"])))
    _di_check("trainer reference NEVER enters the monitorable `features` map",
              "mfe_rate_50" not in _di_a["features"])
    _di_check("trainer reference entries have no live projection and are unmonitorable",
              all(f["live_projection"] is None and f["monitorable"] is False
                  for f in _di_a["trainer_reference"].values()))
    _di_check("a trainer reference does NOT flip the fail-closed verdict",
              _di_a["verdict"] == _di_base["verdict"], _di_a["verdict"]["state"])
    _di_check("reference accounting adds up",
              _di_a["counts"]["monitored"] + _di_a["counts"]["excluded"]
              + _di_a["counts"]["unresolvable"] + _di_a["counts"]["trainer_reference"]
              == _di_a["counts"]["declared"])
    _di_check("reference_coverage separates reference-known from monitorable",
              _di_a["reference_coverage"]["reference_known_fraction"]
              > _di_a["reference_coverage"]["monitorable_fraction"])
    _di_check("a reference too small to back a PSI is refused",
              not trainer_reference_usable(_di_dist(5)))
    _di_b = build_artifact(_di_meta, _di_sample, _di_win, _di_args, __file__,
                           dataset_info=(_di_path, _di_payload(
                               "selftest_di_v1",
                               {"mfe_rate_50": _di_dist(5), "pnl_percent": _di_dist(400)})))
    _di_check("under-sized reference stays in `unresolvable`",
              "mfe_rate_50" in _di_b["unresolvable"]
              and "mfe_rate_50" not in _di_b["trainer_reference"])
    _di_check("a name absent from the contract file stays unresolvable",
              "pnl_percent" in _di_a["unresolvable"])
    _di_check("gap fill only fires on names the reconstruction could not reach",
              _di_a["counts"]["monitored"] == _di_base["counts"]["monitored"])

    def check(name, ok, detail=""):
        print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name, (" — " + detail) if detail else ""))
        if not ok:
            failures.append(name)

    print("== drift baseline self-test")
    sample = [_synth_envelope(i) for i in range(400)]
    names = ["rsi14", "adx", "atr_percent", "trend_strength", "rsi_distance_50",
             "macd_histogram_sign", "bb_position", "hour_sin", "rr_ratio",
             "mfe_rate_50", "pnl_percent"]

    meta = {
        "version": "selftest_v1", "trained_at": "2026-02-18T08:18:06Z",
        "model_type": "selftest", "feature_names": names,
        "ordinal_encoders": {"direction": ["LONG", "SHORT"], "timeframe": ["15m", "1h"]},
    }
    win = {"since": "2026-01-01T00:00:00Z", "until": "2026-02-18T00:00:00Z",
           "basis": "selftest", "declared_by": "self_test"}

    a1 = build_artifact(meta, sample, win, args, __file__)
    a2 = build_artifact(meta, sample, win, args, __file__)

    check("determinism: identical input -> identical canonical hash",
          canonical_hash(a1) == canonical_hash(a2), canonical_hash(a1)[:16])
    check("monitored set is non-trivial", len(a1["monitored_features"]) >= 8,
          "%d monitored" % len(a1["monitored_features"]))
    check("unresolvable keys recorded, never silently dropped",
          "mfe_rate_50" in a1["unresolvable"] and "pnl_percent" in a1["unresolvable"])
    check("every monitored feature carries a live projection",
          all(f.get("live_projection") for f in a1["features"].values()))
    check("declared/unresolvable accounting adds up",
          a1["counts"]["monitored"] + a1["counts"]["excluded"] + a1["counts"]["unresolvable"]
          == a1["counts"]["declared"])

    # PSI semantics
    feature = a1["features"]["rsi14"]
    ref = feature["psi_reference"]
    same = [e["ind"]["rsi14"] for e in sample]
    shifted = [v + 12.0 for v in same]
    psi_same = compute_psi(ref["expected_fractions"], ref["boundaries"], same)
    psi_shift = compute_psi(ref["expected_fractions"], ref["boundaries"], shifted)
    check("PSI == 0 on an identical sample", psi_same == 0.0, "psi=%.6f" % psi_same)
    check("PSI > threshold on a shifted sample", psi_shift > PSI_THRESHOLD, "psi=%.4f" % psi_shift)

    # coverage gate: a key present in the contract but never populated
    weak = dict(meta)
    weak["feature_names"] = names + ["adx"]
    hollow = []
    for e in sample:
        e2 = dict(e)
        e2["ind"] = dict(e["ind"])
        e2["ind"]["adx"] = None
        hollow.append(e2)
    a3 = build_artifact(weak, hollow, win, args, __file__)
    check("coverage-failing key is excluded from the monitored universe",
          "adx" not in a3["monitored_features"], "excluded=%s" % list(a3["excluded"]))
    check("excluded key is absent from the PSI denominator",
          all("adx" != k for k in a3["features"]))

    # a null-only key must not read as no-drift
    a4 = build_artifact(weak, hollow, win, args, __file__)
    check("excluded key carries an explicit reason and measured coverage",
          a4["excluded"].get("adx", {}).get("reason") in ("coverage_below_gate", "no_values_in_window")
          and "coverage" in a4["excluded"]["adx"],
          "reason=%s coverage=%.3f" % (a4["excluded"].get("adx", {}).get("reason"),
                                       a4["excluded"].get("adx", {}).get("coverage", -1)))

    # fail-closed: an artifact with nothing monitorable must never read as healthy
    meta_none = dict(meta, feature_names=["mfe_rate_500", "pnl_percent", "made_up_thing"])
    a5 = build_artifact(meta_none, sample, win, args, __file__)
    check("zero-monitored artifact is fail-closed (never 'no drift')",
          a5["verdict"]["usable"] is False
          and a5["verdict"]["state"] == "no_monitored_features",
          "verdict=%s" % a5["verdict"]["state"])

    # numeric DB columns arrive as Decimal: a silent None here empties a whole feature
    from decimal import Decimal as _Dec
    dec_env = []
    for i, e in enumerate(sample):
        e2 = dict(e)
        e2["trigger_score"] = _Dec(str(50 + (i % 10)))
        dec_env.append(e2)
    a6 = build_artifact(dict(meta, feature_names=["trigger_score"]), dec_env, win, args, __file__)
    check("Decimal column values are not silently dropped",
          a6["counts"]["monitored"] == 1 and a6["features"]["trigger_score"]["coverage"] == 1.0,
          "monitored=%d coverage=%s" % (a6["counts"]["monitored"],
                                        a6["features"].get("trigger_score", {}).get("coverage")))

    print("\nDRIFT BASELINE SELF-TEST %s (%d failures)" % ("PASS" if not failures else "FAIL", len(failures)))
    return 0 if not failures else 1


# --------------------------------------------------------------------------
# live-vs-baseline read
# --------------------------------------------------------------------------
def check_live(args) -> int:
    url = db_url(args.env_file)
    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=args.check_live)).isoformat()
    until = now.isoformat()
    rep = {"generated_at": now.isoformat(), "lookback_days": args.check_live,
           "population": args.population, "models": {}}
    for model_json in args.model_json:
        art_path = os.path.join(args.out_dir, safe_name(load_model_meta(model_json).get("version")) + ".json")
        if not os.path.exists(art_path):
            print("no baseline for %s (expected %s)" % (model_json, art_path))
            continue
        art = json.load(open(art_path))
        env = fetch_envelopes(since, until, args.population, args.max_rows, url)
        encoders = load_model_meta(model_json).get("ordinal_encoders") or {}
        names = list(art["features"])
        live = {n: [] for n in names}
        for e in env:
            for k, v in compute_features(e, names, art["pit"]["frozen_thresholds"], encoders).items():
                live.setdefault(k, []).append(v)
        rows = {}
        flagged = 0
        for n in names:
            ref = art["features"][n]["psi_reference"]
            psi = compute_psi(ref["expected_fractions"], ref["boundaries"], live.get(n, []))
            rows[n] = {"psi": psi, "flagged": psi >= PSI_THRESHOLD, "live_n": len([1 for v in live.get(n, []) if _f(v) is not None])}
            flagged += 1 if psi >= PSI_THRESHOLD else 0
        monitored = len(names)
        frac = (flagged / monitored) if monitored else 0.0
        rep["models"][art["model"]["version"]] = {
            "model_type": art["model"]["model_type"],
            "baseline_trained_at": art["model"]["trained_at"],
            "monitored": monitored, "flagged": flagged, "flagged_fraction": frac,
            "live_rows": len(env), "features": rows,
        }
        print("\n== %s @ %s  (train->%s)" % (art["model"]["model_type"], art["model"]["version"],
                                             art["model"]["trained_at"]))
        print("   live rows=%d  monitored=%d  flagged=%d  fraction=%.3f" % (len(env), monitored, flagged, frac))
        for n, r in sorted(rows.items(), key=lambda kv: -kv[1]["psi"])[:12]:
            print("   %-24s psi=%8.4f %s live_n=%d" % (n, r["psi"], "FLAG" if r["flagged"] else "    ", r["live_n"]))
    out = os.path.join(args.out_dir, "live_vs_baseline_%s.json" % now.strftime("%Y%m%dT%H%M%SZ"))
    with open(out, "w") as fh:
        json.dump(rep, fh, indent=2, sort_keys=True, default=str)
    print("\nwrote %s" % out)
    return 0


# --------------------------------------------------------------------------
def rebuild_manifest(args) -> int:
    """Re-derive MANIFEST.json from the artifacts already on disk, and re-verify
    each recorded sha256.  No DB, no regeneration."""
    entries = []
    bad = []
    for name in sorted(os.listdir(args.out_dir)):
        if not name.endswith(".json") or name == "MANIFEST.json" or name.startswith("live_vs_"):
            continue
        p = os.path.join(args.out_dir, name)
        try:
            art = json.load(open(p))
        except Exception as exc:
            bad.append((name, "unreadable: %s" % exc))
            continue
        if art.get("schema_version") != SCHEMA_VERSION:
            bad.append((name, "schema_version=%r" % art.get("schema_version")))
            continue
        entries.append({
            "model_type": art["model"]["model_type"], "version": art["model"]["version"],
            "path": p, "sha256": sha256_file(p), "artifact_hash": art.get("artifact_hash"),
            "counts": art["counts"], "window": art["pit"]["window"],
            "verdict": art.get("verdict"),
        })
    man = {"schema_version": SCHEMA_VERSION, "generated_at": datetime.now(timezone.utc).isoformat(),
           "population": args.population, "coverage_gate": args.coverage_gate,
           "artifacts": entries, "unverified": bad}
    mpath = os.path.join(args.out_dir, "MANIFEST.json")
    with open(mpath, "w") as fh:
        json.dump(man, fh, indent=2, sort_keys=True, default=str)
    usable = [e["version"] for e in entries if (e.get("verdict") or {}).get("usable")]
    print("rebuilt %s: %d artifacts, %d usable, %d unverified" % (mpath, len(entries), len(usable), len(bad)))
    for v in usable:
        print("  usable: %s" % v)
    for n, why in bad:
        print("  UNVERIFIED: %s (%s)" % (n, why))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Build persisted PIT training-time feature baselines")
    ap.add_argument("--model-json", action="append", default=[], help="model metadata JSON (repeatable)")
    ap.add_argument("--models-dir", default=DEFAULT_MODELS_DIR)
    ap.add_argument("--all-deployed", action="store_true",
                    help="use every metadata JSON in --models-dir (default: only feature-bearing ones)")
    ap.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    ap.add_argument("--env-file", default=DEFAULT_ENV_FILE)
    ap.add_argument("--population", choices=["mfe_first", "pnl", "resolved"], default="mfe_first")
    ap.add_argument("--coverage-gate", type=float, default=DEFAULT_COVERAGE_GATE)
    ap.add_argument("--bins", type=int, default=DEFAULT_BINS)
    ap.add_argument("--max-rows", type=int, default=200000)
    ap.add_argument("--window-days", type=int, default=365)
    ap.add_argument("--since", default=None)
    ap.add_argument("--until", default=None)
    ap.add_argument("--window-basis", default=None)
    ap.add_argument("--window-source", default=None)
    ap.add_argument("--window-map", default=None,
                    help="JSON map {model_version: {since,until,basis,declared_by,exclude_months}}")
    ap.add_argument("--dataset-info", action="append", default=[],
                    help="explicit dataset_info.json (dataset-info/1) reference file (repeatable)")
    ap.add_argument("--ignore-dataset-info", action="store_true",
                    help="consume no trainer reference; keeps the reconstruction-only view")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--check-live", type=int, default=0,
                    help="compute real live-vs-baseline PSI over the last N days")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--rebuild-manifest", action="store_true",
                    help="re-derive MANIFEST.json from artifacts on disk and re-verify sha256")
    args = ap.parse_args()

    if args.self_test:
        return self_test(args)

    if args.rebuild_manifest:
        return rebuild_manifest(args)

    if args.check_live:
        if not args.model_json:
            args.model_json = sorted(str(p) for p in Path(args.models_dir).glob("*.json"))
        return check_live(args)

    model_jsons = list(args.model_json)
    if not model_jsons:
        model_jsons = sorted(str(p) for p in Path(args.models_dir).glob("*.json"))
    metas = []
    for p in model_jsons:
        try:
            m = load_model_meta(p)
        except Exception as e:
            print("skip %s: %s" % (p, str(e)[:80]))
            continue
        if not m.get("feature_names"):
            if not args.all_deployed:
                continue
            print("skip %s: no feature_names" % p)
            continue
        metas.append((p, m))

    url = db_url(args.env_file)
    os.makedirs(args.out_dir, exist_ok=True)
    manifest = {"schema_version": SCHEMA_VERSION,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "population": args.population, "coverage_gate": args.coverage_gate,
                "artifacts": []}

    envelope_cache = {}
    for path, meta in metas:
        win = window_for(meta, args)
        if not win["since"]:
            print("skip %s: no trained_at and no --since/--until" % path)
            continue
        key = (win["since"], win["until"], args.population, args.max_rows,
               tuple(win.get("exclude_months") or []))
        if key not in envelope_cache:
            print("scan window %s .. %s%s ..." % (
                win["since"], win["until"],
                (" excluding %s" % win["exclude_months"]) if win.get("exclude_months") else ""))
            envelope_cache[key] = fetch_envelopes(win["since"], win["until"], args.population,
                                                  args.max_rows, url,
                                                  win.get("exclude_months") or None)
            print("  rows=%d" % len(envelope_cache[key]))
        env = envelope_cache[key]
        di_path, di_payload = resolve_dataset_info(path, meta, args)
        art = build_artifact(meta, env, win, args, path, dataset_info=(di_path, di_payload))
        art["artifact_hash"] = canonical_hash(art)
        out = os.path.join(args.out_dir, safe_name(meta.get("version")) + ".json")
        print("%-46s monitored=%2d excluded=%2d unresolvable=%2d trainer_ref=%2d -> %s" % (
            str(meta.get("version"))[:46], art["counts"]["monitored"],
            art["counts"]["excluded"], art["counts"]["unresolvable"],
            art["counts"]["trainer_reference"], out))
        if di_path:
            print("%-46s   reference: %s (%d features covered, fit on %s)" % (
                "", os.path.basename(di_path), len(art["trainer_reference"]),
                (di_payload.get("split") or {}).get("method")))
        if not args.dry_run:
            with open(out, "w") as fh:
                json.dump(art, fh, indent=2, sort_keys=True, default=str)
            manifest["artifacts"].append({
                "model_type": art["model"]["model_type"], "version": art["model"]["version"],
                "path": out, "sha256": sha256_file(out), "artifact_hash": art["artifact_hash"],
                "counts": art["counts"], "window": win,
                "reference_coverage": art.get("reference_coverage"),
                "trainer_reference_features": sorted(art.get("trainer_reference") or {}),
                "dataset_info": (art.get("trainer_reference_evidence") or {}).get("path"),
                    "verdict": art.get("verdict"),
            })
    if not args.dry_run:
        mpath = os.path.join(args.out_dir, "MANIFEST.json")
        with open(mpath, "w") as fh:
            json.dump(manifest, fh, indent=2, sort_keys=True, default=str)
        print("\nwrote %s (%d artifacts)" % (mpath, len(manifest["artifacts"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())