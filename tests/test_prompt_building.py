from __future__ import annotations

from pathlib import Path

from generator import TaskSpec, _build_generation_prompt


def _make_task(tmp_path: Path, **overrides) -> TaskSpec:
    defaults = dict(
        artifact_dir=tmp_path / "artifact",
        artifact_readme=tmp_path / "artifact" / "README.md",
        howto_path=tmp_path / "howto",
    )
    defaults.update(overrides)
    return TaskSpec(**defaults)


def _empty_notes() -> dict:
    return {"env_setup": [], "build_artifact": [], "benchmark_prep": [], "experiment_runs": []}


def _some_notes() -> dict:
    return {
        "env_setup": ["requires python 3.10", "needs gcc"],
        "build_artifact": ["run make install"],
        "benchmark_prep": [],
        "experiment_runs": ["run ./experiment.sh"],
    }


def test_prompt_contains_artifact_root(tmp_path: Path) -> None:
    task = _make_task(tmp_path)
    prompt = _build_generation_prompt(task, task.artifact_dir, task.artifact_readme,
                                      task.artifact_dir / "oracles", _empty_notes())
    assert task.artifact_dir.as_posix() in prompt


def test_prompt_contains_howto_path(tmp_path: Path) -> None:
    task = _make_task(tmp_path)
    prompt = _build_generation_prompt(task, task.artifact_dir, task.artifact_readme,
                                      task.artifact_dir / "oracles", _empty_notes())
    assert task.howto_path.as_posix() in prompt


def test_prompt_required_outputs_old_names(tmp_path: Path) -> None:
    task = _make_task(tmp_path)
    prompt = _build_generation_prompt(task, task.artifact_dir, task.artifact_readme,
                                      task.artifact_dir / "oracles", _empty_notes())
    assert "main.py" in prompt
    assert "oracle_env_setup.py" in prompt
    assert "oracle_build_install.py" in prompt
    assert "oracle_prep_benchmark.py" in prompt
    assert "oracle_run_experiments.py" in prompt


def test_prompt_no_notes_shorter_workflow(tmp_path: Path) -> None:
    task = _make_task(tmp_path)
    prompt = _build_generation_prompt(task, task.artifact_dir, task.artifact_readme,
                                      task.artifact_dir / "oracles", _empty_notes())
    assert "stage notes" not in prompt.lower()
    assert "Read the artifact README after that" in prompt


def test_prompt_with_notes_includes_workflow_step(tmp_path: Path) -> None:
    task = _make_task(tmp_path)
    prompt = _build_generation_prompt(task, task.artifact_dir, task.artifact_readme,
                                      task.artifact_dir / "oracles", _some_notes())
    assert "Stage notes extracted from artifact files" in prompt


def test_prompt_notes_content_appears(tmp_path: Path) -> None:
    task = _make_task(tmp_path)
    notes = _some_notes()
    prompt = _build_generation_prompt(task, task.artifact_dir, task.artifact_readme,
                                      task.artifact_dir / "oracles", notes)
    assert "requires python 3.10" in prompt
    assert "run make install" in prompt
    assert "run ./experiment.sh" in prompt


def test_prompt_with_notes_different_workflow(tmp_path: Path) -> None:
    task = _make_task(tmp_path)
    notes = _some_notes()
    prompt = _build_generation_prompt(task, task.artifact_dir, task.artifact_readme,
                                      task.artifact_dir / "oracles", notes)
    assert "Use the stage notes as the primary source" in prompt


def test_prompt_no_example_dirs_by_default(tmp_path: Path) -> None:
    task = _make_task(tmp_path)
    prompt = _build_generation_prompt(task, task.artifact_dir, task.artifact_readme,
                                      task.artifact_dir / "oracles", _empty_notes())
    assert "Example oracle directories" not in prompt


def test_prompt_example_dirs_included_when_set(tmp_path: Path) -> None:
    ex = tmp_path / "examples" / "case1"
    task = _make_task(tmp_path, example_oracle_dirs=(ex,))
    prompt = _build_generation_prompt(task, task.artifact_dir, task.artifact_readme,
                                      task.artifact_dir / "oracles", _empty_notes())
    assert "Example oracle directories" in prompt
    assert ex.as_posix() in prompt


def test_prompt_contains_oracle_plan_json(tmp_path: Path) -> None:
    task = _make_task(tmp_path)
    prompt = _build_generation_prompt(task, task.artifact_dir, task.artifact_readme,
                                      task.artifact_dir / "oracles", _empty_notes())
    assert "oracle_plan.json" in prompt


def test_prompt_contains_summary_request(tmp_path: Path) -> None:
    task = _make_task(tmp_path)
    prompt = _build_generation_prompt(task, task.artifact_dir, task.artifact_readme,
                                      task.artifact_dir / "oracles", _empty_notes())
    assert "Reply with a short summary" in prompt
