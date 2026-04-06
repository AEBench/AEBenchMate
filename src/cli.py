#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from case_builder import build_case_bundle, write_case_toml
from extractor import extract_artifact_plan
import dataclasses

from generator import generate as generate_oracles
from utils import (
    EvalSpec,
    configure_logging,
    load_guidance_root,
    load_env_file,
    resolve_log_file,
    write_json_file,
)
from validator import (
    create_missing_refs,
    _report_dict as validation_report_dict,
    validate,
    validate_artifact_plan,
)
from dashboard import (
    print_banner,
    print_footer_mode,
    print_info,
    print_stage_header,
    print_success,
    print_warning,
)

logger = logging.getLogger("cli")


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _add_shared_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--artifact-id", required=False)
    parser.add_argument("--artifact-dir", required=True)
    parser.add_argument("--artifact-plan-path")
    parser.add_argument("--guidance-dir",
                        help="Root dir with howtos/, primitives/, examples/ subdirs. "
                             "Auto-discovers guidance paths unless overridden.")
    parser.add_argument("--oracle-primitives-dir",
                        help="Path to oracle building-block modules.")
    parser.add_argument("--oracle-examples-dir",
                        help="Path to example oracle cases.")
    parser.add_argument("--output-dir-name", default="_agent_eval")
    parser.add_argument("--max-turns", type=int, default=80)
    parser.add_argument("--max-repairs", type=int, default=2)
    parser.add_argument("--max-budget-usd", type=float, default=1.0)
    parser.add_argument("--model")
    parser.add_argument(
        "--effort",
        choices=("low", "medium", "high", "max"),
        default="medium",
    )
    parser.add_argument("--feedback-path")
    parser.add_argument("--report-path")
    parser.add_argument("--skip-validation", action="store_true")
    parser.add_argument("--verbose", action="store_true")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="CLI: extract, generate, validate, or full pipeline.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    extract_parser = subparsers.add_parser("extract")
    _add_shared_args(extract_parser)
    extract_parser.add_argument("--artifact-readme", required=True)

    generate_parser = subparsers.add_parser("generate")
    _add_shared_args(generate_parser)
    generate_parser.add_argument("--artifact-readme", required=True)
    generate_parser.add_argument("--howto-path",
                                 help="Path to howto directory (contains add_case.md).")

    validate_parser = subparsers.add_parser("validate")
    _add_shared_args(validate_parser)
    validate_parser.add_argument("--artifact-readme")
    validate_parser.add_argument("--howto-path")
    validate_parser.add_argument(
        "--target",
        choices=("all", "plan", "oracle"),
        default="all",
    )

    pipeline_parser = subparsers.add_parser("pipeline")
    _add_shared_args(pipeline_parser)
    pipeline_parser.add_argument("--artifact-readme", required=True)
    pipeline_parser.add_argument("--howto-path",
                                 help="Path to howto directory (contains add_case.md).")
    pipeline_parser.add_argument("--case-dir",
                                 help="Where to write case bundle. Skipped if omitted.")
    pipeline_parser.add_argument("--upstream-url",
                                 help="Upstream git URL for case.toml.")
    pipeline_parser.add_argument("--upstream-ref",
                                 help="Full commit hash for case.toml.")

    return parser.parse_args(argv)


def _make_spec(
    args: argparse.Namespace,
    guidance_root: Path | None = None,
) -> EvalSpec:
    guidance_dir_arg = getattr(args, "guidance_dir", None)
    guidance_dir = Path(guidance_dir_arg) if guidance_dir_arg else guidance_root

    howto_path: Path | None = None
    howto_arg = getattr(args, "howto_path", None)
    if howto_arg:
        howto_path = Path(howto_arg)
    elif guidance_dir is not None and (guidance_dir / "howtos").is_dir():
        howto_path = guidance_dir / "howtos"

    primitives_dir: Path | None = None
    primitives_arg = getattr(args, "oracle_primitives_dir", None)
    if primitives_arg:
        primitives_dir = Path(primitives_arg)
    elif guidance_dir is not None and (guidance_dir / "primitives").is_dir():
        primitives_dir = guidance_dir / "primitives"

    examples_dir: Path | None = None
    examples_arg = getattr(args, "oracle_examples_dir", None)
    if examples_arg:
        examples_dir = Path(examples_arg)
    elif guidance_dir is not None and (guidance_dir / "examples").is_dir():
        examples_dir = guidance_dir / "examples"

    raw_case_dir = getattr(args, "case_dir", None)
    output_base_dir: Path | None = Path(raw_case_dir) if raw_case_dir else None

    spec = EvalSpec(
        artifact_id=args.artifact_id if args.artifact_id else Path(args.artifact_dir).name,
        artifact_dir=Path(args.artifact_dir),
        artifact_readme=Path(args.artifact_readme) if args.artifact_readme else None,
        howto_path=howto_path,
        artifact_plan_path=Path(args.artifact_plan_path) if args.artifact_plan_path else None,
        primitives_dir=primitives_dir,
        examples_dir=examples_dir,
        output_dir_name=args.output_dir_name,
        output_base_dir=output_base_dir,
        max_turns=args.max_turns,
        max_repairs=args.max_repairs,
        max_budget_usd=args.max_budget_usd,
        model=args.model,
        effort=args.effort,
        feedback_path=Path(args.feedback_path) if args.feedback_path else None,
        verbose=args.verbose,
    )
    return spec.resolved()


