"""Execute the read-only session-bus delivery fixture resolver."""

from __future__ import annotations

import argparse
import json
import sys

from . import FixtureError, exit_code, load_fixture


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Resolve fresh process-bound recipients from an exported session-bus fixture."
    )
    parser.add_argument("fixture", help="path to an exported JSON fixture")
    args = parser.parse_args()
    try:
        report = load_fixture(args.fixture)
    except FixtureError as exc:
        print(
            json.dumps({"status": "invalid", "error": str(exc)}, sort_keys=True),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return exit_code(report)


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    raise SystemExit(main())
