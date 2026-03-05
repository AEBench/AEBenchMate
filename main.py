#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import generator
from utils import configure_logging, get_logger


logger = get_logger("main")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="AEBench oracle generator — generates artifact evaluator files",
    )
    parser.add_argument(
        "artifact_dir",
        type=Path,
        help="path to the artifact repository root",
    )
    parser.add_argument(
        "--readme",
        type=Path,
        default=None,
        help="path to artifact README (default: <artifact_dir>/README.md)",
    )
    parser.add_argument(
        "--howto",
        type=Path,
        required=True,
        help="path to HOWTO guide directory",
    )
    parser.add_argument(
        "--example-oracles",
        type=Path,
        nargs="*",
        default=[],
        metavar="DIR",
        help="paths to example oracle directories",
    )
    parser.add_argument(
        "--output-dir",
        default="_agent_eval",
        help="output directory name inside the artifact root (default: _agent_eval)",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=80,
        help="maximum agent turns for oracle generation (default: 80)",
    )
    parser.add_argument(
        "--max-repairs",
        type=int,
        default=2,
        help="maximum inline repair attempts after validation (default: 2)",
    )
    parser.add_argument(
        "--max-budget",
        type=float,
        default=1.0,
        metavar="USD",
        help="maximum spend in USD across all calls (default: 1.0)",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="model identifier to use (default: SDK default)",
    )
    parser.add_argument(
        "--effort",
        default="medium",
        choices=["low", "medium", "high"],
        help="effort level for generation (default: medium)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="print agent output lines to stderr",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        default=None,
        help="write debug logs to this file",
    )

    args = parser.parse_args()

    configure_logging(verbose=args.verbose, log_file=args.log_file)

    artifact_dir = args.artifact_dir.resolve()
    if not artifact_dir.is_dir():
        print(f"error: artifact_dir does not exist or is not a directory: {artifact_dir}", file=sys.stderr)
        return 2

    readme_path = args.readme if args.readme is not None else artifact_dir / "README.md"
    if not readme_path.is_file():
        print(f"error: README not found: {readme_path}", file=sys.stderr)
        return 2

    howto_path = args.howto.resolve()
    if not howto_path.exists():
        print(f"error: howto path does not exist: {howto_path}", file=sys.stderr)
        return 2

    task = generator.TaskSpec(
        artifact_dir=artifact_dir,
        artifact_readme=readme_path.resolve(),
        howto_path=howto_path,
        example_oracle_dirs=tuple(p.resolve() for p in args.example_oracles),
        output_dir_name=args.output_dir,
        max_turns=args.max_turns,
        max_repairs=args.max_repairs,
        max_budget_usd=args.max_budget,
        model=args.model,
        effort=args.effort,
        verbose=args.verbose,
    )

    logger.info(
        "starting: artifact_dir=%s readme=%s howto=%s",
        task.artifact_dir,
        task.artifact_readme,
        task.howto_path,
    )

    result = generator.run(task)

    if result.success:
        print(f"oracle generation succeeded after {result.attempts} attempt(s).")
    else:
        print(f"oracle generation failed after {result.attempts} attempt(s).", file=sys.stderr)

    if result.spent_usd is not None:
        print(f"estimated cost: ${result.spent_usd:.4f}")

    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
