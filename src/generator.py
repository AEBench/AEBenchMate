#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeSDKClient

from dataclasses import asdict
from utils import (
    OracleGenConfig,
    read_response,
    log_debug_json,
    log_debug_text,
    read_json_if_exists,
    read_feedback_text,
)

from policies import (
    generator_options,
    write_policy_files,
)
from dashboard import (
    print_agent_line,
    print_cost,
    print_info,
    print_stage_header,
    print_success,
    print_warning,
)

logger = logging.getLogger("generator")


@dataclass(frozen=True)
class ArtifactPlan:
    artifact_plan_path: Path | None
    artifact_name: str
    readme_path: str
    summary: str
    env_setup: tuple[dict[str, Any], ...]
    build_artifact: tuple[dict[str, Any], ...]
    benchmark_prep: tuple[dict[str, Any], ...]
    experiment_runs: tuple[dict[str, Any], ...]

    @classmethod
    def empty(cls) -> "ArtifactPlan":
        return cls(
            artifact_plan_path=None,
            artifact_name="",
            readme_path="",
            summary="",
            env_setup=(),
            build_artifact=(),
            benchmark_prep=(),
            experiment_runs=(),
        )


@dataclass(frozen=True)
class GenerationResult:
    success: bool
    attempts: int
    transcript: str
    cost_usd: float | None = None


async def generate_oracles(spec: OracleGenConfig) -> GenerationResult:
    spec = spec.resolve_paths()
    _check_required_inputs(spec)

    artifact_plan = _load_artifact_plan(spec.artifact_plan_path)
    feedback_text = read_feedback_text(spec.feedback_path)
    output_dir = spec.output_dir

    log_debug_json(logger, "Resolved spec", asdict(spec))
    log_debug_json(
        logger,
        "Loaded artifact plan summary",
        {
            "artifact_plan_path": str(artifact_plan.artifact_plan_path)
            if artifact_plan.artifact_plan_path is not None
            else None,
            "artifact_name": artifact_plan.artifact_name,
            "readme_path": artifact_plan.readme_path,
            "summary": artifact_plan.summary,
            "env_setup": len(artifact_plan.env_setup),
            "build_artifact": len(artifact_plan.build_artifact),
            "benchmark_prep": len(artifact_plan.benchmark_prep),
            "experiment_runs": len(artifact_plan.experiment_runs),
        },
    )

    logger.info("starting oracle generation")
    print_stage_header("generator", spec.artifact_id)
    print_info("starting oracle generation")

    write_policy_files(
        artifact_dir=spec.artifact_dir,
        output_dir=output_dir,
        artifact_readme=spec.artifact_readme,
        howto_path=spec.howto_path,
        artifact_plan_path=artifact_plan.artifact_plan_path,
        primitives_dir=spec.primitives_dir,
        examples_dir=spec.examples_dir,
        output_base_dir=spec.output_base_dir,
    )

    if spec.over_budget:
        logger.warning(
            "budget limit reached before generation cost_usd=%.4f limit_usd=%.4f",
            spec.cost_usd,
            spec.max_budget_usd,
        )
        print_warning(
            f"budget limit reached before generation: "
            f"${spec.cost_usd:.4f} / ${spec.max_budget_usd:.4f}"
        )

        return GenerationResult(
            success=False,
            attempts=0,
            transcript=(
                "Stopped before generation because budget limit was already reached: "
                f"spent=${spec.cost_usd:.4f}, limit=${spec.max_budget_usd:.4f}"
            ),
            cost_usd=spec.cost_usd,
        )

    options = generator_options(spec, output_dir, output_base_dir=spec.output_base_dir)
    transcript_parts: list[str] = []

    async with ClaudeSDKClient(options=options) as client:
        initial_prompt = _build_prompt(spec, artifact_plan, feedback_text)
        log_debug_text(logger, "Initial generator prompt", initial_prompt, max_chars=5000)
        logger.info(
            "generator prompt size: chars=%d bytes=%d",
            len(initial_prompt),
            len(initial_prompt.encode("utf-8")),
        )

        await client.query(initial_prompt)
        text, cost_usd, usage = await read_response(
            client,
            on_line=lambda line: print_agent_line(line, stage="generator"),
        )

        if text is not None:
            transcript_parts.append(text)
        if usage is not None:
            logger.info(
                "generator tokens: input=%s output=%s cache_create=%s cache_read=%s",
                usage.get("input_tokens"),
                usage.get("output_tokens"),
                usage.get("cache_creation_input_tokens"),
                usage.get("cache_read_input_tokens"),
            )

        spec.add_cost(cost_usd)
        print_cost(cost_usd, spec.cost_usd)

        log_debug_text(logger, "Generator response", text, max_chars=4000)
        if cost_usd is None:
            logger.info("session_cost_usd unavailable cost_usd=%.6f", spec.cost_usd)
        else:
            logger.info(
                "session_cost_usd=%.6f accumulated_spent_usd=%.6f",
                cost_usd,
                spec.cost_usd,
            )

    generation_ok = not spec.over_budget

    if generation_ok:
        print_success("oracle generation finished successfully")
    else:
        print_warning("oracle generation stopped after exceeding budget")

    return GenerationResult(
        success=generation_ok,
        attempts=1,
        transcript="\n\n".join(transcript_parts),
        cost_usd=spec.cost_usd,
    )


