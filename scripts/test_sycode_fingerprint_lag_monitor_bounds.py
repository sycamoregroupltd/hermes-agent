#!/usr/bin/env python3
"""Static bounds for t_bce90116: fingerprint lag probes must not seq-scan
signal_fingerprints without a journeys.triggered_at window.

Run: python3 -m pytest scripts/test_sycode_fingerprint_lag_monitor_bounds.py -q
"""
from __future__ import annotations

import ast
import pathlib
import re

SRC = pathlib.Path(__file__).with_name("sycode_fingerprint_lag_monitor.py")


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


def test_timeouts_are_bounded_under_30s_statement():
    src = SRC.read_text()
    assert 'os.getenv("GQT_FP_STMT_TIMEOUT", "20s")' in src
    assert 'os.getenv("GQT_FP_PSQL_TIMEOUT_S", "30")' in src
    assert "acquire_oltp_lock" in src
    assert "LOCK_EX" in src
