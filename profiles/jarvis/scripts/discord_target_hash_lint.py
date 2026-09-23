#!/usr/bin/env python3
# Profile-local 15m guard-bundle adapter for observe-only discord:# hash lint.
# Real file (not a symlink). Never mutates targets.
import os
import sys

os.execv(
    sys.executable,
    [
        sys.executable,
        "/home/frank/.hermes/scripts/discord_target_hash_lint.py",
        *sys.argv[1:],
    ],
)
