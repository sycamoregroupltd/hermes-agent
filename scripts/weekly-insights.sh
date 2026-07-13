#!/usr/bin/env bash
hermes insights --days 7 > /home/frank/uaa-rules/INSIGHTS-WEEKLY.md 2>&1 && echo "[SILENT] insights refreshed"
