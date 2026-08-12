#!/usr/bin/env bash
# HL maker measurement campaign tick (TESTNET validation until HL_MEASURE_MAINNET=1).
# Owner's memo ask #1; spec + firewall in /home/frank/hl-maker-measurement/. Exit rc = liveness.
export HL_MEASURE_MAINNET=1
exec /home/frank/hl-maker-measurement/.venv/bin/python /home/frank/hl-maker-measurement/campaign_tick.py
