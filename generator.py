#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
)

from utils import get_logger


logger = get_logger("generator")


_SYSTEM_APPEND = """
You are an expert Python engineer generating benchmark-specific oracle code.

Hard rules:
- Only write under _agent_eval/ and .claude/
- Do not modify artifact source files
- Generate _agent_eval/main.py and the four oracle modules
- Keep every oracle non-interactive, idempotent, and time-bounded
- Read HOWTO.md first, then the example oracle directories, then the artifact README
- Follow HOWTO.md conventions first
- Reuse patterns from the example oracle directories where appropriate
- Prefer evaluator base classes and primitives already present in the evaluator framework
- When exact refs values are unknown, create typed placeholders in refs JSON files
- Use 0.0 for floats, 0 for ints, empty strings for strings, false for booleans, [] for lists, and {} for objects
- Before finishing, run python -m py_compile on _agent_eval/*.py
""".strip()


_KEY_CONFIG_FILES = [
    "Makefile",
    "makefile",
    "requirements.txt",
    "requirements-dev.txt",
    "setup.py",
    "setup.cfg",
    "pyproject.toml",
    "environment.yml",
    "environment.yaml",
    "Dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
    "CMakeLists.txt",
    "go.mod",
    "pom.xml",
]

_ORACLE_FILES = [
    "main.py",
    "oracle_env_setup.py",
    "oracle_build_install.py",
    "oracle_prep_benchmark.py",
    "oracle_run_experiments.py",
]


@dataclass(frozen=True)
class TaskSpec:
    artifact_dir: Path
    artifact_readme: Path
    howto_path: Path
    example_oracle_dirs: tuple[Path, ...] = ()
    output_dir_name: str = "_agent_eval"
    max_turns: int = 80
    max_repairs: int = 2
    max_budget_usd: float = 1.0
    model: str | None = None
    effort: str | None = "medium"
    verbose: bool = False


@dataclass(frozen=True)
class GenerationResult:
    success: bool
    attempts: int
    transcript: str
    spent_usd: float | None = None


def run(task: TaskSpec) -> GenerationResult:
    return asyncio.run(generate(task))