def _check_required_inputs(spec: OracleGenConfig) -> None:
    if spec.artifact_readme is None:
        raise ValueError("artifact_readme is required for generator")
    if spec.howto_path is None:
        raise ValueError("howto_path is required for generator")
    if not spec.howto_path.is_dir():
        raise ValueError("howto_path must point to the howto directory")


def _build_prompt(
    spec: OracleGenConfig,
    artifact_plan: ArtifactPlan,
    feedback_text: str | None,
) -> str:
    howto_dir = spec.howto_path
    instructions_path = howto_dir / "add_case.md"
    building_blocks_path = spec.primitives_dir or howto_dir / "oracle_building_blocks"
    examples_path = spec.examples_dir or howto_dir / "oracle_examples"

    lines = [
        "Create a complete ArtEvalBench evaluator for this artifact.",
        "",
        f"Artifact root: {spec.artifact_dir.as_posix()}",
        f"Artifact README: {spec.artifact_readme.as_posix()}",
        f"HOWTO guide directory: {howto_dir.as_posix()}",
        f"Instructions and guide file: {instructions_path.as_posix()}",
        f"Oracle building blocks: {building_blocks_path.as_posix()}",
        f"Oracle examples: {examples_path.as_posix()}",
    ]

    if artifact_plan.artifact_plan_path is not None:
        lines.append(
            f"artifact evaluation plan: {artifact_plan.artifact_plan_path.as_posix()}"
        )

    lines.extend(
        [
            "",
            "Hard constraint: do not inspect artifact source-code files unless a specific planned step cannot be resolved from artifact_plan.json, README, configuration, scripts/script-like, or infrastructure-related files.",
            "You must follow this workflow exactly:",
            "1. Read INSTRUCTIONS.md in the HOWTO directory first.",
            "2. Read oracle_building_blocks/ next and reuse those building blocks whenever possible.",
            "3. Read oracle_examples/ next and reuse their structure and patterns when appropriate.",
        ]
    )

    step = 4
    if _count_plan_steps(artifact_plan) > 0:
        lines.append(f"{step}. Read the artifact evaluation plan and the compact plan summary below.")
        step += 1
        lines.append(f"{step}. Use the artifact plan as the primary structured checklist.")
        step += 1
        lines.append(f"{step}. Read the artifact README only after that, as fallback augmentation.")
        step += 1
    else:
        lines.append(f"{step}. Read the artifact README after that.")
        step += 1

    lines.extend([
        f"{step}. Write oracles/oracle_plan.json before or alongside code generation.",
        f"{step + 1}. Generate the artifact-specific evaluator files.",
        f"{step + 2}. Run a lightweight self-check: python -m py_compile oracles/env_setup.py oracles/artifact_build.py oracles/benchmark_prep.py oracles/experiment_runs.py.",
    ])

    lines.extend(
        [
            "",
            "Required outputs (AEBench oracle layout):",
            "- oracles/env_setup.py        (class OracleEnvSetup(CaseOracleEnvSetupBase))",
            "- oracles/artifact_build.py   (class OracleArtifactBuild(CaseOracleArtifactBuildBase))",
            "- oracles/benchmark_prep.py   (class OracleBenchmarkPrep(CaseOracleBenchmarkPrepBase))",
            "- oracles/experiment_runs.py  (class OracleExperimentRuns(CaseOracleExperimentRunsBase))",
            "- oracles/common.py           (shared helpers -- not a phase class)",
            "- oracles/oracle_plan.json",
            "- oracles/refs/*.json only when the checks actually need them",
            "",
            "oracle_plan.json must include:",
            "- howto_rules, example_patterns, borrowed_structure, artifact_specific_adaptations",
            "- env_requirements, build_steps, benchmark_assets, experiment_outputs",
            "- reference_templates, plan_summary, step_coverage, step_gaps, step_augmentations",
            "",
            "Rules:",
            "- Follow add_case.md conventions first.",
            "- Reuse code patterns from oracle_building_blocks/ whenever possible.",
            "- Reuse structure and style from oracle_examples/ where appropriate.",
            "- Use the artifact evaluation plan and artifact README as the primary sources.",
            "- Do not proactively inspect artifact source code trees such as src/, lib/, internal/, pkg/, cmd/, app/, or similar directories.",
            "- Do not read artifact source code files to discover extra checks or requirements, unless there is significant ambiguity in the artifact README.",
            "- Only inspect repository files beyond the README when a planned step explicitly names a file, directory, manifest, command, or output path that must be confirmed.",
            "- If you add a check not present in the artifact evaluation plan, record it under step_augmentations in oracle_plan.json.",
            "- If a planned step cannot be operationalized into a check, record it under step_gaps in oracle_plan.json.",
            "- Prefer the existing evaluator base classes and primitives.",
            "- Keep checks objective, non-interactive, idempotent, and time-bounded.",
            "- Any use of subprocess.run, subprocess.check_call, or subprocess.check_output must include an explicit timeout=... argument.",
            "- Do not modify artifact source files.",
            "- Keep writes inside oracles/ only.",
            "- Use refs JSON files only when referenced by the checks.",
            "- If exact reference values are unknown, preserve the schema and use typed placeholders.",
            '- Use 0.0 for floats, 0 for ints, "" for strings, false for booleans, [] for lists, and {} for objects.',
            "",
        ]
    )

    if _count_plan_steps(artifact_plan) > 0:
        lines.append("artifact evaluation plan summary:")
        lines.append(_summarize_artifact_plan(artifact_plan))
        lines.append("")

    if feedback_text:
        lines.append("Previous validation feedback (fix these issues):")
        lines.append(feedback_text)
        lines.append("")

    lines.extend(
        [
            "Before finishing:",
            "- verify that all referenced refs JSON files exist",
            "- ensure imports, file names, and class names match AEBench oracle conventions (see Required outputs above)",
            "- ensure oracle_plan.json records step_coverage, step_gaps, and step_augmentations",
            "",
            "Reply with a short summary of:",
            "- what patterns you reused from HOWTO.md",
            "- what patterns you reused from the examples",
            "- how you used the artifact evaluation plan",
            "- what files you created or updated",
        ]
    )

    return "\n".join(lines)


