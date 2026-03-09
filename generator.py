from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeSDKClient

from policies import generator_options, write_policy_files
from utils import (
    EvalSpec,
    read_feedback_text,
    read_json_if_exists,
    read_response,
)

logger = logging.getLogger("generator")


@dataclass(frozen=True)
class ArtifactPlan:
    artifact_plan_path: Path | None
    artifact_name: str
    source_file: str
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
            source_file="",
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


def run(spec: EvalSpec) -> GenerationResult:
    return asyncio.run(generate(spec))


async def generate(spec: EvalSpec) -> GenerationResult:
    spec = spec.resolved()

    if spec.artifact_readme is None:
        raise ValueError("artifact_readme is required for generator")
    if spec.howto_path is None:
        raise ValueError("howto_path is required for generator")

    artifact_plan = _load_artifact_plan(spec.artifact_plan_path)
    feedback_text = read_feedback_text(spec.feedback_path)
    output_dir = spec.output_dir

    logger.info(
        "starting generation artifact_id=%s plan_steps=%d",
        spec.artifact_id,
        _count_plan_steps(artifact_plan),
    )

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
            "budget exhausted before generation cost_usd=%.4f limit=%.4f",
            spec.cost_usd,
            spec.max_budget_usd,
        )
        return GenerationResult(
            success=False,
            attempts=0,
            transcript=(
                f"Stopped before generation: budget exhausted "
                f"cost=${spec.cost_usd:.4f} limit=${spec.max_budget_usd:.4f}"
            ),
            cost_usd=spec.cost_usd,
        )

    options = generator_options(spec, output_dir, output_base_dir=spec.output_base_dir)
    transcript_parts: list[str] = []

    async with ClaudeSDKClient(options=options) as client:
        initial_prompt = _build_prompt(spec, artifact_plan, feedback_text)

        logger.info(
            "generator prompt size: chars=%d bytes=%d",
            len(initial_prompt),
            len(initial_prompt.encode("utf-8")),
        )

        await client.query(initial_prompt)
        text, cost_usd = await read_response(client, logger=logger, verbose=spec.verbose)

        if text:
            transcript_parts.append(text)

        spec.add_cost(cost_usd)
        logger.info("session_cost_usd=%.6f accumulated=%.6f", cost_usd or 0, spec.cost_usd)

    generation_ok = not spec.over_budget

    return GenerationResult(
        success=generation_ok,
        attempts=1,
        transcript="\n\n".join(transcript_parts),
        cost_usd=spec.cost_usd,
    )


def _load_artifact_plan(path: Path | None) -> ArtifactPlan:
    if path is None:
        return ArtifactPlan.empty()

    payload = read_json_if_exists(path)
    if payload is None or not isinstance(payload, dict):
        if payload is not None:
            logger.warning("artifact plan %s is not a JSON object", path)
        return ArtifactPlan.empty()

    stages = payload.get("stages", {})
    if not isinstance(stages, dict):
        stages = {}

    return ArtifactPlan(
        artifact_plan_path=path,
        artifact_name=str(payload.get("artifact_name", "")),
        source_file=str(payload.get("source_file", "")),
        summary=str(payload.get("summary", "")),
        env_setup=_load_stage_items(stages, "env_setup"),
        build_artifact=_load_stage_items(stages, "build_artifact"),
        benchmark_prep=_load_stage_items(stages, "benchmark_prep"),
        experiment_runs=_load_stage_items(stages, "experiment_runs"),
    )


def _load_stage_items(
    stages: dict[str, Any],
    stage_name: str,
) -> tuple[dict[str, Any], ...]:
    value = stages.get(stage_name, [])
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, dict))


def _count_plan_steps(bundle: ArtifactPlan) -> int:
    return (
        len(bundle.env_setup)
        + len(bundle.build_artifact)
        + len(bundle.benchmark_prep)
        + len(bundle.experiment_runs)
    )


