from __future__ import annotations

import json
from pathlib import Path

import pytest

from validator import validate, validate_artifact_plan, create_missing_refs, ORACLES_DIR


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, "utf-8")


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), "utf-8")


def _scaffold_oracle(root: Path, extras: dict[str, str] | None = None) -> None:
    ae = root / ORACLES_DIR
    ae.mkdir(parents=True, exist_ok=True)
    for name, cls in [
        ("env_setup.py", "OracleEnvSetup"),
        ("artifact_build.py", "OracleArtifactBuild"),
        ("benchmark_prep.py", "OracleBenchmarkPrep"),
        ("experiment_runs.py", "OracleExperimentRuns"),
    ]:
        _write(ae / name, f"class {cls}: pass\n")
    _write(ae / "common.py", "\n")
    if extras:
        for fname, content in extras.items():
            _write(ae / fname, content)


def test_validate_subprocess_popen_forbidden(tmp_path: Path) -> None:
    _scaffold_oracle(tmp_path, extras={
        "env_setup.py": (
            "import subprocess\n"
            "class OracleEnvSetup:\n"
            "    def run(self):\n"
            "        subprocess.Popen(['ls'])\n"
        ),
    })
    report = validate(tmp_path)
    codes = [i.code for i in report.issues]
    assert "forbidden_popen" in codes


def test_validate_os_system_forbidden(tmp_path: Path) -> None:
    _scaffold_oracle(tmp_path, extras={
        "env_setup.py": (
            "import os\n"
            "class OracleEnvSetup:\n"
            "    def run(self):\n"
            "        os.system('ls')\n"
        ),
    })
    report = validate(tmp_path)
    codes = [i.code for i in report.issues]
    assert "forbidden_call" in codes


def test_validate_hardcoded_path_warning(tmp_path: Path) -> None:
    _scaffold_oracle(tmp_path, extras={
        "env_setup.py": (
            "class OracleEnvSetup:\n"
            "    PATH = '/home/user/data'\n"
        ),
    })
    report = validate(tmp_path)
    codes = [i.code for i in report.issues]
    assert "hardcoded_path" in codes
    assert all(i.severity == "warning" for i in report.issues if i.code == "hardcoded_path")


def test_validate_valid_oracle_all_phases_passes(tmp_path: Path) -> None:
    _scaffold_oracle(tmp_path)
    _write_json(tmp_path / ORACLES_DIR / "oracle_plan.json", {
        "howto_rules": [],
        "example_patterns": [],
        "borrowed_structure": [],
        "artifact_specific_adaptations": [],
        "reference_templates": {},
        "plan_summary": "",
        "step_coverage": {},
        "step_gaps": [],
        "step_augmentations": [],
    })
    report = validate(tmp_path)
    errors = [i for i in report.issues if i.severity == "error"]
    assert errors == []
    assert report.ok is True


def test_validate_artifact_plan_item_missing_name(tmp_path: Path) -> None:
    plan_path = tmp_path / "artifact_plan.json"
    _write_json(plan_path, {
        "schema_version": 1,
        "artifact_name": "x",
        "source_file": "y",
        "summary": "z",
        "stages": {
            "env_setup": [{"item_id": "env-1", "kind": "check", "requirement": "", "command": "", "expected_outputs": [], "notes": ""}],
            "build_artifact": [],
            "benchmark_prep": [],
            "experiment_runs": [],
        },
    })
    report = validate_artifact_plan(plan_path)
    msgs = [i.message for i in report.issues if i.code == "artifact_plan_item_field_missing"]
    assert any("'name'" in m for m in msgs)


def test_validate_artifact_plan_item_missing_kind(tmp_path: Path) -> None:
    plan_path = tmp_path / "artifact_plan.json"
    _write_json(plan_path, {
        "schema_version": 1,
        "artifact_name": "x",
        "source_file": "y",
        "summary": "z",
        "stages": {
            "env_setup": [{"item_id": "env-1", "name": "a", "requirement": "", "command": "", "expected_outputs": [], "notes": ""}],
            "build_artifact": [],
            "benchmark_prep": [],
            "experiment_runs": [],
        },
    })
    report = validate_artifact_plan(plan_path)
    msgs = [i.message for i in report.issues if i.code == "artifact_plan_item_field_missing"]
    assert any("'kind'" in m for m in msgs)


