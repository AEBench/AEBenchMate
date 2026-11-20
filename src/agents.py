"""Load one agent adapter from agents/<name>/agent.py.

The directory-per-agent layout follows AEBench PR 155. Mate keeps a small
Python entry point so its existing SDK-based Claude integration can be reused.
"""

from __future__ import annotations

import importlib.util
import inspect
import re
import sys
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from types import ModuleType
from typing import Any, Literal

from utils import OracleGenConfig

AGENTS_DIR = Path(__file__).resolve().parents[1] / "agents"


@lru_cache(maxsize=None)
def load_agent(name: str) -> ModuleType:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name):
        raise ValueError(f"Invalid agent name: {name!r}")
    path = AGENTS_DIR / name / "agent.py"
    if not path.is_file():
        raise ValueError(f"Unknown agent {name!r}: expected {path}")
    definition = importlib.util.spec_from_file_location(
        f"_aebenchmate_agent_{name}",
        path,
        submodule_search_locations=[str(path.parent)],
    )
    assert definition is not None and definition.loader is not None
    module = importlib.util.module_from_spec(definition)
    # Register before execution so adapters can use dataclasses and relative imports.
    sys.modules[definition.name] = module
    try:
        definition.loader.exec_module(module)
        if not inspect.iscoroutinefunction(getattr(module, "run", None)):
            raise ValueError(
                f"{path} must define async run(spec, prompt, *, stage, on_line)"
            )
    except BaseException:
        sys.modules.pop(definition.name, None)
        raise
    return module


async def run_agent(
    spec: OracleGenConfig,
    prompt: str,
    *,
    stage: Literal["extract", "generate"],
    on_line: Callable[[str], None],
) -> tuple[str, float | None, dict[str, Any] | None]:
    return await load_agent(spec.agent).run(spec, prompt, stage=stage, on_line=on_line)
