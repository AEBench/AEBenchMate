from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    ClaudeAgentOptions,
    HookMatcher,
    PermissionResultAllow,
    PermissionResultDeny,
)
from claude_agent_sdk.types import ToolPermissionContext

from utils import write_json_file


PLANNER_SYSTEM_APPEND = """
You are analyzing a README to produce a structured artifact plan.

Hard rules:
- Use only the provided README text.
- Do not execute commands.
- Do not modify files.
- Do not fetch from the network.
- Return only the requested structured JSON output.
- Do not invent requirements, commands, outputs, or steps.
""".strip()

GENERATOR_SYSTEM_APPEND = """
You are an expert Python engineer generating benchmark-specific oracle code for AEBench.

Hard rules:
- Only write under _agent_eval/ and .claude/
- Do not modify artifact source files
- Generate the four AEBench oracle modules with exact file and class names:
    _agent_eval/env_setup.py        → class OracleEnvSetup(CaseOracleEnvSetupBase)
    _agent_eval/artifact_build.py   → class OracleArtifactBuild(CaseOracleArtifactBuildBase)
    _agent_eval/benchmark_prep.py   → class OracleBenchmarkPrep(CaseOracleBenchmarkPrepBase)
    _agent_eval/experiment_runs.py  → class OracleExperimentRuns(CaseOracleExperimentRunsBase)
    _agent_eval/common.py           → shared helpers (no oracle base class)
- Keep every oracle non-interactive, idempotent, and time-bounded
- Read add_case.md first, then the oracle building blocks, then the artifact evaluation plan, then the artifact README
- Follow add_case.md conventions first
- Reuse patterns from the oracle building blocks and example cases where appropriate
- Use the artifact evaluation plan as the primary structured checklist
- Only use the artifact README or repository to resolve ambiguity, fill gaps, or verify uncertain steps
- Prefer CaseOracleXxxBase base classes and primitives from the evaluator framework
- When exact refs values are unknown, create typed placeholders in refs JSON files
- Use 0.0 for floats, 0 for ints, empty strings for strings, false for booleans, [] for lists, and {} for objects
- Create _agent_eval/oracle_plan.json and include howto_rules, example_patterns, borrowed_structure, artifact_specific_adaptations, reference_templates, plan_summary, step_coverage, step_gaps, and step_augmentations
- Before finishing, run: python -m py_compile _agent_eval/env_setup.py _agent_eval/artifact_build.py _agent_eval/benchmark_prep.py _agent_eval/experiment_runs.py
""".strip()

_ALLOWED_GENERATOR_TOOLS = (
    "LS",
    "Read",
    "Grep",
    "Glob",
    "Write",
    "Edit",
    "MultiEdit",
    "Bash",
)

_FORBIDDEN_GENERATOR_TOOLS = (
    "WebFetch",
    "Agent(Explore)",
    "Agent(Plan)",
)

_FORBIDDEN_PLANNER_TOOLS = (
    "Bash",
    "Edit",
    "Write",
    "MultiEdit",
    "WebFetch",
    "Agent(Explore)",
    "Agent(Plan)",
)

_FORBIDDEN_BASH = re.compile(
    r"(^|[;&|`(])\s*(git|sudo|ssh|scp|curl|wget|docker|podman|kubectl|rsync)\b"
    r"|\brm\s+-rf\b"
)

_SECRET_FILE_NAMES = (
    ".env",
    ".env.local",
    ".env.prod",
    ".env.production",
    ".env.dev",
)


def planner_options(spec: Any) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        tools={"type": "preset", "preset": "claude_code"},
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": PLANNER_SYSTEM_APPEND,
        },
        disallowed_tools=list(_FORBIDDEN_PLANNER_TOOLS),
        setting_sources=[],
        permission_mode="default",
        cwd=spec.artifact_dir.as_posix(),
        max_turns=spec.max_turns,
        model=spec.model,
        max_budget_usd=spec.max_budget_usd,
        effort=spec.effort,
    )


