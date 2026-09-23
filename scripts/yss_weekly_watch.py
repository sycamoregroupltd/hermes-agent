#!/usr/bin/env python3
"""Thin cron entrypoint: no_agent weekly watcher for the YSS truth layer.

Wraps yss_ingest.py's cmd_watch() so the cron job can point --script at a
zero-argument file (hermes cron create's --script mode does not pass argv).
"""
import sys
sys.path.insert(0, "/home/frank/.hermes/scripts")
from yss_ingest import cmd_watch

if __name__ == "__main__":
    sys.exit(cmd_watch())