async def generate(task: TaskSpec) -> GenerationResult:
    artifact_dir = task.artifact_dir.resolve()
    readme_path = task.artifact_readme.resolve()
    output_dir = artifact_dir / task.output_dir_name

    # --- Phase 1: read README and scan a handful of key config files ---

    readme_text = readme_path.read_text(encoding="utf-8")

    scanned: dict[str, str] = {}
    for name in _KEY_CONFIG_FILES:
        p = artifact_dir / name
        if p.is_file() and p.stat().st_size < 65536:
            try:
                scanned[name] = p.read_text(encoding="utf-8")
            except Exception:
                pass

    # also pick up shell scripts at the repo root
    for p in sorted(artifact_dir.iterdir()):
        if p.is_file() and p.suffix in {".sh", ".bash"} and p.stat().st_size < 65536:
            try:
                scanned[p.name] = p.read_text(encoding="utf-8")
            except Exception:
                pass

    logger.info(
        "scanning artifact: readme_chars=%d extra_files=%d",
        len(readme_text),
        len(scanned),
    )

    # --- Phase 2: extract stage notes via a quick model call ---

    notes = await _extract_stage_notes(task, artifact_dir, readme_text, scanned)

    logger.info(
        "stage notes: env=%d build=%d prep=%d run=%d",
        len(notes["env_setup"]),
        len(notes["build_artifact"]),
        len(notes["benchmark_prep"]),
        len(notes["experiment_runs"]),
    )

    # --- Phase 3: write policy files ---

    output_dir.mkdir(parents=True, exist_ok=True)

    claude_dir = artifact_dir / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)

    settings_payload = {
        "permissions": {
            "allow": [
                f"Write({output_dir.as_posix()}/**)",
                "Write(.claude/**)",
            ],
            "deny": ["Write(**)"],
        }
    }
    (claude_dir / "settings.json").write_text(
        json.dumps(settings_payload, indent=2), encoding="utf-8"
    )

    claude_md_lines = [
        "# Oracle Generator Context",
        "",
        f"Artifact root: {artifact_dir.as_posix()}",
        f"Artifact README: {readme_path.as_posix()}",
        f"HOWTO guide: {task.howto_path.as_posix()}",
        f"Output directory: {output_dir.as_posix()}",
        "",
        "Only write files under _agent_eval/ and .claude/.",
        "Do not modify artifact source files.",
    ]
    if task.example_oracle_dirs:
        claude_md_lines.append("")
        claude_md_lines.append("Example oracle directories:")
        for ep in task.example_oracle_dirs:
            claude_md_lines.append(f"- {ep.as_posix()}")
    (claude_dir / "CLAUDE.md").write_text(
        "\n".join(claude_md_lines) + "\n", encoding="utf-8"
    )

    # --- Phase 4: generate oracle files ---

    prompt = _build_generation_prompt(task, artifact_dir, readme_path, output_dir, notes)

    options = ClaudeAgentOptions(
        tools={"type": "preset", "preset": "claude_code"},
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": _SYSTEM_APPEND,
        },
        allowed_tools=["LS", "Read", "Grep", "Glob", "Write", "Edit", "MultiEdit", "Bash"],
        disallowed_tools=["WebFetch", "Agent(Explore)", "Agent(Plan)"],
        setting_sources=["project"],
        permission_mode="default",
        sandbox={
            "enabled": True,
            "autoAllowBashIfSandboxed": True,
            "allowUnsandboxedCommands": False,
        },
        cwd=artifact_dir.as_posix(),
        max_turns=task.max_turns,
        model=task.model,
        effort=task.effort,
    )

    transcript: list[str] = []
    spent_usd = 0.0
    attempts = 1

    async with ClaudeSDKClient(options=options) as client:
        await client.query(prompt)
        text, cost = await _consume_response(client, verbose=task.verbose)
        if text:
            transcript.append(text)
        if cost is not None:
            spent_usd = cost
            logger.info("session_cost_usd=%.4f", spent_usd)

        missing, compile_errors, ok = _validate_oracle_outputs(output_dir)

        repairs = 0
        while not ok and repairs < task.max_repairs:
            if task.max_budget_usd is not None and spent_usd >= task.max_budget_usd:
                logger.warning(
                    "budget limit reached spent_usd=%.4f limit_usd=%.4f",
                    spent_usd,
                    task.max_budget_usd,
                )
                transcript.append(
                    f"Stopped because budget limit was reached: "
                    f"spent=${spent_usd:.4f}, limit=${task.max_budget_usd:.4f}"
                )
                break
            repairs += 1
            attempts += 1
            logger.info("starting repair attempt %d", repairs)

            repair_lines = [
                "The generated evaluator has issues. Patch only the files under _agent_eval/.",
                "Do not modify artifact source files.",
                "Preserve correct work.",
                "",
                "Issues found:",
            ]
            for fname in missing:
                repair_lines.append(f"- missing required file: {fname}")
            for err in compile_errors:
                repair_lines.append(f"- compile error: {err}")
            repair_lines.extend([
                "",
                "Keep following HOWTO.md conventions and the example oracle patterns.",
                "If a referenced refs JSON file is missing and exact values are unknown,",
                "create it with the expected schema and typed placeholders.",
            ])

            await client.query("\n".join(repair_lines))
            text, cost = await _consume_response(client, verbose=task.verbose)
            if text:
                transcript.append(text)
            if cost is not None:
                spent_usd = cost
                logger.info("session_cost_usd=%.4f after repair %d", spent_usd, repairs)

            missing, compile_errors, ok = _validate_oracle_outputs(output_dir)

    logger.info("generation finished ok=%s attempts=%d spent_usd=%.4f", ok, attempts, spent_usd)

    return GenerationResult(
        success=ok,
        attempts=attempts,
        transcript="\n\n".join(t for t in transcript if t),
        spent_usd=spent_usd,
    )


async def _extract_stage_notes(
    task: TaskSpec,
    artifact_dir: Path,
    readme_text: str,
    scanned: dict[str, str],
) -> dict[str, list[str]]:
    notes: dict[str, list[str]] = {
        "env_setup": [],
        "build_artifact": [],
        "benchmark_prep": [],
        "experiment_runs": [],
    }

    context_parts = [f"README ({task.artifact_readme.name}):\n{readme_text}"]
    for file_name, file_text in scanned.items():
        context_parts.append(f"{file_name}:\n{file_text}")
    combined_context = "\n\n---\n\n".join(context_parts)

    extraction_prompt = (
        "Extract stage notes from the following artifact repository files.\n\n"
        "Return a JSON object with exactly these four keys: "
        "env_setup, build_artifact, benchmark_prep, experiment_runs.\n"
        "Each key maps to a list of short note strings.\n"
        "Notes should be terse, factual, and grounded only in the provided text.\n"
        "Do not invent facts not present in the files.\n"
        "If a stage has no relevant information, return an empty list for it.\n"
        "Return only the JSON object with no markdown fences.\n\n"
        f"{combined_context}\n"
    )

    extraction_options = ClaudeAgentOptions(
        tools={"type": "preset", "preset": "claude_code"},
        system_prompt={"type": "preset", "preset": "claude_code"},
        disallowed_tools=["Bash", "Edit", "Write", "MultiEdit", "WebFetch"],
        setting_sources=[],
        permission_mode="default",
        cwd=artifact_dir.as_posix(),
        max_turns=4,
        model=task.model,
        effort="low",
    )

    async with ClaudeSDKClient(options=extraction_options) as extraction_client:
        await extraction_client.query(extraction_prompt)
        extraction_text, _ = await _consume_response(extraction_client, verbose=False)

    try:
        cleaned = extraction_text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```[a-zA-Z0-9_-]*\n", "", cleaned)
            cleaned = re.sub(r"\n```$", "", cleaned)
            cleaned = cleaned.strip()
        raw_notes = json.loads(cleaned)
        if isinstance(raw_notes, dict):
            for stage in notes:
                stage_val = raw_notes.get(stage, [])
                if isinstance(stage_val, list):
                    notes[stage] = [str(n).strip() for n in stage_val if n]
    except Exception as exc:
        logger.warning("failed to parse stage notes response: %s", exc)

    return notes