def generator_options(
    spec: Any,
    output_dir: Path,
    *,
    output_base_dir: Path | None = None,
) -> ClaudeAgentOptions:
    cwd = output_base_dir.as_posix() if output_base_dir is not None else spec.artifact_dir.as_posix()

    return ClaudeAgentOptions(
        tools={"type": "preset", "preset": "claude_code"},
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": GENERATOR_SYSTEM_APPEND,
        },
        allowed_tools=list(_ALLOWED_GENERATOR_TOOLS),
        disallowed_tools=list(_FORBIDDEN_GENERATOR_TOOLS),
        setting_sources=["project"],
        permission_mode="default",
        can_use_tool=permission_handler(
            spec.artifact_dir,
            output_dir,
            output_base_dir=output_base_dir,
        ),
        hooks=_HOOK_MATCHERS,
        sandbox={
            "enabled": True,
            "autoAllowBashIfSandboxed": True,
            "allowUnsandboxedCommands": False,
        },
        cwd=cwd,
        max_turns=spec.max_turns,
        model=spec.model,
        effort=spec.effort,
    )


def write_policy_files(
    artifact_dir: Path,
    output_dir: Path,
    artifact_readme: Path,
    howto_path: Path,
    artifact_plan_path: Path | None = None,
    primitives_dir: Path | None = None,
    examples_dir: Path | None = None,
    *,
    output_base_dir: Path | None = None,
) -> None:
    artifact_dir = artifact_dir.resolve()
    output_dir = output_dir.resolve()
    artifact_readme = artifact_readme.resolve()
    howto_path = howto_path.resolve()

    if artifact_plan_path is not None:
        artifact_plan_path = artifact_plan_path.resolve()
    if primitives_dir is not None:
        primitives_dir = primitives_dir.resolve()
    if examples_dir is not None:
        examples_dir = examples_dir.resolve()

    claude_base = output_base_dir.resolve() if output_base_dir is not None else artifact_dir
    claude_dir = claude_base / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    settings_path = claude_dir / "settings.json"
    claude_md_path = claude_dir / "CLAUDE.md"

    write_json_file(settings_path, _build_settings(output_dir), indent=2, sort_keys=True)

    claude_md_path.write_text(
        _build_claude_md(
            artifact_dir=artifact_dir,
            output_dir=output_dir,
            artifact_readme=artifact_readme,
            howto_path=howto_path,
            artifact_plan_path=artifact_plan_path,
            primitives_dir=primitives_dir,
            examples_dir=examples_dir,
        ),
        "utf-8",
    )



def permission_handler(
    artifact_dir: Path,
    output_dir: Path,
    *,
    output_base_dir: Path | None = None,
) -> Any:
    artifact_dir = artifact_dir.resolve()
    output_dir = output_dir.resolve()
    claude_base = output_base_dir.resolve() if output_base_dir is not None else artifact_dir
    claude_dir = (claude_base / ".claude").resolve()

    async def can_use_tool(
        tool_name: str,
        input_data: dict[str, Any],
        _context: ToolPermissionContext,
    ) -> PermissionResultAllow | PermissionResultDeny:

        if tool_name not in _ALLOWED_GENERATOR_TOOLS:
            return PermissionResultDeny(
                message=f"Tool {tool_name} is not allowed.",
                interrupt=True,
            )

        if tool_name == "Bash":
            return _check_bash_permission(input_data)

        if tool_name in {"Write", "Edit", "MultiEdit"}:
            return _check_write_permission(
                artifact_dir=artifact_dir,
                output_dir=output_dir,
                claude_dir=claude_dir,
                tool_name=tool_name,
                input_data=input_data,
            )

        if tool_name == "Read":
            return _check_read_permission(
                artifact_dir=artifact_dir,
                input_data=input_data,
            )

        return PermissionResultAllow(updated_input=input_data)

    return can_use_tool


