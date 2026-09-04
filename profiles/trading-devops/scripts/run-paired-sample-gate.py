#!/usr/bin/env python3
"""Raise session statement_timeout then exec paired-sample-gate.py.

tick_trades GROUP BY scans exceed the DB default 30s/1min timeout
(dgx-trading-data-engineering §9). 6h-chunked 7d fetches still need a
raised session timeout under load. fetch_minute_bars must NOT overwrite
this 600s value (180s overwrite was the r4 QueryCanceled cause).
This wrapper does not change the 200 paired-n gate, pooling, or any
span heuristic.
"""
from __future__ import annotations

import runpy
import sys

import psycopg2

_ORIG_CONNECT = psycopg2.connect


def _connect(*args, **kwargs):
    conn = _ORIG_CONNECT(*args, **kwargs)
    ac = conn.autocommit
    conn.autocommit = True
    cur = conn.cursor()
    try:
        cur.execute("SET statement_timeout = '600s'")
    finally:
        cur.close()
        conn.autocommit = ac
    return conn


psycopg2.connect = _connect

if len(sys.argv) < 2:
    sys.stderr.write("usage: run-paired-sample-gate.py <gate.py> [args...]\n")
    sys.exit(2)

gate = sys.argv[1]
sys.argv = [gate, *sys.argv[2:]]
runpy.run_path(gate, run_name="__main__")
