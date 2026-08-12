#!/usr/bin/env bash
# RESHAPE W3 top-up: same idempotent script as the backfill (resumes from max(open_time)).
# Canonical logic: /home/frank/orthogonal-collectors/perp_backfill_4h.py. Exit rc = liveness.
exec /usr/bin/python3 /home/frank/orthogonal-collectors/perp_backfill_4h.py
