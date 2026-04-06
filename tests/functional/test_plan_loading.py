from __future__ import annotations

import json
from pathlib import Path

import pytest

from generator import ArtifactPlan, _load_artifact_plan, _load_stage_items, _count_plan_steps, _summarize_artifact_plan


def _write_plan(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload), "utf-8")
    return path


def _make_bundle(**overrides) -> ArtifactPlan:
    defaults = dict(
        artifact_plan_path=None,
        artifact_name="",
        source_file="",
        summary="",
        env_setup=(),
        build_artifact=(),
        benchmark_prep=(),
        experiment_runs=(),
    )
    defaults.update(overrides)
    return ArtifactPlan(**defaults)


def test_load_artifact_plan_valid(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.json"
    _write_plan(plan_path, {
        "artifact_name": "foo",
        "source_file": "bar.py",
        "summary": "test summary",
        "stages": {
            "env_setup": [{"kind": "check", "name": "a"}],
            "build_artifact": [],
            "benchmark_prep": [],
            "experiment_runs": [],
        },
    })
    b = _load_artifact_plan(plan_path)
    assert b.artifact_plan_path == plan_path
    assert b.artifact_name == "foo"
    assert b.source_file == "bar.py"
    assert b.summary == "test summary"
    assert len(b.env_setup) == 1
    assert b.env_setup[0]["name"] == "a"


def test_load_artifact_plan_missing_file() -> None:
    b = _load_artifact_plan(Path("/nonexistent/plan.json"))
    assert b.artifact_plan_path is None
    assert b.env_setup == ()


def test_load_artifact_plan_none_path() -> None:
    b = _load_artifact_plan(None)
    assert b.artifact_plan_path is None


def test_load_artifact_plan_empty_stages(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.json"
    _write_plan(plan_path, {
        "artifact_name": "x",
        "stages": {
            "env_setup": [],
            "build_artifact": [],
            "benchmark_prep": [],
            "experiment_runs": [],
        },
    })
    b = _load_artifact_plan(plan_path)
    assert b.env_setup == ()
    assert b.build_artifact == ()


def test_load_artifact_plan_all_stages_populated(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.json"
    _write_plan(plan_path, {
        "stages": {
            "env_setup": [{"name": "a"}, {"name": "b"}],
            "build_artifact": [{"name": "c"}],
            "benchmark_prep": [{"name": "d"}, {"name": "e"}, {"name": "f"}],
            "experiment_runs": [{"name": "g"}],
        },
    })
    b = _load_artifact_plan(plan_path)
    assert len(b.env_setup) == 2
    assert len(b.build_artifact) == 1
    assert len(b.benchmark_prep) == 3
    assert len(b.experiment_runs) == 1


def test_load_artifact_plan_non_list_stage_value(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.json"
    _write_plan(plan_path, {
        "stages": {"env_setup": "not a list"},
    })
    b = _load_artifact_plan(plan_path)
    assert b.env_setup == ()


def test_load_stage_items_valid_list() -> None:
    stages = {"env_setup": [{"name": "a"}, {"name": "b"}]}
    result = _load_stage_items(stages, "env_setup")
    assert len(result) == 2
    assert result[0]["name"] == "a"


def test_load_stage_items_missing_key() -> None:
    assert _load_stage_items({}, "env_setup") == ()


def test_load_stage_items_non_list_value() -> None:
    assert _load_stage_items({"env_setup": 42}, "env_setup") == ()


def test_load_stage_items_filters_non_dict() -> None:
    stages = {"env_setup": [{"name": "a"}, "garbage", 99, {"name": "b"}]}
    result = _load_stage_items(stages, "env_setup")
    assert len(result) == 2
    assert result[1]["name"] == "b"


def test_count_plan_steps_populated() -> None:
    b = _make_bundle(
        env_setup=({"a": 1}, {"b": 2}),
        build_artifact=({"c": 3},),
        benchmark_prep=(),
        experiment_runs=({"d": 4}, {"e": 5}, {"f": 6}),
    )
    assert _count_plan_steps(b) == 6


def test_count_plan_steps_all_empty() -> None:
    assert _count_plan_steps(_make_bundle()) == 0


def test_summarize_artifact_plan_nonempty() -> None:
    b = _make_bundle(
        artifact_plan_path=Path("/tmp/plan.json"),
        artifact_name="myart",
        env_setup=({"kind": "check", "name": "a"},),
        build_artifact=({"kind": "run", "name": "b"},),
        benchmark_prep=(),
        experiment_runs=(),
    )
    text = _summarize_artifact_plan(b)
    assert "Environment setup steps (1 steps)" in text
    assert "Build artifact steps (1 steps)" in text
    assert "Benchmark prep steps (0 steps)" in text
    assert "myart" in text


def test_summarize_artifact_plan_empty() -> None:
    text = _summarize_artifact_plan(_make_bundle())
    assert "0 steps" in text


def test_summarize_artifact_plan_truncation() -> None:
    items = tuple({"kind": "check", "name": f"step_{i}"} for i in range(20))
    b = _make_bundle(env_setup=items)
    text = _summarize_artifact_plan(b)
    assert "additional steps" in text
