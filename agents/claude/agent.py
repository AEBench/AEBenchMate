"""Mate's original Claude SDK calls, exposed through the agent adapter interface."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from claude_agent_sdk import ClaudeSDKClient

from policies import extractor_options, generator_options, write_policy_files
from utils import OracleGenConfig, read_response

# Docker forwards only the host credentials declared by the selected adapter.
ENV_VARS = ("ANTHROPIC_API_KEY",)


async def run(
    spec: OracleGenConfig,
    prompt: str,
    *,
    stage: Literal["extract", "generate"],
    on_line: Callable[[str], None],
) -> tuple[str, float | None, dict[str, Any] | None]:
    if stage == "extract":
        options = extractor_options(spec)
    else:
        write_policy_files(
            artifact_dir=spec.artifact_dir,
            output_dir=spec.output_dir,
            artifact_readme=spec.artifact_readme,
            howto_path=spec.howto_path,
            artifact_plan_path=spec.artifact_plan_path,
            primitives_dir=spec.primitives_dir,
            examples_dir=spec.examples_dir,
            output_base_dir=spec.output_base_dir,
        )
        options = generator_options(
            spec, spec.output_dir, output_base_dir=spec.output_base_dir
        )
    async with ClaudeSDKClient(options=options) as client:
        await client.query(prompt)
        return await read_response(client, on_line=on_line)
