#!/usr/bin/env python3
"""secret_redact.py — scrub secret material before persisting captured text.

These Hermes helper scripts shell out to `curl -H "X-Sycode-Token:..."`. When
the subprocess raises (e.g. TimeoutExpired) Python repr()s the argv list, which
embeds the token verbatim. The cron runner captures that stderr into a job's
`last_error` and (because jobs.json is git-tracked) it becomes a committed
secret. This module provides a single `redact()` used to mask the token before
any error string is logged, printed, or persisted.

Standard library only so it can be imported from any Hermes script.
"""
import re

# Token-shaped secrets that must never be persisted in plaintext.
_PATTERNS = [
    # OpenClaw / Sycode auth header value (covers the curl -H form and the
    # repr() of the argv list that leaks it).
    re.compile(r"(X-Sycode-Token):[0-9a-fA-F]{16,}"),
    # Env-style assignments that might appear in logs.
    re.compile(r"(SYCODE_READ_TOKEN|OPENCLAW_READ_TOKEN)[=:][0-9a-fA-F]{16,}"),
    # `-H 'X-Sycode-Token:...'` / `-H "X-Sycode-Token:..."` forms.
    re.compile(r"(-H\s+['\"]?X-Sycode-Token):[0-9a-fA-F]{16,}"),
]

_REDACTED = "***REDACTED***"


def redact(text):
    """Return `text` with any recognised secret value replaced by ***REDACTED***."""
    if not text:
        return text
    out = text
    for pat in _PATTERNS:
        out = pat.sub(lambda m: f"{m.group(1)}:{_REDACTED}", out)
    return out


if __name__ == "__main__":
    import sys
    sys.stdout.write(redact(sys.stdin.read()))
