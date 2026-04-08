#!/usr/bin/env python3
from __future__ import annotations

import json
import logging
import os
import sys
import tomllib
from pathlib import Path
from typing import Any

_DEFAULT_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_DEFAULT_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


from dataclasses import asdict, dataclass

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock


def load_guidance_root(project_root: Path) -> Path | None:
    """Load guidance root path from aebenchmate.toml."""
    config_path = project_root / "aebenchmate.toml"
    if not config_path.is_file():
        return None

    with config_path.open("rb") as f:
        data = tomllib.load(f)
    raw = data.get("guidance", {}).get("root")
    if not raw:
        return None

    return (project_root / raw).resolve()

@dataclass
class EvalSpec:
    artifact_id: str
    artifact_dir: Path
    artifact_readme: Path | None = None
    howto_path: Path | None = None
    artifact_plan_path: Path | None = None
    example_oracle_dirs: tuple[Path, ...] = ()
    primitives_dir: Path | None = None
    examples_dir: Path | None = None
    output_dir_name: str = "oracles"
    output_base_dir: Path | None = None
    max_turns: int = 80
    max_repairs: int = 2
    max_budget_usd: float = 1.0
    model: str | None = None
    effort: str | None = "medium"
    feedback_path: Path | None = None
    verbose: bool = False
    cost_usd: float = 0.0

    def resolved(self) -> "EvalSpec":
        self.artifact_dir = self.artifact_dir.resolve()
        self.artifact_readme = self.artifact_readme.resolve() if self.artifact_readme else None
        self.howto_path = self.howto_path.resolve() if self.howto_path else None
        self.artifact_plan_path = self.artifact_plan_path.resolve() if self.artifact_plan_path else None
        self.feedback_path = self.feedback_path.resolve() if self.feedback_path else None
        self.primitives_dir = self.primitives_dir.resolve() if self.primitives_dir else None
        self.examples_dir = self.examples_dir.resolve() if self.examples_dir else None
        self.output_base_dir = self.output_base_dir.resolve() if self.output_base_dir else None

        if self.artifact_plan_path is None:
            default_plan_path = self.default_artifact_plan_path
            if default_plan_path.is_file():
                self.artifact_plan_path = default_plan_path.resolve()
            elif self.output_base_dir is not None:
                fallback = self.artifact_dir / "artifact_plan.json"
                if fallback.is_file():
                    self.artifact_plan_path = fallback.resolve()

        resolved_example_oracle_dirs: list[Path] = []
        for path in self.example_oracle_dirs:
            resolved_example_oracle_dirs.append(path.resolve())
        self.example_oracle_dirs = tuple(resolved_example_oracle_dirs)

        return self

    @property
    def output_dir(self) -> Path:
        base = self.output_base_dir if self.output_base_dir is not None else self.artifact_dir
        return base / self.output_dir_name

    @property
    def oracle_plan_path(self) -> Path:
        return self.output_dir / "oracle_plan.json"

    @property
    def default_artifact_plan_path(self) -> Path:
        base = self.output_base_dir if self.output_base_dir is not None else self.artifact_dir
        return base / "artifact_plan.json"

    @property
    def over_budget(self) -> bool:
        return self.cost_usd >= self.max_budget_usd

    def add_cost(self, reported_cost_usd: float | None) -> float:
        if reported_cost_usd is None:
            return self.cost_usd

        self.cost_usd += reported_cost_usd
        return self.cost_usd


def configure_logging(
    verbose: bool = False,
    debug: bool = False,
    log_file: Path | None = None,
) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.DEBUG)
    root.propagate = False

    formatter = logging.Formatter(
        fmt=_DEFAULT_FORMAT,
        datefmt=_DEFAULT_DATE_FORMAT,
    )

    console_handler = logging.StreamHandler(sys.stderr)
    if debug:
        console_handler.setLevel(logging.DEBUG)
    elif verbose:
        console_handler.setLevel(logging.INFO)
    else:
        console_handler.setLevel(logging.WARNING)
    console_handler.setFormatter(formatter)
    root.addHandler(console_handler)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    logging.captureWarnings(True)


def load_env_file(path: Path) -> None:
    if not path.is_file():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue

        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]

        os.environ.setdefault(key, value)


def resolve_log_file(base_dir: Path, value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def read_text_if_exists(path: Path) -> str | None:
    if not path.is_file():
        return None
    return path.read_text("utf-8")


def read_json_if_exists(path: Path) -> Any | None:
    text = read_text_if_exists(path)
    if text is None:
        return None
    return json.loads(text)


def write_json_file(
    path: Path,
    payload: Any,
    *,
    indent: int = 2,
    sort_keys: bool = False,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=indent, sort_keys=sort_keys) + "\n",
        "utf-8",
    )


async def read_response(
    client: "ClaudeSDKClient",
    *,
    logger=None,
    verbose: bool = False,
    line_fn=None,
) -> tuple[str, float | None, dict[str, Any] | None]:
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

    parts: list[str] = []
    total_cost_usd: float | None = None
    usage: dict[str, Any] | None = None

    async for message in client.receive_response():
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    parts.append(block.text)
                    if line_fn is not None and block.text:
                        for line in block.text.splitlines():
                            line_fn(line)
        elif isinstance(message, ResultMessage):
            total_cost_usd = message.total_cost_usd
            usage = message.usage

    return "\n".join(parts).strip(), total_cost_usd, usage


def read_feedback_text(feedback_path: Path | None) -> str | None:
    if feedback_path is None:
        return None

    text = read_text_if_exists(feedback_path)
    if text is None:
        return None

    text = text.strip()
    if not text:
        return None

    return text


def strip_code_fences(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines)
    return cleaned.strip()


def log_debug_text(
    logger: logging.Logger,
    title: str,
    text: str,
    *,
    max_chars: int = 2000,
) -> None:
    if not logger.isEnabledFor(logging.DEBUG):
        return

    preview = text.strip()
    if len(preview) > max_chars:
        preview = preview[: max_chars - 3] + "..."

    logger.debug("%s:", title)
    for line in preview.splitlines():
        logger.debug("  %s", line)


def log_debug_json(
    logger: logging.Logger,
    title: str,
    payload: Any,
    *,
    max_chars: int = 4000,
) -> None:
    if not logger.isEnabledFor(logging.DEBUG):
        return

    try:
        text = json.dumps(payload, indent=2, sort_keys=True, default=str)
    except Exception as exc:
        logger.debug("%s: <failed to serialize JSON: %s>", title, exc)
        return

    log_debug_text(logger, title, text, max_chars=max_chars)


def log_debug_spec(logger: logging.Logger, spec: EvalSpec) -> None:
    if not logger.isEnabledFor(logging.DEBUG):
        return

    log_debug_json(logger, "Resolved spec", asdict(spec))


def log_debug_stage_counts(
    logger: logging.Logger,
    stages: dict[str, Any],
) -> None:
    if not logger.isEnabledFor(logging.DEBUG):
        return

    counts = {name: len(items) if isinstance(items, list) else 0
              for name, items in stages.items()}
    log_debug_json(logger, "Stage counts", counts)
