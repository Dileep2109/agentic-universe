"""Run the controller until the connected scenario finishes.

Usage:
    python -m controller --url http://127.0.0.1:8700
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from .loop import run
from .store import WorldReset


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Formi call-agent controller")
    parser.add_argument("--url", required=True, help="simulator base URL")
    parser.add_argument("--state-dir", default="state", help="directory for the SQLite checkpoint")
    parser.add_argument("--tick-minutes", type=int, default=15, help="simulated minutes per advance")
    parser.add_argument(
        "--max-advances",
        type=int,
        default=None,
        help="stop after this many confirmed advances (tests only; run.sh does not set it)",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    try:
        summary = run(
            url=args.url,
            state_dir=args.state_dir,
            tick_minutes=args.tick_minutes,
            max_advances=args.max_advances,
        )
    except WorldReset as exc:
        logging.getLogger("controller").error("%s", exc)
        return 2
    except Exception:
        logging.getLogger("controller").exception("controller failed")
        return 1
    json.dump(summary, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
