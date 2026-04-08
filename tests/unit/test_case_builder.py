"""Case toml generation and bundle assembly."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from case_builder import build_case_bundle, write_case_toml



def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _setup_oracles(root: Path) -> Path:
    d = root / "oracles"
    for name in ("env_setup.py", "artifact_build.py", "benchmark_prep.py", "experiment_runs.py"):
        _write(d / name, f"# {name}\n")
    _write(d / "common.py", "# shared\n")
    refs = d / "refs"
    refs.mkdir(parents=True, exist_ok=True)
    (refs / "data.ref.json").write_text('{"key": 1}\n', encoding="utf-8")
    return d



def test_toml_contains_case_id() -> None:
    toml = write_case_toml(case_id="osdi24_test")
    assert 'id = "osdi24_test"' in toml


def test_toml_contains_upstream_url() -> None:
    toml = write_case_toml(case_id="x", upstream_url="https://github.com/ex/repo.git")
    assert "https://github.com/ex/repo.git" in toml


def test_toml_custom_readme_path() -> None:
    toml = write_case_toml(case_id="x", readme_path_in_artifact="docs/README.md")
    assert 'path = "docs/README.md"' in toml


def test_toml_escapes_quotes() -> None:
    toml = write_case_toml(case_id="x", core_claim='claim with "quotes"')
    assert '\\"quotes\\"' in toml



def test_bundle_copies_oracle_files(tmp_path: Path) -> None:
    oracles_src = _setup_oracles(tmp_path)
    case_dir = tmp_path / "case_out"
    toml = write_case_toml(case_id="test")
    build_case_bundle(oracles_dir=oracles_src, case_dir=case_dir, case_toml_content=toml)

    assert (case_dir / "case.toml").is_file()
    assert (case_dir / "oracles" / "env_setup.py").is_file()
    assert (case_dir / "oracles" / "artifact_build.py").is_file()
    assert (case_dir / "oracles" / "common.py").is_file()


def test_bundle_copies_refs(tmp_path: Path) -> None:
    oracles_src = _setup_oracles(tmp_path)
    case_dir = tmp_path / "case_out"
    toml = write_case_toml(case_id="test")
    build_case_bundle(oracles_dir=oracles_src, case_dir=case_dir, case_toml_content=toml)

    ref = case_dir / "refs" / "data.ref.json"
    assert ref.is_file()
    assert json.loads(ref.read_text())["key"] == 1


def test_bundle_skips_missing_common(tmp_path: Path) -> None:
    oracles_src = _setup_oracles(tmp_path)
    (oracles_src / "common.py").unlink()
    case_dir = tmp_path / "case_out"
    toml = write_case_toml(case_id="test")
    written = build_case_bundle(oracles_dir=oracles_src, case_dir=case_dir, case_toml_content=toml)
    assert not any("common.py" in w for w in written)