def _artifact_plan_output_path(spec: EvalSpec) -> Path:
    if spec.artifact_plan_path is not None:
        return spec.artifact_plan_path
    return spec.default_artifact_plan_path


def _print_and_write_report(
    payload: dict[str, Any],
    report_path_value: str | None,
) -> None:
    text = json.dumps(payload, indent=2, sort_keys=True, default=str)

    if report_path_value:
        report_path = Path(report_path_value)
        write_json_file(report_path, payload, indent=2, sort_keys=True)
        logger.info("wrote report to %s", report_path)

    print(text, flush=True)


def _write_feedback_file(
    path: Path,
    *,
    stage: str,
    attempt: int,
    report: dict[str, Any],
) -> None:
    payload = {
        "stage": stage,
        "attempt": attempt,
        "ok": report.get("ok", False),
        "issues": report.get("issues", []),
    }
    write_json_file(path, payload, indent=2, sort_keys=True)


def _print_validation_report(stage_name: str, report: dict[str, Any]) -> None:
    ok = bool(report.get("ok", False))
    issues = report.get("issues", [])
    issue_count = len(issues)

    if ok:
        print_success(f"{stage_name} validation passed ({issue_count} issues)")
        return

    print_warning(f"{stage_name} validation failed ({issue_count} issues)")

    if not issues:
        print_warning("validator reported failure but returned no detailed issues")
        return

    for issue in issues[:12]:
        severity = str(issue.get("severity", ""))
        code = str(issue.get("code", ""))
        path = str(issue.get("path", ""))
        message = str(issue.get("message", ""))

        print_warning(f"[{severity}] {code} {path}: {message}")

    remaining = issue_count - min(issue_count, 12)
    if remaining > 0:
        print_warning(f"... and {remaining} more issue(s)")


async def _run_extract(spec: EvalSpec) -> dict[str, Any]:
    if spec.artifact_readme is None:
        raise ValueError("artifact_readme is required for extract")

    readme_text = spec.artifact_readme.read_text("utf-8")
    readme_path = spec.artifact_readme.as_posix()

    plan, warnings = await extract_artifact_plan(
        spec,
        readme_text=readme_text,
        readme_path=readme_path,
    )

    artifact_plan_path = _artifact_plan_output_path(spec)
    write_json_file(artifact_plan_path, plan, indent=2, sort_keys=False)

    payload = {
        "artifact_plan_path": str(artifact_plan_path),
        "cost_usd": spec.cost_usd,
        "warnings": warnings,
    }
    return payload


def _run_validate_extract(spec: EvalSpec) -> dict[str, Any]:
    print_stage_header("validation (extractor)", spec.artifact_id)
    print_info("validating extracted artifact plan")

    artifact_plan_path = _artifact_plan_output_path(spec)
    report = validate_artifact_plan(artifact_plan_path)
    payload = validation_report_dict(report)
    _print_validation_report("extractor", payload)
    return payload