def _build_prompt(
    spec: EvalSpec,
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
        lines.append(f"Artifact evaluation plan: {artifact_plan.artifact_plan_path.as_posix()}")

    lines.extend([
        "",
        "Hard constraint: do not inspect artifact source-code files unless a specific planned step cannot be resolved from artifact_plan.json, README, configuration, scripts/script-like, or infrastructure-related files.",
        "You must follow this workflow exactly:",
        "1. Read INSTRUCTIONS.md in the HOWTO directory first.",
        "2. Read oracle_building_blocks/ next and reuse those building blocks whenever possible.",
        "3. Read oracle_examples/ next and reuse their structure and patterns when appropriate.",
    ])

    if _count_plan_steps(artifact_plan) > 0:
        lines.extend([
            "4. Read the artifact evaluation plan and the compact plan summary below.",
            "5. Use the artifact plan as the primary structured checklist.",
            "6. Read the artifact README only after that, as fallback augmentation.",
            "7. Do not proactively inspect artifact source code trees; only inspect named non-source files or paths when needed to confirm a planned step.",
            "8. Write _agent_eval/oracle_plan.json before or alongside code generation.",
            "9. Generate the artifact-specific evaluator files.",
            "10. Run a lightweight self-check: python -m py_compile _agent_eval/env_setup.py _agent_eval/artifact_build.py _agent_eval/benchmark_prep.py _agent_eval/experiment_runs.py.",
        ])
    else:
        lines.extend([
            "4. Read the artifact README after that.",
            "5. Do not proactively inspect artifact source code trees; only inspect named non-source files or paths when needed to confirm a planned step.",
            "6. Write _agent_eval/oracle_plan.json before or alongside code generation.",
            "7. Generate the artifact-specific evaluator files.",
            "8. Run a lightweight self-check: python -m py_compile _agent_eval/env_setup.py _agent_eval/artifact_build.py _agent_eval/benchmark_prep.py _agent_eval/experiment_runs.py.",
        ])

    lines.extend([
        "",
        "Required outputs (AEBench oracle layout):",
        "- _agent_eval/env_setup.py        (class OracleEnvSetup(CaseOracleEnvSetupBase))",
        "- _agent_eval/artifact_build.py   (class OracleArtifactBuild(CaseOracleArtifactBuildBase))",
        "- _agent_eval/benchmark_prep.py   (class OracleBenchmarkPrep(CaseOracleBenchmarkPrepBase))",
        "- _agent_eval/experiment_runs.py  (class OracleExperimentRuns(CaseOracleExperimentRunsBase))",
        "- _agent_eval/common.py           (shared helpers — not a phase class)",
        "- _agent_eval/oracle_plan.json",
        "- _agent_eval/refs/*.json only when the checks actually need them",
        "",
        "oracle_plan.json must include:",
        "- howto_rules",
        "- example_patterns",
        "- borrowed_structure",
        "- artifact_specific_adaptations",
        "- reference_templates",
        "- plan_summary",
        "- step_coverage",
        "- step_gaps",
        "- step_augmentations",
        "",
        "Implementation rules:",
        "- Follow INSTRUCTIONS.md conventions first.",
        "- Reuse code patterns from oracle_building_blocks/ whenever possible.",
        "- Reuse structure and style from oracle_examples/ where appropriate.",
        "- Use the artifact evaluation plan as the primary source of artifact requirements when it is available.",
        "- For every important oracle check, prefer to map it to one or more planned steps from the artifact plan.",
        "- Use the artifact README as fallback only.",
        "- Do not proactively inspect artifact source code trees.",
        "- Keep checks objective, non-interactive, idempotent, and time-bounded.",
        "- Any use of subprocess.run, subprocess.check_call, or subprocess.check_output must include an explicit timeout=... argument.",
        "- Do not modify artifact source files.",
        "- Keep writes inside _agent_eval/ only.",
        "",
    ])

    if _count_plan_steps(artifact_plan) > 0:
        lines.append("Artifact evaluation plan summary:")
        lines.append(_summarize_artifact_plan(artifact_plan))
        lines.append("")

    if feedback_text:
        lines.append("Previous validation feedback (fix these issues):")
        lines.append(feedback_text)
        lines.append("")

    lines.extend([
        "Before finishing:",
        "- verify that all referenced refs JSON files exist",
        "- ensure imports, file names, and class names match AEBench oracle conventions",
        "- ensure oracle_plan.json records step_coverage, step_gaps, and step_augmentations",
        "",
        "Reply with a short summary of:",
        "- what patterns you reused from HOWTO",
        "- what patterns you reused from the examples",
        "- how you used the artifact evaluation plan",
        "- what files you created or updated",
    ])

    return "\n".join(lines)


def _summarize_artifact_plan(bundle: ArtifactPlan) -> str:
    lines: list[str] = []

    if bundle.artifact_plan_path is not None:
        lines.append(f"Use artifact plan from: {bundle.artifact_plan_path.as_posix()}")
    if bundle.artifact_name:
        lines.append(f"Artifact name: {bundle.artifact_name}")
    if bundle.summary:
        lines.append(f"Plan summary: {bundle.summary}")
    if lines:
        lines.append("")

    for title, steps in [
        ("Environment setup steps", bundle.env_setup),
        ("Build artifact steps", bundle.build_artifact),
        ("Benchmark prep steps", bundle.benchmark_prep),
        ("Experiment run steps", bundle.experiment_runs),
    ]:
        lines.append(f"{title} ({len(steps)} steps):")
        shown = 0
        for step in steps:
            if shown >= 15:
                break
            item_id = str(step.get("item_id", ""))
            kind = str(step.get("kind", ""))
            name = str(step.get("name", ""))
            requirement = str(step.get("requirement", ""))
            command = str(step.get("command", ""))
            parts = [f"{kind}: {name}"]
            if requirement:
                parts.append(f"requirement={requirement}")
            if command:
                parts.append(f"command={command}")
            detail = " | ".join(parts)
            line = f"  [{item_id}] {detail}" if item_id else f"  {detail}"
            lines.append(line)
            shown += 1
        remaining = len(steps) - shown
        if remaining > 0:
            lines.append(f"  ... {remaining} additional steps available")
        lines.append("")

    return "\n".join(lines).strip()
