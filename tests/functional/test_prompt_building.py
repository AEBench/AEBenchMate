"""Generator prompt construction."""
from __future__ import annotations

from pathlib import Path

import pytest

claude_agent_sdk = pytest.importorskip("claude_agent_sdk")

from generator import ArtifactPlan, _build_prompt
from utils import OracleGenConfig


def _make_config(tmp_path: Path) -> OracleGenConfig:
    artifact_dir = tmp_path / "artifact"
    artifact_dir.mkdir()
    readme = artifact_dir / "README.md"
    readme.write_text("# Test artifact")
    howto = tmp_path / "howto"
    howto.mkdir()
    return OracleGenConfig(
        artifact_id="test",
        artifact_dir=artifact_dir,
        artifact_readme=readme,
        howto_path=howto,
    )


_EMPTY_PLAN = ArtifactPlan.empty()

_NONEMPTY_PLAN = ArtifactPlan(
    artifact_plan_path=Path("/tmp/plan.json"),
    artifact_name="widget",
    readme_path="README.md",
    summary="reproduce widget results",
    env_setup=({"item_id": "env-001", "kind": "dependency", "name": "python"},),
    build_artifact=(),
    benchmark_prep=(),
    experiment_runs=(),
)


def test_prompt_contains_paths(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    prompt = _build_prompt(config, _EMPTY_PLAN, None)
    assert config.artifact_dir.as_posix() in prompt
    assert config.artifact_readme.as_posix() in prompt
    assert config.howto_path.as_posix() in prompt


def test_prompt_contains_required_oracle_files(tmp_path: Path) -> None:
    prompt = _build_prompt(_make_config(tmp_path), _EMPTY_PLAN, None)
    for name in ("env_setup.py", "artifact_build.py", "benchmark_prep.py",
                 "experiment_runs.py", "common.py", "oracle_plan.json"):
        assert f"oracles/{name}" in prompt


def test_prompt_contains_self_check(tmp_path: Path) -> None:
    prompt = _build_prompt(_make_config(tmp_path), _EMPTY_PLAN, None)
    assert "py_compile" in prompt


def test_no_plan_workflow(tmp_path: Path) -> None:
    prompt = _build_prompt(_make_config(tmp_path), _EMPTY_PLAN, None)
    assert "Read the artifact README after that" in prompt
    assert "artifact evaluation plan summary" not in prompt


def test_with_plan_workflow(tmp_path: Path) -> None:
    prompt = _build_prompt(_make_config(tmp_path), _NONEMPTY_PLAN, None)
    assert "Read the artifact evaluation plan" in prompt
    assert "primary structured checklist" in prompt
    assert "artifact evaluation plan summary" in prompt
    assert "widget" in prompt


def test_no_feedback(tmp_path: Path) -> None:
    prompt = _build_prompt(_make_config(tmp_path), _EMPTY_PLAN, None)
    assert "Previous validation feedback" not in prompt


def test_with_feedback(tmp_path: Path) -> None:
    prompt = _build_prompt(_make_config(tmp_path), _EMPTY_PLAN, "missing class OracleEnvSetup")
    assert "Previous validation feedback" in prompt
    assert "missing class OracleEnvSetup" in prompt
