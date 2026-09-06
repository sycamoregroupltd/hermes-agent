"""Command-line entry point for the fixture-driven liveness checker."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from . import FixtureError, exit_code, load_fixture


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Classify profile-aware provider liveness from a JSON fixture."
    )
    parser.add_argument("fixture", help="path to a deterministic JSON fixture")
    parser.add_argument(
        "--compact", action="store_true", help="emit compact rather than indented JSON"
    )
    args = parser.parse_args(argv)

    try:
        report = load_fixture(args.fixture)
    except FixtureError as exc:
        print(
            json.dumps(
                {"status": "invalid_fixture", "error": str(exc)}, sort_keys=True
            ),
            file=sys.stderr,
        )
        return 2

    print(
        json.dumps(
            report,
            indent=None if args.compact else 2,
            sort_keys=True,
        )
    )
    return exit_code(report)


if __name__ == "__main__":
    raise SystemExit(main())