async def _run_extract_with_retries(
    spec: EvalSpec,
) -> tuple[dict[str, Any], dict[str, Any]]:
    original_feedback_path = spec.feedback_path
    feedback_path = spec.artifact_dir / ".extract_feedback.json"

    last_extract_payload: dict[str, Any] | None = None
    last_plan_report: dict[str, Any] | None = None

    try:
        for attempt in range(1, spec.max_repairs + 2):
            if attempt == 1:
                spec.feedback_path = original_feedback_path
            else:
                spec.feedback_path = feedback_path

            logger.info("extract attempt %d", attempt)
            if attempt > 1:
                print_info(f"extract retry attempt {attempt}")

            last_extract_payload = await _run_extract(spec)
            last_plan_report = _run_validate_extract(spec)

            if last_plan_report["ok"]:
                return last_extract_payload, last_plan_report

            if attempt <= spec.max_repairs:
                _write_feedback_file(
                    feedback_path,
                    stage="extractor",
                    attempt=attempt,
                    report=last_plan_report,
                )

        assert last_extract_payload is not None
        assert last_plan_report is not None
        return last_extract_payload, last_plan_report
    finally:
        spec.feedback_path = original_feedback_path


def _run_validate_generate(spec: EvalSpec) -> dict[str, Any]:
    print_stage_header("validation (generator)", spec.artifact_id)
    print_info("validating generated oracle package")

    output_root = spec.output_dir.parent
    create_missing_refs(output_root)
    report = validate(output_root)
    payload = validation_report_dict(report)
    _print_validation_report("generator", payload)
    return payload


async def _run_generate_with_retries(
    spec: EvalSpec,
) -> tuple[dict[str, Any], dict[str, Any]]:
    original_feedback_path = spec.feedback_path
    feedback_path = spec.artifact_dir / ".generate_feedback.json"

    last_generate_payload: dict[str, Any] | None = None
    last_oracle_report: dict[str, Any] | None = None

    try:
        for attempt in range(1, spec.max_repairs + 2):
            if attempt == 1:
                spec.feedback_path = original_feedback_path
            else:
                spec.feedback_path = feedback_path

            logger.info("generate attempt %d", attempt)
            if attempt > 1:
                print_info(f"generate retry attempt {attempt}")

            last_generate_payload = await _run_generate(spec)
            last_oracle_report = _run_validate_generate(spec)

            ok = bool(last_generate_payload["success"]) and bool(last_oracle_report["ok"])
            if ok:
                return last_generate_payload, last_oracle_report

            if attempt <= spec.max_repairs:
                _write_feedback_file(
                    feedback_path,
                    stage="generator",
                    attempt=attempt,
                    report=last_oracle_report,
                )

        assert last_generate_payload is not None
        assert last_oracle_report is not None
        return last_generate_payload, last_oracle_report
    finally:
        spec.feedback_path = original_feedback_path


async def _run_generate(spec: EvalSpec) -> dict[str, Any]:
    result = await generate_oracles(spec)
    return dataclasses.asdict(result)


def _run_validate(spec: EvalSpec, target: str) -> dict[str, Any]:
    payload: dict[str, Any] = {}

    if target in {"all", "plan"}:
        payload["plan"] = _run_validate_extract(spec)

    if target in {"all", "oracle"}:
        payload["oracle"] = _run_validate_generate(spec)

    return payload


def _run_build_case_bundle(
    spec: EvalSpec,
    *,
    case_dir: Path,
    upstream_url: str,
    upstream_ref: str,
) -> dict[str, Any]:
    """Build case bundle from _agent_eval/ output."""
    print_stage_header("case bundle", spec.artifact_id)

    agent_eval_dir = spec.output_dir

    readme_path_in_artifact = "README.md"
    if spec.artifact_readme is not None:
        try:
            readme_path_in_artifact = spec.artifact_readme.relative_to(spec.artifact_dir).as_posix()
        except ValueError:
            readme_path_in_artifact = spec.artifact_readme.name

    toml_content = write_case_toml(
        case_id=spec.artifact_id,
        upstream_url=upstream_url,
        upstream_ref=upstream_ref,
        readme_path_in_artifact=readme_path_in_artifact,
    )
    written = build_case_bundle(
        agent_eval_dir=agent_eval_dir,
        case_dir=case_dir,
        case_toml_content=toml_content,
    )
    print_success(f"case bundle written to {case_dir} ({len(written)} files)")
    print_info("Next steps:")
    print_info("  1. Review oracle/ files and fill in TODOs in case.toml")
    print_info("  2. Add ref data to refs/ if needed")
    print_info("  3. Register the case in cases.json")
    return {"case_dir": str(case_dir), "files_written": written}


