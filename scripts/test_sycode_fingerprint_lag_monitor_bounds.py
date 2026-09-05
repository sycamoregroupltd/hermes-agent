#!/usr/bin/env python3
"""Static bounds for t_bce90116: fingerprint lag probes must not seq-scan
signal_fingerprints without a journeys.triggered_at window.

Run: python3 -m pytest scripts/test_sycode_fingerprint_lag_monitor_bounds.py -q
"""
from __future__ import annotations

import ast
import os
import pathlib
import re
import stat
import subprocess
import time

SRC = pathlib.Path(__file__).with_name("sycode_fingerprint_lag_monitor.py")
FP_SHIM = pathlib.Path(__file__).with_name("sycode_fingerprint_lag_monitor.sh")
COLLECTOR_SHIM = (
    pathlib.Path(__file__).resolve().parents[1]
    / "profiles"
    / "trading-devops"
    / "scripts"
    / "sycode_db_latency_slo_collector.sh"
)


def _sql_literals(tree: ast.AST) -> list[str]:
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            v = node.value
            if re.search(r"^\s*SELECT\b", v, re.IGNORECASE | re.MULTILINE) and re.search(
                r"\bFROM\s+\w+", v, re.IGNORECASE
            ):
                out.append(v)
    return out


def test_no_unbounded_fingerprint_table_scan():
    src = SRC.read_text()
    tree = ast.parse(src)
    sqls = _sql_literals(tree)
    assert sqls, "expected SQL string literals in monitor"
    bare = re.compile(
        r"FROM\s+signal_fingerprints\s*(;|$)",
        re.IGNORECASE | re.MULTILINE,
    )
    created_at_window = re.compile(
        r"FROM\s+signal_fingerprints[\s\S]{0,200}created_at\s*>=",
        re.IGNORECASE,
    )
    offenders = []
    for sql in sqls:
        if bare.search(sql) or created_at_window.search(sql):
            offenders.append(sql.strip()[:180])
    assert not offenders, (
        "unbounded or created_at-windowed scan of signal_fingerprints "
        f"(no created_at index): {offenders}"
    )


def test_every_fingerprint_sql_joins_journeys_triggered_at():
    src = SRC.read_text()
    tree = ast.parse(src)
    sqls = [s for s in _sql_literals(tree) if "signal_fingerprints" in s.lower()]
    assert sqls, "expected fingerprint SQL"
    for sql in sqls:
        assert "signal_journeys" in sql.lower(), sql[:160]
        assert "triggered_at" in sql.lower(), sql[:160]
        assert "correlation_id" in sql.lower(), sql[:160]


def test_timeouts_are_bounded_under_shim_wall():
    src = SRC.read_text()
    assert 'os.getenv("GQT_FP_STMT_TIMEOUT", "20s")' in src
    assert 'os.getenv("GQT_FP_PSQL_TIMEOUT_S", "25")' in src
    assert 'os.getenv("GQT_FP_PROBE_BUDGET_S", "55")' in src
    assert "_is_timeout_err" in src
    assert "probe budget exhausted" in src
    assert "acquire_oltp_lock" in src
    assert "LOCK_EX" in src
    fp_shim = FP_SHIM.read_text()
    assert 'GQT_FP_WALL_S:-60' in fp_shim
    assert "exit 3" in fp_shim
    assert "OLTP_PROBE_SKIP locked" in fp_shim
    col = COLLECTOR_SHIM.read_text()
    assert "SYCODE_DB_SLO_WALL_S:-75" in col
    assert "collector_success 0" in col
    assert "write_fail_closed" in col
    assert "timeout --kill-after=5s 30s" not in fp_shim
    assert "timeout --kill-after=5s 30s" not in col


def test_fingerprint_shim_maps_timeout_to_exit_3(tmp_path):
    hang = tmp_path / "hang.py"
    hang.write_text("import time; time.sleep(30)\n")
    hang.chmod(hang.stat().st_mode | stat.S_IEXEC)
    env = {
        **dict(os.environ),
        "GQT_FP_PY": str(hang),
        "GQT_FP_WALL_S": "1",
        "SYCODE_OLTP_LOCK_DIR": str(tmp_path),
    }
    t0 = time.monotonic()
    r = subprocess.run(
        ["bash", str(FP_SHIM)],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    elapsed = time.monotonic() - t0
    assert r.returncode == 3, r.stdout + r.stderr
    assert "PROBE FAILURE" in (r.stdout + r.stderr)
    assert elapsed < 10


def test_collector_shim_fail_closed_prom_on_timeout(tmp_path):
    hang = tmp_path / "hang.py"
    hang.write_text("import time; time.sleep(30)\n")
    prom = tmp_path / "sycode_db_latency_slo.prom"
    env = {
        **dict(os.environ),
        "SYCODE_DB_SLO_COLLECTOR_PY": str(hang),
        "SYCODE_DB_SLO_PROM": str(prom),
        "SYCODE_DB_SLO_WALL_S": "1",
        "SYCODE_OLTP_LOCK_DIR": str(tmp_path),
    }
    r = subprocess.run(
        ["bash", str(COLLECTOR_SHIM)],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    assert r.returncode == 124, r.stdout + r.stderr
    text = prom.read_text()
    assert "sycode_db_collector_success 0" in text
    assert "sycode_db_collector_last_run_timestamp" in text
    assert "OLTP_PROBE_FAIL" in (r.stdout + r.stderr)
