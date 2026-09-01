"""
Tests for dgx_report_anomaly_detector.py — Enhanced ACRADR Phase 1 core.

Covers:
  - Positive fixtures (each rule class fires)
  - Negative fixtures (clean reports produce zero anomalies)
  - Zero-token fallback path (provider outage flag still detects)
  - Classification by "# Cron Job:" header and filename
  - Git metadata + system metrics enrichment are attached
  - Health-canary JSONL gateway / freshness record parsing
Exit-code semantics (2 on critical, 0 otherwise).
"""

import json
from pathlib import Path

import pytest

import dgx_report_anomaly_detector as det

# ── Fixtures ───────────────────────────────────────────────────────────────

HERE = Path(__file__).parent
FIX = HERE / "fixtures" / "acradr"
FIX.mkdir(parents=True, exist_ok=True)


def _write(name: str, content: str) -> Path:
    p = FIX / name
    p.write_text(content, encoding="utf-8")
    return p


# --- Health canary JSONL (gateway + freshness records) ---
HC_GATEWAY_DOWN = _write("health_gw_down.jsonl",
    '{"ts":"2026-07-11T02:00:00Z","gateway_running":false,"hermes_cli":true}\n')

HC_GATEWAY_OK = _write("health_gw_ok.jsonl",
    '{"ts":"2026-07-11T02:00:00Z","gateway_running":true,"hermes_cli":true}\n')

HC_FRESHNESS_STALE = _write("health_fresh_stale.jsonl",
    '{"ts":"2026-07-11T02:00:00Z","source":"data-freshness-probe",'
    '"data_freshness":{"overall":"degraded","stale_count":1,"pipelines":'
    '{"candles":{"status":"stale","age_h":12.4,"budget":3}}}}\n')

HC_FRESHNESS_OK = _write("health_fresh_ok.jsonl",
    '{"ts":"2026-07-11T02:00:00Z","source":"data-freshness-probe",'
    '"data_freshness":{"overall":"ok","stale_count":0,"pipelines":'
    '{"candles":{"status":"fresh","age_h":0.4,"budget":3}}}}\n')

# --- Fusion calibration report (markdown) ---
CALIB_BAD = _write("calib_bad.md",
    "# Cron Job: fusion-calibration-report\n"
    "## 7. Observations\n"
    "- **Clean win rate: 12.3%** (5W / 35L).\n"
    "- **Sample-weighted MCE: 27.5pp** (unweighted legacy method: 30.1pp; alert threshold 15pp).\n")

CALIB_PARSING_ERROR = _write("calib_parse_err.md",
    "# Cron Job: fusion-calibration-report\n"
    "ERROR: parsing-error while reading fusion_json column\n")

CALIB_OK = _write("calib_ok.md",
    "# Cron Job: fusion-calibration-report\n"
    "## 7. Observations\n"
    "- **Clean win rate: 47.5%** (38W / 42L).\n"
    "- **Sample-weighted MCE: 8.2pp** (unweighted legacy method: 9.0pp; alert threshold 15pp).\n")

# --- News catalyst report (markdown) ---
NEWS_CLOSE_LIMIT = _write("news_limit.md",
    "# Cron Job: news-sentiment-catalyst\n"
    "WARN: news collector running close to limit (94% of hourly budget)\n")

NEWS_TIMEOUT = _write("news_timeout.md",
    "# Cron Job: news-sentiment-catalyst\n"
    "requests.exceptions.Timeout: HTTPSConnectionPool timeout\n")

NEWS_EXCEPTION = _write("news_exc.md",

    "# Cron Job: news-sentiment-catalyst\n"
    "Traceback (most recent call last): Exception: rate limited\n")

NEWS_OK = _write("news_ok.md",
    "# Cron Job: news-sentiment-catalyst\n"
    "Collected 42 headlines, 0 errors, sentiment distribution nominal\n")

# --- Fusion engine report (markdown) ---
FUSION_DB_ERR = _write("fusion_dberr.md",
    "# Cron Job: run-signal-fusion\n"
    "database error: could not connect to postgres primary\n")

