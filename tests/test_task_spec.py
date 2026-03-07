from __future__ import annotations

from pathlib import Path

from generator import GenerationResult, TaskSpec


def test_task_spec_required_fields(tmp_path: Path) -> None:
    task = TaskSpec(
        artifact_dir=tmp_path,
        artifact_readme=tmp_path / "README.md",
        howto_path=tmp_path / "howto",
    )
    assert task.artifact_dir == tmp_path
    assert task.artifact_readme == tmp_path / "README.md"
    assert task.howto_path == tmp_path / "howto"


def test_task_spec_defaults(tmp_path: Path) -> None:
    task = TaskSpec(
        artifact_dir=tmp_path,
        artifact_readme=tmp_path / "README.md",
        howto_path=tmp_path / "howto",
    )
    assert task.output_dir_name == "_agent_eval"
    assert task.max_turns == 80
    assert task.max_repairs == 2
    assert task.max_budget_usd == 1.0
    assert task.model is None
    assert task.effort == "medium"
    assert task.verbose is False
    assert task.example_oracle_dirs == ()


def test_task_spec_is_frozen(tmp_path: Path) -> None:
    import pytest
    task = TaskSpec(
        artifact_dir=tmp_path,
        artifact_readme=tmp_path / "README.md",
        howto_path=tmp_path / "howto",
    )
    with pytest.raises(Exception):
        task.verbose = True  # type: ignore[misc]


def test_task_spec_example_oracle_dirs(tmp_path: Path) -> None:
    ex = tmp_path / "examples" / "case1"
    task = TaskSpec(
        artifact_dir=tmp_path,
        artifact_readme=tmp_path / "README.md",
        howto_path=tmp_path / "howto",
        example_oracle_dirs=(ex,),
    )
    assert len(task.example_oracle_dirs) == 1
    assert task.example_oracle_dirs[0] == ex


def test_task_spec_custom_values(tmp_path: Path) -> None:
    task = TaskSpec(
        artifact_dir=tmp_path,
        artifact_readme=tmp_path / "README.md",
        howto_path=tmp_path / "howto",
        max_turns=40,
        max_repairs=5,
        max_budget_usd=3.0,
        model="claude-opus-4-6",
        effort="high",
        verbose=True,
        output_dir_name="my_eval",
    )
    assert task.max_turns == 40
    assert task.max_repairs == 5
    assert task.max_budget_usd == 3.0
    assert task.model == "claude-opus-4-6"
    assert task.effort == "high"
    assert task.verbose is True
    assert task.output_dir_name == "my_eval"


def test_generation_result_success(tmp_path: Path) -> None:
    result = GenerationResult(success=True, attempts=1, transcript="done", spent_usd=0.42)
    assert result.success is True
    assert result.attempts == 1
    assert result.transcript == "done"
    assert result.spent_usd == 0.42


def test_generation_result_failure() -> None:
    result = GenerationResult(success=False, attempts=3, transcript="failed")
    assert result.success is False
    assert result.attempts == 3
    assert result.spent_usd is None


def test_generation_result_is_frozen() -> None:
    import pytest
    result = GenerationResult(success=True, attempts=1, transcript="x")
    with pytest.raises(Exception):
        result.success = False  # type: ignore[misc]
