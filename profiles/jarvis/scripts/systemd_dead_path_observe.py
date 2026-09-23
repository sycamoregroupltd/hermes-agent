#!/usr/bin/env python3
# Profile-local 15m guard-bundle adapter for observe-only systemd dead-path.
# Real file (not a symlink). Never forwards --apply-mask.
import os
import sys

os.execv(
    sys.executable,
    [
        sys.executable,
        "/home/frank/.hermes/scripts/systemd_dead_path_observe.py",
        *sys.argv[1:],
    ],
)
