#!/usr/bin/env python3
"""Read-only: boards-manifest entries for jarvis-os + sycode-trading."""
import json
import sys
from pathlib import Path

def main():
    p = Path("/home/frank/.hermes/kanban/boards-manifest.json")
    if not p.exists():
        print("manifest missing")
        sys.exit(1)
    d = json.loads(p.read_text())
    boards = d.get("boards", {})
    for slug in ("jarvis-os", "sycode-trading"):
        cfg = boards.get(slug, {})
        print(f"===== {slug} =====")
        print(json.dumps(cfg, indent=2))
    # which boards dispatch to trading profiles
    print("===== boards dispatching to trading profiles =====")
    for slug, cfg in boards.items():
        assignable = cfg.get("assignable_profiles") or cfg.get("profiles") or []
        if any("trading" in str(a) for a in assignable):
            print(slug, "->", assignable)

if __name__ == "__main__":
    main()