def _load_artifact_plan(path: Path | None) -> ArtifactPlan:
    if path is None:
        return ArtifactPlan.empty()

    payload = read_json_if_exists(path)
    if payload is None:
        return ArtifactPlan.empty()

    if not isinstance(payload, dict):
        logger.warning("artifact plan %s is not a JSON object", path)
        return ArtifactPlan.empty()

    stages = payload.get("stages", {})
    if not isinstance(stages, dict):
        stages = {}

    env_setup = _load_stage_items(stages, "env_setup")
    build_artifact = _load_stage_items(stages, "build_artifact")
    benchmark_prep = _load_stage_items(stages, "benchmark_prep")
    experiment_runs = _load_stage_items(stages, "experiment_runs")

    artifact_name = str(payload.get("artifact_name", ""))
    readme_path = str(payload.get("source_file", ""))
    summary = str(payload.get("summary", ""))

    return ArtifactPlan(
        artifact_plan_path=path,
        artifact_name=artifact_name,
        readme_path=readme_path,
        summary=summary,
        env_setup=env_setup,
        build_artifact=build_artifact,
        benchmark_prep=benchmark_prep,
        experiment_runs=experiment_runs,
    )


def _load_stage_items(
    stages: dict[str, Any],
    stage_name: str,
) -> tuple[dict[str, Any], ...]:
    value = stages.get(stage_name, [])
    if not isinstance(value, list):
        return ()

    items: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict):
            items.append(item)

    return tuple(items)