def _build_generation_prompt(
    task: TaskSpec,
    artifact_dir: Path,
    readme_path: Path,
    output_dir: Path,
    notes: dict[str, list[str]],
) -> str:
    total_notes = sum(len(v) for v in notes.values())

    prompt_lines = [
        "Create a complete ArtEvalBench evaluator for this artifact.",
        "",
        f"Artifact root: {artifact_dir.as_posix()}",
        f"Artifact README: {readme_path.as_posix()}",
        f"HOWTO guide: {task.howto_path.as_posix()}",
        f"Output directory: {output_dir.as_posix()}",
        "",
        "You must follow this workflow exactly:",
        "1. Read HOWTO.md first.",
        "2. Read the example oracle directories next.",
    ]

    if total_notes > 0:
        prompt_lines.extend([
            "3. Read the stage notes below.",
            "4. Use the stage notes as the primary source of artifact requirements.",
            "5. Read the artifact README only after that, as fallback augmentation.",
            "6. Write _agent_eval/oracle_plan.json before or alongside code generation.",
            "7. Generate the artifact-specific evaluator files.",
            "8. Run python -m py_compile on _agent_eval/*.py as a self-check.",
        ])
    else:
        prompt_lines.extend([
            "3. Read the artifact README after that.",
            "4. Write _agent_eval/oracle_plan.json before or alongside code generation.",
            "5. Generate the artifact-specific evaluator files.",
            "6. Run python -m py_compile on _agent_eval/*.py as a self-check.",
        ])

    prompt_lines.extend([
        "",
        "Required outputs:",
        "- _agent_eval/main.py",
        "- _agent_eval/oracle_env_setup.py",
        "- _agent_eval/oracle_build_install.py",
        "- _agent_eval/oracle_prep_benchmark.py",
        "- _agent_eval/oracle_run_experiments.py",
        "- _agent_eval/oracle_plan.json",
        "- _agent_eval/refs/*.json only when the checks actually need them",
        "",
        "oracle_plan.json must include: howto_rules, example_patterns, "
        "borrowed_structure, artifact_specific_adaptations, assumptions, "
        "reference_templates.",
        "",
        "Implementation rules:",
        "- Follow HOWTO.md conventions first.",
        "- Reuse patterns from the examples where appropriate.",
        "- Only use the artifact README or repository to resolve ambiguity.",
        "- Keep checks objective, non-interactive, idempotent, and time-bounded.",
        "- Do not modify artifact source files.",
        "- Keep writes inside _agent_eval/ only.",
        "- Use refs JSON files only when they are referenced by the checks.",
        "",
    ])

    if task.example_oracle_dirs:
        prompt_lines.append("Example oracle directories:")
        for ep in task.example_oracle_dirs:
            prompt_lines.append(f"- {ep.as_posix()}")
        prompt_lines.append("")

    if total_notes > 0:
        prompt_lines.append("Stage notes extracted from artifact files:")
        for stage_name, stage_notes in notes.items():
            label = stage_name.replace("_", " ").title()
            prompt_lines.append(f"  {label} ({len(stage_notes)} note(s)):")
            for note in stage_notes:
                prompt_lines.append(f"    - {note}")
        prompt_lines.append("")

    prompt_lines.extend([
        "Reply with a short summary of:",
        "- what patterns you reused from HOWTO.md",
        "- what patterns you reused from the examples",
        "- what files you created or updated",
    ])

    return "\n".join(prompt_lines)


def _validate_oracle_outputs(
    output_dir: Path,
) -> tuple[list[str], list[str], bool]:
    missing = [f for f in _ORACLE_FILES if not (output_dir / f).is_file()]
    compile_errors: list[str] = []
    for fname in _ORACLE_FILES:
        p = output_dir / fname
        if p.is_file() and p.suffix == ".py":
            result = subprocess.run(
                [sys.executable, "-m", "py_compile", str(p)],
                capture_output=True,
            )
            if result.returncode != 0:
                compile_errors.append(f"{fname}: {result.stderr.decode().strip()}")
    ok = not missing and not compile_errors
    return missing, compile_errors, ok


async def _consume_response(
    client: ClaudeSDKClient,
    verbose: bool = False,
) -> tuple[str, float | None]:
    chunks: list[str] = []
    total_cost_usd: float | None = None
    async for message in client.receive_response():
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    chunks.append(block.text)
                    if verbose and block.text:
                        for line in block.text.splitlines():
                            logger.info("[assistant] %s", line)
        elif isinstance(message, ResultMessage):
            total_cost_usd = message.total_cost_usd
    return "\n".join(chunks).strip(), total_cost_usd
