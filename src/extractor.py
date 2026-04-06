from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeSDKClient

from policies import planner_options
from utils import (
    EvalSpec,
    read_response,
    log_debug_json,
    log_debug_spec,
    log_debug_stage_counts,
    log_debug_text,
    strip_code_fences,
    read_feedback_text,
)
from dashboard import (
    print_agent_line,
    print_cost,
    print_info,
    print_stage_header,
    print_success,
    print_warning,
)

logger = logging.getLogger("extractor")


STAGES = (
    "env_setup",
    "build_artifact",
    "benchmark_prep",
    "experiment_runs",
)

_STAGE_PREFIX = {
    "env_setup": "env",
    "build_artifact": "build",
    "benchmark_prep": "prep",
    "experiment_runs": "run",
}

_ALLOWED_KINDS = (
    "dependency",
    "env_var",
    "service",
    "hardware",
    "command",
    "file",
    "directory",
    "dataset",
    "benchmark_asset",
    "result",
    "metric",
)


def _plan_prompt(
    readme_text: str,
    readme_path: str,
    feedback_text: str | None = None,
) -> str:
    prompt = (
        "Create one JSON object for the artifact plan derived from the README.\n\n"
        "The top-level JSON object must have exactly these fields:\n"
        "{\n"
        '  "schema_version": 1,\n'
        '  "artifact_name": "string",\n'
        '  "source_file": "string",\n'
        '  "summary": "string",\n'
        '  "stages": {\n'
        '    "env_setup": [ ... ],\n'
        '    "build_artifact": [ ... ],\n'
        '    "benchmark_prep": [ ... ],\n'
        '    "experiment_runs": [ ... ]\n'
        "  }\n"
        "}\n\n"
        "Each item inside a stage list must have exactly these fields:\n"
        "{\n"
        '  "kind": "string",\n'
        '  "name": "string",\n'
        '  "requirement": "string",\n'
        '  "command": "string",\n'
        '  "expected_outputs": ["string"],\n'
        '  "notes": "string"\n'
        "}\n\n"
        f"Allowed kind values: {', '.join(_ALLOWED_KINDS)}.\n"
        "Use empty strings for unknown text fields.\n"
        "Use [] for unknown expected_outputs.\n"
        "If a stage is not described, return an empty list for that stage.\n"
        "Do not wrap the JSON in markdown fences.\n\n"
        f"README path: {readme_path}\n\n"
        "README text:\n\n"
        f"{readme_text}\n"
    )

    if feedback_text:
        prompt += (
            "\n\nPrevious validation feedback for the last extraction attempt:\n"
            f"{feedback_text}\n\n"
            "Fix the issues above while still using only the provided README text.\n"
        )

    return prompt


async def _query_model(spec: EvalSpec, prompt: str) -> tuple[str, float | None]:
    options = planner_options(spec)
    async with ClaudeSDKClient(options=options) as client:
        await client.query(prompt)
        return await read_response(
                        client,
                        logger=logger,
                        verbose=spec.verbose,
                        line_fn=lambda line: print_agent_line(line, stage="extractor"),
                    )


def _parse_plan_json(text: str) -> dict[str, Any]:
    cleaned = strip_code_fences(text)
    payload = json.loads(cleaned)

    if not isinstance(payload, dict):
        raise ValueError("model response must be a JSON object")

    return payload


def _make_empty_plan(readme_path: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "artifact_name": "",
        "source_file": readme_path,
        "summary": "",
        "stages": {
            "env_setup": [],
            "build_artifact": [],
            "benchmark_prep": [],
            "experiment_runs": [],
        },
    }


def _str_field(d: dict[str, Any], key: str) -> str:
    val = d.get(key)
    return "" if val is None else str(val).strip()


def _clean_item(raw_item: Any) -> dict[str, Any] | None:
    if not isinstance(raw_item, dict):
        return None

    kind = _str_field(raw_item, "kind")
    name = _str_field(raw_item, "name")
    if not kind or not name:
        return None

    raw_outputs = raw_item.get("expected_outputs", [])
    expected_outputs: list[str] = []
    if isinstance(raw_outputs, list):
        for raw_output in raw_outputs:
            text = str(raw_output).strip() if raw_output is not None else ""
            if text:
                expected_outputs.append(text)

    return {
        "item_id": "",
        "kind": kind,
        "name": name,
        "requirement": _str_field(raw_item, "requirement"),
        "command": _str_field(raw_item, "command"),
        "expected_outputs": expected_outputs,
        "notes": _str_field(raw_item, "notes"),
    }