def _count_plan_steps(plan: ArtifactPlan) -> int:
    return (
        len(plan.env_setup)
        + len(plan.build_artifact)
        + len(plan.benchmark_prep)
        + len(plan.experiment_runs)
    )


def _summarize_artifact_plan(plan: ArtifactPlan) -> str:
    lines: list[str] = []

    if plan.artifact_plan_path is not None:
        lines.append(f"Use artifact plan from: {plan.artifact_plan_path.as_posix()}")
    if plan.artifact_name:
        lines.append(f"Artifact name: {plan.artifact_name}")
    if plan.readme_path:
        lines.append(f"Plan source file: {plan.readme_path}")
    if plan.summary:
        lines.append(f"Plan summary: {plan.summary}")

    if lines:
        lines.append("")

    _append_stage_summary(lines, "Environment setup steps", plan.env_setup)
    _append_stage_summary(lines, "Build artifact steps", plan.build_artifact)
    _append_stage_summary(lines, "Benchmark prep steps", plan.benchmark_prep)
    _append_stage_summary(lines, "Experiment run steps", plan.experiment_runs)

    return "\n".join(lines).strip()


def _append_stage_summary(
    lines: list[str],
    title: str,
    steps: tuple[dict[str, Any], ...],
) -> None:
    lines.append(f"{title} ({len(steps)} steps):")

    shown = 0
    for step in steps:
        if shown >= 15:
            break
        lines.append(f"- {_format_step_summary_line(step)}")
        shown += 1

    remaining = len(steps) - shown
    if remaining > 0:
        lines.append(f"- ... {remaining} additional steps are available")

    lines.append("")


def _format_step_summary_line(step: dict[str, Any]) -> str:
    item_id = str(step.get("item_id", ""))
    category = str(step.get("kind", ""))
    name = str(step.get("name", ""))
    requirement = str(step.get("requirement", ""))
    command = str(step.get("command", ""))
    notes = str(step.get("notes", ""))

    parts: list[str] = [f"{category}: {name}"]

    if requirement:
        parts.append(f"requirement={requirement}")
    if command:
        parts.append(f"command={command}")

    expected_outputs = step.get("expected_outputs", [])
    if isinstance(expected_outputs, list) and expected_outputs:
        parts.append(f"outputs={expected_outputs}")

    if notes:
        parts.append(f"notes={notes}")

    detail = " | ".join(parts)
    if item_id:
        return f"[{item_id}] {detail}"
    return detail