FUSION_WRITE_FAIL = _write("fusion_writefail.md",
    "# Cron Job: run-signal-fusion\n"
    "failed to write trade_setup for BTCUSDT 15m\n")

FUSION_FILL_LOW = _write("fusion_filllow.md",
    "# Cron Job: run-signal-fusion\n"
    "fill rate 71.2% below 85% threshold\n")

FUSION_OK = _write("fusion_ok.md",
    "# Cron Job: run-signal-fusion\n"
    "Loaded 50 signals | Written: 41 trade setups | High conviction: 6\n"
    "fill rate 96.4%\n")

# --- Misclassified / empty ---
EMPTY = _write("empty.md", "")
NONREPORT = _write("notes.md",
    "# Standup notes\nNothing to report today.\n")


# ── Helpers ─────────────────────────────────────────────────────────────────

def scan_file(path: Path, provider_outage: bool = False):
    """Classify + scan a single fixture file, returning anomalies."""
    header = None
    first = path.read_text(encoding="utf-8", errors="replace").splitlines()[:1]
    if first and first[0].startswith("# Cron Job:"):
        header = first[0][len("# Cron Job:"):].strip()
    report_class = det.classify_file(path, header)
    assert report_class is not None, f"fixture {path.name} was not classified"
    return det.scan_text_report(path, report_class, header, provider_outage)


def ids(anoms):
    return sorted(a.rule_id for a in anoms)


# ── Positive tests (each rule fires) ────────────────────────────────────────

def test_health_gateway_down_is_critical():
    a = scan_file(HC_GATEWAY_DOWN)
    assert ids(a) == ["health.gateway_down"]
    assert a[0].severity == "critical"


def test_health_freshness_stale_detected():
    a = scan_file(HC_FRESHNESS_STALE)
    assert "freshness.pipeline_stale" in ids(a)
    assert "freshness.stale_overall" in ids(a)


def test_calibration_win_rate_low():
    a = scan_file(CALIB_BAD)
    assert "calibration.win_rate_low" in ids(a)
    assert any("12.3" in x.snippet for x in a)


def test_calibration_mce_high_v2():
    a = scan_file(CALIB_BAD)
    assert "calibration.mce_high_v2" in ids(a)
    assert any("27.5" in x.snippet for x in a)


def test_calibration_parsing_error():
    a = scan_file(CALIB_PARSING_ERROR)
    assert "calibration.parsing_error" in ids(a)


def test_news_close_to_limit():
    a = scan_file(NEWS_CLOSE_LIMIT)
    assert "news.close_to_limit" in ids(a)
    assert a[0].severity == "warning"


def test_news_timeout():
    a = scan_file(NEWS_TIMEOUT)
    assert "news.timeout" in ids(a)


def test_news_exception():
    a = scan_file(NEWS_EXCEPTION)
    assert "news.exception" in ids(a)


def test_fusion_database_error_critical():
    a = scan_file(FUSION_DB_ERR)
    assert ids(a) == ["fusion.database_error"]
    assert a[0].severity == "critical"


def test_fusion_failed_to_write_critical():
    a = scan_file(FUSION_WRITE_FAIL)
    assert ids(a) == ["fusion.failed_to_write"]
    assert a[0].severity == "critical"


def test_fusion_fill_rate_low_threshold():
    a = scan_file(FUSION_FILL_LOW)
    assert "fusion.fill_rate_low" in ids(a)


# ── Negative tests (clean reports produce nothing) ───────────────────────────

@pytest.mark.parametrize("path", [
    HC_GATEWAY_OK, HC_FRESHNESS_OK, CALIB_OK,
    NEWS_OK, FUSION_OK, EMPTY, NONREPORT,
])
def test_negative_no_anomalies(path):
    # classify may return None for non-reports; skip those
    header = None
    first = path.read_text(encoding="utf-8", errors="replace").splitlines()[:1]
    if first and first[0].startswith("# Cron Job:"):
        header = first[0][len("# Cron Job:"):].strip()
    rc = det.classify_file(path, header)
    if rc is None:
        pytest.skip("not a tracked report type")
    a = det.scan_text_report(path, rc, header, False)
    assert a == [], f"expected no anomalies for {path.name}, got {ids(a)}"