async def _run_pipeline(
    spec: EvalSpec,
    *,
    skip_validation: bool,
) -> dict[str, Any]:
    if skip_validation:
        extract_payload = await _run_extract(spec)
        generate_payload = await _run_generate(spec)

        ok = bool(generate_payload["success"])

        return {
            "extract": extract_payload,
            "generate": generate_payload,
            "validate": {
                "plan": None,
                "oracle": None,
            },
            "ok": ok,
        }

    extract_payload, plan_report = await _run_extract_with_retries(spec)

    if not plan_report["ok"]:
        return {
            "extract": extract_payload,
            "generate": None,
            "validate": {
                "plan": plan_report,
                "oracle": None,
            },
            "ok": False,
        }

    generate_payload, oracle_report = await _run_generate_with_retries(spec)

    ok = bool(plan_report["ok"]) and bool(generate_payload["success"]) and bool(oracle_report["ok"])

    return {
        "extract": extract_payload,
        "generate": generate_payload,
        "validate": {
            "plan": plan_report,
            "oracle": oracle_report,
        },
        "ok": ok,
    }


def main(argv: list[str] | None = None) -> int:
    root = _project_root()
    load_env_file(root / ".env")

    args = parse_args(argv if argv is not None else sys.argv[1:])
    log_file = resolve_log_file(root, os.environ.get("ARTIFACT_EVAL_DEBUG_LOG"))
    configure_logging(verbose=args.verbose, log_file=log_file)

    print_banner()
    print_footer_mode(f"mode: {args.command}")

    guidance_root = load_guidance_root(root)
    spec = _make_spec(args, guidance_root)

    if args.command == "extract":
        logger.info("running EXTRACTOR")
        print_info("running EXTRACTOR")

        if args.skip_validation:
            extract_payload = asyncio.run(_run_extract(spec))
            payload = {
                "extract": extract_payload,
                "validate": {
                    "plan": None,
                },
                "ok": True,
            }
        else:
            extract_payload, plan_report = asyncio.run(_run_extract_with_retries(spec))
            payload = {
                "extract": extract_payload,
                "validate": {
                    "plan": plan_report,
                },
                "ok": bool(plan_report["ok"]),
            }

        _print_and_write_report(payload, args.report_path)

        if payload["ok"]:
            print_success("extract completed")
        else:
            print_warning("extract completed with validation errors")
        return 0 if payload["ok"] else 1

    if args.command == "generate":
        logger.info("running GENERATOR")
        print_info("running GENERATOR")

        if args.skip_validation:
            generate_payload = asyncio.run(_run_generate(spec))
            payload = {
                "generate": generate_payload,
                "validate": {
                    "oracle": None,
                },
                "ok": bool(generate_payload["success"]),
            }
        else:
            generate_payload, oracle_report = asyncio.run(_run_generate_with_retries(spec))
            ok = bool(generate_payload["success"]) and bool(oracle_report["ok"])
            payload = {
                "generate": generate_payload,
                "validate": {
                    "oracle": oracle_report,
                },
                "ok": ok,
            }

        _print_and_write_report(payload, args.report_path)

        if payload["ok"]:
            print_success("generate completed")
        else:
            print_warning("generate completed with errors")
        return 0 if payload["ok"] else 1

    if args.command == "validate":
        logger.info("running VALIDATOR")
        print_info("running VALIDATOR")
        payload = _run_validate(spec, args.target)
        _print_and_write_report(payload, args.report_path)

        ok = True
        for report in payload.values():
            if not report["ok"]:
                ok = False
                break

        if ok:
            print_success("validate completed")
        else:
            print_warning("validate completed with errors")
        return 0 if ok else 1

    if args.command == "pipeline":
        logger.info("running pipeline")
        print_info("running pipeline")
        payload = asyncio.run(
            _run_pipeline(
                spec,
                skip_validation=args.skip_validation,
            )
        )

        case_bundle_payload: dict[str, Any] | None = None
        case_dir_arg = getattr(args, "case_dir", None)
        if case_dir_arg and payload["ok"]:
            case_bundle_payload = _run_build_case_bundle(
                spec,
                case_dir=Path(case_dir_arg),
                upstream_url=getattr(args, "upstream_url", "") or "",
                upstream_ref=getattr(args, "upstream_ref", "") or "",
            )
        elif case_dir_arg and not payload["ok"]:
            print_warning("pipeline did not succeed — skipping case bundle assembly")

        if case_bundle_payload is not None:
            payload["case_bundle"] = case_bundle_payload

        _print_and_write_report(payload, args.report_path)
        if payload["ok"]:
            print_success("pipeline completed")
        else:
            print_warning("pipeline completed with errors")
        return 0 if payload["ok"] else 1

    raise ValueError(f"unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())