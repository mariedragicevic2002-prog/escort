"""
CLI entry point for the simulation framework.

Usage:
    python -m tests.simulation.runner [--count N] [--seed S] [--quiet]

Options:
    --count N    Number of conversations to generate (default: 500)
    --seed S     Global random seed (default: 42)
    --quiet      Suppress progress output
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests.simulation import generator as gen


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the Adella chatbot conversation simulation framework."
    )
    parser.add_argument("--count", type=int, default=gen.TARGET_COUNT,
                        help="Number of conversations to generate (default: 500)")
    parser.add_argument("--seed", type=int, default=gen.GLOBAL_SEED,
                        help="Global random seed (default: 42)")
    parser.add_argument("--quiet", action="store_true",
                        help="Suppress progress output")
    args = parser.parse_args()

    gen.TARGET_COUNT = args.count
    gen.GLOBAL_SEED = args.seed

    gen.run(verbose=not args.quiet)


if __name__ == "__main__":
    main()