# ── Threshold boundary behavior ─────────────────────────────────────────────

def test_calibration_win_rate_boundary_20_not_firing():
    p = _write("calib_20.md",
        "# Cron Job: fusion-calibration-report\n"
        "**Clean win rate: 20.0%** (10W / 40L).\n")
    a = scan_file(p)
    assert "calibration.win_rate_low" not in ids(a)


def test_fusion_fill_rate_boundary_85_not_firing():
    p = _write("fusion_85.md",
        "# Cron Job: run-signal-fusion\nfill rate 85.0% on target\n")
    a = scan_file(p)
    assert "fusion.fill_rate_low" not in ids(a)


# ── Zero-token fallback (provider outage) ──────────────────────────────────

def test_fallback_flag_set_on_provider_outage():
    a = scan_file(CALIB_BAD, provider_outage=True)
    assert a
    assert all(x.fallback_used for x in a)


def test_fallback_still_detects_criticals(tmp_path):
    """Simulated outage must still surface critical anomalies (100% coverage)."""
    report = tmp_path / "fusion_dberr.md"
    report.write_text(FUSION_DB_ERR.read_text())
    anoms = det.run_detection(tmp_path, provider_outage=True, limit=10)
    assert any(x.rule_id == "fusion.database_error" for x in anoms)
    assert all(x.fallback_used for x in anoms)


# ── Classification ──────────────────────────────────────────────────────────

def test_classify_by_header():
    assert det.classify_file(CALIB_BAD, "fusion-calibration-report") == "fusion_calibration"
    assert det.classify_file(NEWS_OK, "news-sentiment-catalyst") == "news_catalyst"
    assert det.classify_file(FUSION_OK, "run-signal-fusion") == "fusion_engine"


def test_classify_health_canary_by_name():
    assert det.classify_file(HC_GATEWAY_DOWN, None) == "health_canary"


def test_classify_unknown_returns_none():
    assert det.classify_file(NONREPORT, None) is None


# ── Enrichment present ───────────────────────────────────────────────────────

def test_git_and_metrics_enrichment_attached():
    a = scan_file(FUSION_DB_ERR)
    assert a
    an = a[0]
    # git context is a list (may be a stub line on failure, but never None)
    assert isinstance(an.git_context, list) and len(an.git_context) >= 1
    # system metrics dict always present with at least one key attempted
    assert isinstance(an.system_metrics, dict)


# ── End-to-end discovery across a tree ──────────────────────────────────────

def test_full_scan_tree(tmp_path):
    # Build a small tree with one anomaly and one clean file
    (tmp_path / "health_canary.jsonl").write_text(HC_GATEWAY_DOWN.read_text())
    sub = tmp_path / "job123"
    sub.mkdir()
    (sub / "report.md").write_text(CALIB_OK.read_text())
    anoms = det.run_detection(tmp_path, provider_outage=False, limit=50)
    assert any(x.rule_id == "health.gateway_down" for x in anoms)
    assert not any("calibration" in x.rule_id for x in anoms)


def test_exit_code_critical_vs_clean(tmp_path, monkeypatch):
    # critical path -> rc 2
    (tmp_path / "fusion_dberr.md").write_text(FUSION_DB_ERR.read_text())
    monkeypatch.chdir(tmp_path)
    rc = det.main(["--scan-root", str(tmp_path), "--quiet"])
    assert rc == 2

    # clean path -> rc 0
    clean = tmp_path / "clean"
    clean.mkdir()
    (clean / "fusion_ok.md").write_text(FUSION_OK.read_text())
    rc2 = det.main(["--scan-root", str(clean), "--quiet"])
    assert rc2 == 0
