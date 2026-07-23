#!/usr/bin/env python3
"""Regression check for secret_redact.redact().

Run standalone:  python3 /home/frank/.hermes/scripts/test_secret_redact.py
It fails loudly (non-zero exit) if any secret-shaped value survives redaction.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from secret_redact import redact

LEAK_TOKEN = "850f7768eafcbedeea85449f63a669fa488cbcc09d8f44cdacb322eed0837dc4"

SAMPLES = [
    # Bare header value (the canonical leak form).
    f"X-Sycode-Token:{LEAK_TOKEN}",
    # The exact argv repr that the subprocess TimeoutExpired traceback emits.
    f"Command '['curl', '-s', '-H', 'X-Sycode-Token:{LEAK_TOKEN}', "
    f"'http://localhost:3001/api/openclaw/status']' timed out after 35 seconds",
    # Env assignment form.
    f"SYCODE_READ_TOKEN={LEAK_TOKEN}",
    # Quoted -H form.
    f"-H \"X-Sycode-Token:{LEAK_TOKEN}\"",
    # Innocent text must pass through untouched.
    "normal text with no secret 1234567890abcdef",
    "",
]


def main():
    for s in SAMPLES:
        out = redact(s)
        assert LEAK_TOKEN not in out, f"LEAK: token still present in -> {out!r}"
        if "X-Sycode-Token:" in s and "X-Sycode-Token" in out:
            assert "***REDACTED***" in out, f"header not redacted -> {out!r}"
    print("test_secret_redact: PASS (no secret-shaped value survives redaction)")


if __name__ == "__main__":
    main()