def test_validate_artifact_plan_missing_stage(tmp_path: Path) -> None:
    plan_path = tmp_path / "artifact_plan.json"
    _write_json(plan_path, {
        "schema_version": 1,
        "artifact_name": "x",
        "source_file": "y",
        "summary": "z",
        "stages": {
            "env_setup": [],
            "build_artifact": [],
        },
    })
    report = validate_artifact_plan(plan_path)
    codes = [i.code for i in report.issues]
    assert "artifact_plan_stage_missing" in codes


def test_validate_artifact_plan_wrong_id_prefix(tmp_path: Path) -> None:
    plan_path = tmp_path / "artifact_plan.json"
    _write_json(plan_path, {
        "schema_version": 1,
        "artifact_name": "x",
        "source_file": "y",
        "summary": "z",
        "stages": {
            "env_setup": [{
                "item_id": "build-1",
                "kind": "check",
                "name": "a",
                "requirement": "",
                "command": "",
                "expected_outputs": [],
                "notes": "",
            }],
            "build_artifact": [],
            "benchmark_prep": [],
            "experiment_runs": [],
        },
    })
    report = validate_artifact_plan(plan_path)
    codes = [i.code for i in report.issues]
    assert "artifact_plan_item_id_prefix" in codes


def test_validate_artifact_plan_zero_items_still_valid(tmp_path: Path) -> None:
    plan_path = tmp_path / "artifact_plan.json"
    _write_json(plan_path, {
        "schema_version": 1,
        "artifact_name": "x",
        "source_file": "y",
        "summary": "z",
        "stages": {
            "env_setup": [],
            "build_artifact": [],
            "benchmark_prep": [],
            "experiment_runs": [],
        },
    })
    report = validate_artifact_plan(plan_path)
    assert report.ok is True


def test_create_missing_refs_from_oracle_plan(tmp_path: Path) -> None:
    ae = tmp_path / ORACLES_DIR
    ae.mkdir(parents=True, exist_ok=True)

    _write_json(ae / "oracle_plan.json", {
        "reference_templates": {
            "data.json": {"key": "value"},
            "metrics.json": {"score": 0},
        },
    })
    _write(ae / "main.py", (
        "from entry import EntryConfig\n"
        "import os\n"
        "def main(argv):\n"
        "    cfg = EntryConfig(\n"
        "        ground_truth_paths={'data': os.path.join('refs', 'data.json'),\n"
        "                            'metrics': os.path.join('refs', 'metrics.json')}\n"
        "    )\n"
        "    return cfg\n"
    ))
    created = create_missing_refs(tmp_path)
    names = {p.name for p in created}
    assert "data.json" in names
    assert "metrics.json" in names
    assert (ae / "refs" / "data.json").exists()
    data = json.loads((ae / "refs" / "data.json").read_text())
    assert data == {"key": "value"}


def test_create_missing_refs_skips_existing(tmp_path: Path) -> None:
    ae = tmp_path / ORACLES_DIR
    refs_dir = ae / "refs"
    refs_dir.mkdir(parents=True, exist_ok=True)
    _write_json(refs_dir / "data.json", {"already": "here"})

    _write_json(ae / "oracle_plan.json", {
        "reference_templates": {
            "data.json": {"key": "value"},
            "other.json": {"new": True},
        },
    })
    _write(ae / "main.py", (
        "from entry import EntryConfig\n"
        "import os\n"
        "def main(argv):\n"
        "    cfg = EntryConfig(\n"
        "        ground_truth_paths={'d': os.path.join('refs', 'data.json'),\n"
        "                            'o': os.path.join('refs', 'other.json')}\n"
        "    )\n"
    ))
    created = create_missing_refs(tmp_path)
    names = {p.name for p in created}
    assert "data.json" not in names
    assert "other.json" in names
    existing = json.loads((refs_dir / "data.json").read_text())
    assert existing == {"already": "here"}
