#!/usr/bin/env python3
"""Case bundle assembly."""
from __future__ import annotations

import datetime
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("case_builder")

_ORACLE_FILES = (
    "env_setup.py",
    "artifact_build.py",
    "benchmark_prep.py",
    "experiment_runs.py",
    "common.py",
)


def write_case_toml(
    *,
    case_id: str,
    core_claim: str = "",
    acceptable_evidence: str = "",
    allowed_tolerance: str = "Results may vary across environments but must preserve the reference trend.",
    readme_path_in_artifact: str = "README.md",
    runtime_mode: str = "docker",
    timeout_ms: int = 14_400_000,
    upstream_source_type: str = "git",
    upstream_url: str = "",
    upstream_ref: str = "",
    upstream_resolved_at: str = "",
) -> str:
    """Render case.toml content. Unknown fields get placeholder values."""
    if not upstream_resolved_at:
        upstream_resolved_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    lines = [
        f'id = "{case_id}"',
        "",
        "[case_brief]",
        f'core_claim = "{_escape_toml(core_claim or "TODO: describe the core claim")}"',
        f'acceptable_evidence = "{_escape_toml(acceptable_evidence or "TODO: describe what counts as success")}"',
        f'allowed_tolerance = "{_escape_toml(allowed_tolerance)}"',
        "",
        "[run]",
        f'id = "{case_id}"',
        "",
        "[run.instructions]",
        f'path = "{readme_path_in_artifact}"',
        "",
        "[run.runtime]",
        f'mode = "{runtime_mode}"',
        f"timeout_ms = {timeout_ms}",
        "gpu = false",
        "interactive = false",
        "",
        "[run.prompt]",
        'profile = "artifact-eval-v1"',
        "",
        "[oracle]",
        "expected_score = 4",
        'phases = ["env_setup", "artifact_build", "benchmark_prep", "experiment_runs"]',
        'score_mode = "phase_count"',
        'failure_mode = "fail_fast"',
        "placeholder = false",
        "",
        "[upstream]",
        f'source_type = "{upstream_source_type}"',
        f'url = "{upstream_url or "TODO: upstream git URL"}"',
        f'ref = "{upstream_ref or "TODO: full commit hash"}"',
        f'resolved_at = "{upstream_resolved_at}"',
        "",
    ]
    return "\n".join(lines)


def build_case_bundle(
    *,
    oracles_dir: Path,
    case_dir: Path,
    case_toml_content: str,
) -> list[str]:
    """Copy oracle outputs into case_dir/ and return list of written paths."""
    written: list[str] = []
    case_dir.mkdir(parents=True, exist_ok=True)

    toml_path = case_dir / "case.toml"
    toml_path.parent.mkdir(parents=True, exist_ok=True)
    toml_path.write_text(case_toml_content, "utf-8")
    written.append(str(toml_path))
    logger.info("wrote %s", toml_path)

    oracles_dest = case_dir / "oracles"
    oracles_dest.mkdir(parents=True, exist_ok=True)
    for name in _ORACLE_FILES:
        src = oracles_dir / name
        if src.is_file():
            dest = oracles_dest / name
            dest.write_bytes(src.read_bytes())
            written.append(str(dest))
            logger.info("copied %s -> oracles/%s", name, name)

    refs_src = oracles_dir / "refs"
    if refs_src.is_dir():
        refs_dest = case_dir / "refs"
        refs_dest.mkdir(parents=True, exist_ok=True)
        for ref_file in sorted(refs_src.iterdir()):
            if ref_file.is_file():
                dest = refs_dest / ref_file.name
                dest.write_bytes(ref_file.read_bytes())
                written.append(str(dest))
                logger.info("copied refs/%s", ref_file.name)

    return written


def _escape_toml(value: str) -> str:
    """Escape backslash, quote, newline for TOML strings."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
