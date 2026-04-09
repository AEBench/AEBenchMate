from __future__ import annotations

import json
from pathlib import Path

import pytest

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib # sometimes gets redefinition error

from case_builder import write_case_toml, build_case_bundle


def test_write_case_toml_is_valid_toml() -> None:
    text = write_case_toml(case_id="test-001")
    parsed = tomllib.loads(text)
    assert parsed["id"] == "test-001"


def test_write_case_toml_required_sections() -> None:
    text = write_case_toml(case_id="test-002")
    parsed = tomllib.loads(text)
    assert "case_brief" in parsed
    assert "run" in parsed
    assert "oracle" in parsed
    assert "upstream" in parsed


def test_write_case_toml_special_chars_in_core_claim() -> None:
    claim = 'Results show "50% improvement" & backslash \\ test'
    text = write_case_toml(case_id="test-003", core_claim=claim)
    parsed = tomllib.loads(text)
    assert parsed["case_brief"]["core_claim"] == claim


def test_write_case_toml_newlines_in_text_field() -> None:
    claim = "line one\nline two\nline three"
    text = write_case_toml(case_id="test-004", core_claim=claim)
    parsed = tomllib.loads(text)
    assert parsed["case_brief"]["core_claim"] == claim


def test_build_case_bundle_all_files(tmp_path: Path) -> None:
    ae = tmp_path / "src" / "oracles"
    ae.mkdir(parents=True)
    for name in ("env_setup.py", "artifact_build.py", "benchmark_prep.py", "experiment_runs.py", "common.py"):
        (ae / name).write_text(f"# {name}\n")
    refs = ae / "refs"
    refs.mkdir()
    (refs / "data.json").write_text('{"k": 1}')

    case_dir = tmp_path / "out"
    toml_text = write_case_toml(case_id="bundle-test")
    written = build_case_bundle(oracles_dir=ae, case_dir=case_dir, case_toml_content=toml_text)

    assert (case_dir / "case.toml").exists()
    assert (case_dir / "oracles" / "env_setup.py").exists()
    assert (case_dir / "oracles" / "common.py").exists()
    assert (case_dir / "refs" / "data.json").exists()
    assert len(written) == 7  # toml + 5 py + 1 ref


def test_build_case_bundle_nested_refs_not_recursed(tmp_path: Path) -> None:
    ae = tmp_path / "src" / "oracles"
    ae.mkdir(parents=True)
    refs = ae / "refs"
    refs.mkdir()
    (refs / "top.json").write_text("{}")
    nested = refs / "subdir"
    nested.mkdir()
    (nested / "deep.json").write_text("{}")

    case_dir = tmp_path / "out"
    written = build_case_bundle(oracles_dir=ae, case_dir=case_dir, case_toml_content="id = 'x'\n")

    written_names = [Path(p).name for p in written]
    assert "top.json" in written_names
    assert "deep.json" not in written_names


def test_build_case_bundle_missing_oracle_files(tmp_path: Path) -> None:
    ae = tmp_path / "src" / "oracles"
    ae.mkdir(parents=True)
    (ae / "env_setup.py").write_text("# only one\n")

    case_dir = tmp_path / "out"
    written = build_case_bundle(oracles_dir=ae, case_dir=case_dir, case_toml_content="id = 'x'\n")

    assert (case_dir / "oracles" / "env_setup.py").exists()
    assert not (case_dir / "oracles" / "artifact_build.py").exists()
    assert len(written) == 2  # toml + 1 py
