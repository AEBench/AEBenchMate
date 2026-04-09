"""Oracle package and artifact plan validation."""
from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from validator import validate_oracle_package, validate_artifact_plan



def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content), encoding="utf-8")


def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2), encoding="utf-8")


_ORACLE_STUB = """\
    from evaluator.oracles.case_base import CaseOracle{base}Base
    class Oracle{cls}(CaseOracle{base}Base):
        def requirements(self): return []
"""

_PHASES = [
    ("env_setup.py",        "EnvSetup",       "EnvSetup"),
    ("artifact_build.py",   "ArtifactBuild",  "ArtifactBuild"),
    ("benchmark_prep.py",   "BenchmarkPrep",  "BenchmarkPrep"),
    ("experiment_runs.py",  "ExperimentRuns",  "ExperimentRuns"),
]

_ORACLE_PLAN_KEYS = [
    "howto_rules", "example_patterns", "borrowed_structure",
    "artifact_specific_adaptations", "reference_templates",
    "plan_summary", "step_coverage", "step_gaps", "step_augmentations",
]


def _setup_valid_package(root: Path) -> None:
    for filename, cls, base in _PHASES:
        _write(root / "oracles" / filename, _ORACLE_STUB.format(cls=cls, base=base))
    _write_json(root / "oracles" / "oracle_plan.json", {k: {} for k in _ORACLE_PLAN_KEYS})


def _setup_valid_plan(root: Path) -> None:
    _write_json(root / "artifact_plan.json", {
        "schema_version": 1,
        "artifact_name": "test",
        "source_file": "README.md",
        "summary": "test artifact",
        "stages": {
            "env_setup": [{"item_id": "env-001", "kind": "dependency", "name": "python",
                           "requirement": "", "command": "", "expected_outputs": [], "notes": ""}],
            "build_artifact": [],
            "benchmark_prep": [],
            "experiment_runs": [],
        },
    })



def test_validate_valid_package(tmp_path: Path) -> None:
    _setup_valid_package(tmp_path)
    report = validate_oracle_package(tmp_path)
    assert report.ok
    assert all(i.severity != "error" for i in report.issues)



def test_validate_missing_oracle_file(tmp_path: Path) -> None:
    _setup_valid_package(tmp_path)
    (tmp_path / "oracles" / "env_setup.py").unlink()
    report = validate_oracle_package(tmp_path)
    assert not report.ok
    codes = [i.code for i in report.issues]
    assert "missing_file" in codes


def test_validate_empty_oracles(tmp_path: Path) -> None:
    (tmp_path / "oracles").mkdir()
    report = validate_oracle_package(tmp_path)
    assert not report.ok
    assert sum(1 for i in report.issues if i.code == "missing_file") == 4



def test_validate_compile_error(tmp_path: Path) -> None:
    _setup_valid_package(tmp_path)
    (tmp_path / "oracles" / "env_setup.py").write_text("def broken(\n", encoding="utf-8")
    report = validate_oracle_package(tmp_path)
    assert not report.ok
    assert any(i.code == "compile_error" for i in report.issues)


def test_validate_missing_class(tmp_path: Path) -> None:
    _setup_valid_package(tmp_path)
    _write(tmp_path / "oracles" / "env_setup.py", "class WrongName: pass\n")
    report = validate_oracle_package(tmp_path)
    assert not report.ok
    assert any(i.code == "missing_class" for i in report.issues)


def test_validate_forbidden_call(tmp_path: Path) -> None:
    _setup_valid_package(tmp_path)
    p = tmp_path / "oracles" / "env_setup.py"
    p.write_text(p.read_text() + "\nx = input('hi')\n")
    report = validate_oracle_package(tmp_path)
    assert any(i.code == "forbidden_call" for i in report.issues)


def test_validate_forbidden_popen(tmp_path: Path) -> None:
    _setup_valid_package(tmp_path)
    p = tmp_path / "oracles" / "env_setup.py"
    p.write_text(p.read_text() + "\nimport subprocess\nsubprocess.Popen(['ls'])\n")
    report = validate_oracle_package(tmp_path)
    assert any(i.code == "forbidden_popen" for i in report.issues)


def test_validate_hardcoded_path_warning(tmp_path: Path) -> None:
    _setup_valid_package(tmp_path)
    p = tmp_path / "oracles" / "env_setup.py"
    p.write_text(p.read_text() + "\npath = '/home/someuser/data'\n")
    report = validate_oracle_package(tmp_path)
    assert any(i.code == "hardcoded_path" for i in report.issues)
    assert all(i.severity != "error" for i in report.issues)



def test_validate_missing_oracle_plan_is_warning(tmp_path: Path) -> None:
    _setup_valid_package(tmp_path)
    (tmp_path / "oracles" / "oracle_plan.json").unlink()
    report = validate_oracle_package(tmp_path)
    assert report.ok  # warning, not error
    assert any(i.code == "missing_plan" for i in report.issues)



def test_plan_valid(tmp_path: Path) -> None:
    _setup_valid_plan(tmp_path)
    report = validate_artifact_plan(tmp_path)
    assert report.ok


def test_plan_missing_file(tmp_path: Path) -> None:
    report = validate_artifact_plan(tmp_path / "nonexistent")
    assert not report.ok
    assert report.issues[0].code == "missing_artifact_plan"


def test_plan_not_json(tmp_path: Path) -> None:
    (tmp_path / "artifact_plan.json").write_text("not json{{{", encoding="utf-8")
    report = validate_artifact_plan(tmp_path)
    assert not report.ok


def test_plan_missing_stages(tmp_path: Path) -> None:
    _write_json(tmp_path / "artifact_plan.json", {
        "schema_version": 1, "artifact_name": "x", "source_file": "y", "summary": "z",
    })
    report = validate_artifact_plan(tmp_path)
    assert not report.ok
    assert any("stages" in i.message for i in report.issues)


def test_plan_missing_stage_name(tmp_path: Path) -> None:
    _write_json(tmp_path / "artifact_plan.json", {
        "schema_version": 1, "artifact_name": "x", "source_file": "y", "summary": "z",
        "stages": {"env_setup": [], "benchmark_prep": [], "experiment_runs": []},
    })
    report = validate_artifact_plan(tmp_path)
    assert not report.ok
    assert any("build_artifact" in i.message for i in report.issues)


def test_plan_duplicate_item_id(tmp_path: Path) -> None:
    item = {"item_id": "env-001", "kind": "dep", "name": "x",
            "requirement": "", "command": "", "expected_outputs": [], "notes": ""}
    _write_json(tmp_path / "artifact_plan.json", {
        "schema_version": 1, "artifact_name": "x", "source_file": "y", "summary": "z",
        "stages": {"env_setup": [item, item], "build_artifact": [],
                   "benchmark_prep": [], "experiment_runs": []},
    })
    report = validate_artifact_plan(tmp_path)
    assert any(i.code == "artifact_plan_duplicate_item_id" for i in report.issues)


def test_plan_wrong_id_prefix(tmp_path: Path) -> None:
    item = {"item_id": "build-001", "kind": "dep", "name": "x",
            "requirement": "", "command": "", "expected_outputs": [], "notes": ""}
    _write_json(tmp_path / "artifact_plan.json", {
        "schema_version": 1, "artifact_name": "x", "source_file": "y", "summary": "z",
        "stages": {"env_setup": [item], "build_artifact": [],
                   "benchmark_prep": [], "experiment_runs": []},
    })
    report = validate_artifact_plan(tmp_path)
    assert any(i.code == "artifact_plan_item_id_prefix" for i in report.issues)