def _clean_stage(raw_items: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_items, list):
        return []

    items: list[dict[str, Any]] = []
    for raw_item in raw_items:
        normalized_item = _clean_item(raw_item)
        if normalized_item is not None:
            items.append(normalized_item)

    return items


def _assign_item_ids(plan: dict[str, Any]) -> None:
    for stage_name, prefix in _STAGE_PREFIX.items():
        items = plan["stages"][stage_name]
        for index, item in enumerate(items, start=1):
            item["item_id"] = f"{prefix}-{index:03d}"


def _clean_plan(payload: dict[str, Any], *, readme_path: str) -> dict[str, Any]:
    plan = _make_empty_plan(readme_path)

    artifact_name = _str_field(payload, "artifact_name")
    source_file = _str_field(payload, "source_file")
    summary = _str_field(payload, "summary")

    plan["artifact_name"] = artifact_name
    if source_file:
        plan["source_file"] = source_file
    plan["summary"] = summary

    raw_stages = payload.get("stages", {})
    if not isinstance(raw_stages, dict):
        raw_stages = {}

    for stage_name in STAGES:
        raw_items = raw_stages.get(stage_name, [])
        plan["stages"][stage_name] = _clean_stage(raw_items)

    _assign_item_ids(plan)
    return plan


async def extract_artifact_plan(
    spec: EvalSpec,
    readme_text: str,
    readme_path: str = "README.md",
) -> tuple[dict[str, Any], list[str]]:
    """Parse README into a normalized artifact plan via one model call."""
    spec = spec.resolved()
    log_debug_spec(logger, spec)
    warnings: list[str] = []

    logger.info(
        "starting planner extraction from README path=%s chars=%d",
        readme_path,
        len(readme_text),
    )

    print_stage_header("extractor", spec.artifact_id)
    print_info(f"reading README: {readme_path}")

    if spec.over_budget:
        logger.warning(
            "planner stopped before query because budget is exhausted cost_usd=%.6f limit_usd=%.6f",
            spec.cost_usd,
            spec.max_budget_usd,
        )
        warnings.append("budget exhausted before query")
        print_warning(
            f"extractor budget exhausted before query: "
            f"${spec.cost_usd:.6f} / ${spec.max_budget_usd:.6f}"
        )

        return _make_empty_plan(readme_path), warnings

    feedback_text = read_feedback_text(spec.feedback_path)
    prompt = _plan_prompt(
        readme_text,
        readme_path,
        feedback_text=feedback_text,
    )

    log_debug_text(logger, "Planner prompt", prompt, max_chars=5000)
    logger.info(
        "extractor prompt size: chars=%d bytes=%d",
        len(prompt),
        len(prompt.encode("utf-8")),
    )
    text, cost_usd, usage = await _query_model(spec, prompt)

    if usage is not None:
        logger.info(
            "extractor tokens: input=%s output=%s cache_create=%s cache_read=%s",
            usage.get("input_tokens"),
            usage.get("output_tokens"),
            usage.get("cache_creation_input_tokens"),
            usage.get("cache_read_input_tokens"),
        )

    spec.add_cost(cost_usd)
    print_cost(cost_usd, spec.cost_usd)

    log_debug_text(logger, "Raw planner response", text, max_chars=3000)

    if spec.over_budget:
        logger.warning(
            "planner exceeded budget after model response cost_usd=%.6f limit_usd=%.6f",
            spec.cost_usd,
            spec.max_budget_usd,
        )
        warnings.append("budget exceeded after model response")

    try:
        payload = _parse_plan_json(text)
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning("planner JSON parse failed for %s: %s", readme_path, exc)
        warnings.append(f"JSON parse failed: {exc}")
        return _make_empty_plan(readme_path), warnings

    log_debug_json(logger, "Parsed planner payload", payload, max_chars=4000)

    plan = _clean_plan(
        payload,
        readme_path=readme_path,
    )

    log_debug_stage_counts(logger, plan["stages"])
    log_debug_json(logger, "Normalized artifact plan", plan, max_chars=5000)

    print_success(
        "extractor finished "
        f"(env={len(plan['stages']['env_setup'])}, "
        f"build={len(plan['stages']['build_artifact'])}, "
        f"prep={len(plan['stages']['benchmark_prep'])}, "
        f"run={len(plan['stages']['experiment_runs'])})"
    )

    logger.info(
        "finished planner extraction: env=%d build=%d prep=%d run=%d cost_usd=%.6f",
        len(plan["stages"]["env_setup"]),
        len(plan["stages"]["build_artifact"]),
        len(plan["stages"]["benchmark_prep"]),
        len(plan["stages"]["experiment_runs"]),
        spec.cost_usd,
    )

    return plan, warnings