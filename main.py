#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

import extractor
import generator
from utils import (
    EvalSpec,
    configure_logging,
    load_env_file,
    resolve_log_file,
    write_json_file,
)
from validator import ValidationReport, create_missing_refs, validate

logger = logging.getLogger("main")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="AEBench oracle generator",
    )
    parser.add_argument("artifact_dir", type=Path)
    parser.add_argument("--artifact-id", default=None)
    parser.add_argument("--readme", type=Path, default=None)
    parser.add_argument("--howto", type=Path, required=True)
    parser.add_argument("--primitives-dir", type=Path, default=None)
    parser.add_argument("--examples-dir", type=Path, default=None)
    parser.add_argument("--example-oracles", type=Path, nargs="*", default=[])
    parser.add_argument("--output-dir", default="_agent_eval")
    parser.add_argument("--max-turns", type=int, default=80)
    parser.add_argument("--max-repairs", type=int, default=2)
    parser.add_argument("--max-budget", type=float, default=1.0, metavar="USD")
    parser.add_argument("--model", default=None)
    parser.add_argument("--effort", default="medium", choices=["low", "medium", "high"])
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--log-file", type=Path, default=None)
    parser.add_argument("--env-file", type=Path, default=None)

    args = parser.parse_args()

    configure_logging(verbose=args.verbose, log_file=args.log_file)

    if args.env_file:
        load_env_file(args.env_file)

    artifact_dir = args.artifact_dir.resolve()
    if not artifact_dir.is_dir():
        print(f"error: artifact_dir not found: {artifact_dir}", file=sys.stderr)
        return 2

    readme_path = args.readme if args.readme is not None else artifact_dir / "README.md"
    if not readme_path.is_file():
        print(f"error: README not found: {readme_path}", file=sys.stderr)
        return 2

    howto_path = args.howto.resolve()
    if not howto_path.exists():
        print(f"error: howto path not found: {howto_path}", file=sys.stderr)
        return 2

    artifact_id = args.artifact_id or artifact_dir.name

    spec = EvalSpec(
        artifact_id=artifact_id,
        artifact_dir=artifact_dir,
        artifact_readme=readme_path.resolve(),
        howto_path=howto_path,
        primitives_dir=args.primitives_dir.resolve() if args.primitives_dir else None,
        examples_dir=args.examples_dir.resolve() if args.examples_dir else None,
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
        "starting pipeline: artifact_id=%s artifact_dir=%s",
        spec.artifact_id,
        spec.artifact_dir,
    )

    async def _pipeline() -> tuple[generator.GenerationResult, ValidationReport]:
        readme_text = readme_path.read_text(encoding="utf-8")

        plan, warnings = await extractor.extract_artifact_plan(
            spec, readme_text, readme_path.name
        )

        for w in warnings:
            logger.warning("extraction warning: %s", w)

        plan_path = artifact_dir / "artifact_plan.json"
        write_json_file(plan_path, plan, indent=2)
        spec.artifact_plan_path = plan_path

        logger.info(
            "artifact plan written to %s stages: env=%d build=%d prep=%d run=%d",
            plan_path,
            len(plan.get("stages", {}).get("env_setup", [])),
            len(plan.get("stages", {}).get("build_artifact", [])),
            len(plan.get("stages", {}).get("benchmark_prep", [])),
            len(plan.get("stages", {}).get("experiment_runs", [])),
        )

        result = await generator.generate(spec)
        create_missing_refs(artifact_dir)
        report = validate(artifact_dir)

        repairs = 0
        while not report.ok and repairs < spec.max_repairs:
            repairs += 1
            logger.info(
                "starting repair attempt %d/%d issues=%d",
                repairs,
                spec.max_repairs,
                len(report.issues),
            )

            feedback_path = artifact_dir / "_validation_feedback.txt"
            feedback_lines = [
                f"[{i.severity}] {i.code} {i.path}: {i.message}"
                for i in report.issues
            ]
            feedback_path.write_text("\n".join(feedback_lines), encoding="utf-8")
            spec.feedback_path = feedback_path

            result = await generator.generate(spec)
            create_missing_refs(artifact_dir)
            report = validate(artifact_dir)

        return result, report

    result, report = asyncio.run(_pipeline())

    error_count = sum(1 for i in report.issues if i.severity == "error")
    warning_count = sum(1 for i in report.issues if i.severity == "warning")

    if result.success and report.ok:
        print(f"succeeded after {result.attempts} attempt(s).")
    else:
        print(
            f"finished with issues after {result.attempts} attempt(s). "
            f"errors={error_count} warnings={warning_count}",
            file=sys.stderr,
        )
        for issue in report.issues:
            print(f"  [{issue.severity}] {issue.code} {issue.path}: {issue.message}", file=sys.stderr)

    if result.cost_usd is not None:
        print(f"estimated cost: ${result.cost_usd:.4f}")

    return 0 if (result.success and report.ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