async def keepalive_hook(
    _input_data: dict[str, Any],
    _tool_use_id: str | None,
    _context: Any,
) -> dict[str, Any]:
    return {"continue_": True}


_HOOK_MATCHERS: dict[str, list[HookMatcher]] = {"PreToolUse": [HookMatcher(hooks=[keepalive_hook])]}


def _build_settings(output_dir: Path) -> dict[str, Any]:
    rel_output = "/" + output_dir.name

    return {
        "permissions": {
            "defaultMode": "default",
            "allow": [
                "LS",
                "Read",
                "Grep",
                "Glob",
                "Bash",
                f"Write({rel_output}/**)",
                f"Edit({rel_output}/**)",
                "Write(/.claude/**)",
                "Edit(/.claude/**)",
            ],
            "deny": [
                "WebFetch",
                "Agent(Explore)",
                "Agent(Plan)",
                "Read(./.env)",
                "Read(./.env.*)",
                "Read(./**/.env)",
                "Read(./**/.env.*)",
                "Bash(git *)",
                "Bash(sudo *)",
                "Bash(ssh *)",
                "Bash(scp *)",
                "Bash(curl *)",
                "Bash(wget *)",
                "Bash(docker *)",
                "Bash(podman *)",
                "Bash(kubectl *)",
                "Bash(rsync *)",
                "Bash(rm -rf *)",
            ],
        },
        "sandbox": {
            "enabled": True,
            "autoAllowBashIfSandboxed": True,
            "allowUnsandboxedCommands": False,
        },
    }


def _build_claude_md(
    artifact_dir: Path,
    output_dir: Path,
    artifact_readme: Path,
    howto_path: Path,
    artifact_plan_path: Path | None,
    primitives_dir: Path | None = None,
    examples_dir: Path | None = None,
) -> str:
    instructions_path = howto_path / "INSTRUCTIONS.md"
    building_blocks_path = primitives_dir or howto_path / "oracle_building_blocks"
    examples_path = examples_dir or howto_path / "oracle_examples"

    lines = [
        "You are generating ArtEvalBench oracle code for this artifact.",
        "",
        f"HOWTO guide directory: {howto_path}",
        f"Instructions and guide file: {instructions_path.as_posix()}",
        f"Oracle building blocks: {building_blocks_path.as_posix()}",
        f"Oracle examples: {examples_path.as_posix()}",
        f"Artifact root: {artifact_dir.as_posix()}",
        f"Output directory: {output_dir.as_posix()}",
        f"Artifact README: {artifact_readme.as_posix()}",
    ]
    if artifact_plan_path is not None:
        lines.append(f"Artifact plan: {artifact_plan_path.as_posix()}")

    lines.extend([
        "",
        "Before writing code, read in this order:",
        f"1. Read {instructions_path} file in the {howto_path.as_posix()}/ directory.",
        f"2. Read {building_blocks_path}/ next and reuse those building blocks whenever possible.",
        f"3. Read {examples_path}/ next and reuse their structure and patterns when appropriate.",
    ])

    next_index = 4
    if artifact_plan_path is not None:
        lines.append(f"{next_index}. {artifact_plan_path.as_posix()}")
        next_index += 1

    lines.append(f"{next_index}. {artifact_readme.as_posix()}")

    lines.extend([
        "",
        "Hard constraint: do not inspect artifact source-code files unless a specific planned step cannot be resolved from artifact_plan.json, README, configuration, scripts/script-like, or infrastructure-related files.",
        "Rules:",
        "- Follow HOWTO.md conventions first.",
        "- Reuse patterns from the example oracles when applicable.",
        "- Use the artifact evaluation plan as the primary structured checklist when it is present.",
        "- Only use the artifact README or repository to resolve ambiguity, fill gaps, or verify uncertain steps.",
        "- Do not proactively inspect artifact source code trees; only inspect named non-source files or paths when needed to confirm a planned step.",
        "- Only deviate from the examples when the artifact requires it.",
        "- Write only under _agent_eval/ and .claude/.",
        "- Do not modify artifact source files.",
        "- Keep all oracles non-interactive, idempotent, and time-bounded.",
        "- Any use of subprocess.run, subprocess.check_call, or subprocess.check_output must include an explicit timeout=... argument.",
        "- Before finishing, verify that no generated oracle contains an unbounded subprocess call.",
        "- Prefer evaluator base classes and primitives already present in the framework.",
        "- Use refs JSON files only when they are needed by the checks.",
        "- If exact refs values are unknown, preserve the schema and use typed placeholders.",
        '- Use 0.0 for floats, 0 for ints, "" for strings, false for booleans, [] for lists, and {} for objects.',
        "",
        "Create _agent_eval/oracle_plan.json with:",
        "- howto_rules",
        "- example_patterns",
        "- borrowed_structure",
        "- artifact_specific_adaptations",
        "- reference_templates",
        "- plan_summary",
        "- step_coverage",
        "- step_gaps",
        "- step_augmentations",
    ])

    return "\n".join(lines) + "\n"


def _check_bash_permission(
    input_data: dict[str, Any],
) -> PermissionResultAllow | PermissionResultDeny:
    command = str(input_data.get("command", "")).strip()
    if not command:
        return PermissionResultDeny(message="Empty bash command is not allowed.", interrupt=True)

    if input_data.get("dangerouslyDisableSandbox"):
        return PermissionResultDeny(message="Unsandboxed bash is not allowed.", interrupt=True)

    if _FORBIDDEN_BASH.search(command):
        return PermissionResultDeny(message=f"Blocked bash command: {command}", interrupt=True)

    return PermissionResultAllow(updated_input=input_data)


def _check_write_permission(
    artifact_dir: Path,
    output_dir: Path,
    claude_dir: Path,
    tool_name: str,
    input_data: dict[str, Any],
) -> PermissionResultAllow | PermissionResultDeny:
    tool_path = _extract_tool_path(input_data)
    if tool_path is None:
        return PermissionResultDeny(message=f"{tool_name} requires file_path.", interrupt=True)

    resolved = _resolve_tool_path(artifact_dir, tool_path)
    if not _is_within_path(resolved, output_dir) and not _is_within_path(resolved, claude_dir):
        return PermissionResultDeny(
            message=f"Writes are limited to {output_dir} and {claude_dir}.",
            interrupt=True,
        )

    updated = dict(input_data)
    updated["file_path"] = resolved.as_posix()
    return PermissionResultAllow(updated_input=updated)


def _check_read_permission(
    artifact_dir: Path,
    input_data: dict[str, Any],
) -> PermissionResultAllow | PermissionResultDeny:
    tool_path = _extract_tool_path(input_data)
    if tool_path is None:
        return PermissionResultAllow(updated_input=input_data)

    resolved = _resolve_tool_path(artifact_dir, tool_path)
    if _is_secret_path(artifact_dir, resolved):
        return PermissionResultDeny(
            message=f"Blocked read of {resolved.as_posix()}.",
            interrupt=True,
        )

    updated = dict(input_data)
    updated["file_path"] = resolved.as_posix()
    return PermissionResultAllow(updated_input=updated)


def _extract_tool_path(input_data: dict[str, Any]) -> str | None:
    value = input_data.get("file_path")
    if value:
        return str(value)
    value = input_data.get("path")
    if value:
        return str(value)
    return None


def _resolve_tool_path(root: Path, value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def _is_within_path(path: Path, parent: Path) -> bool:
    return path.is_relative_to(parent)


def _is_secret_path(root: Path, path: Path) -> bool:
    if path.name in _SECRET_FILE_NAMES:
        return True
    if not path.is_relative_to(root):
        return False
    rel = path.relative_to(root)
    for part in rel.parts:
        if part.startswith(".env"):
            return True
    return False
