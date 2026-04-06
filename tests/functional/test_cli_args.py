"""CLI argument parsing and spec construction."""
from __future__ import annotations

from pathlib import Path

import pytest

from cli import parse_args, _make_spec



def test_parse_extract_args() -> None:
    ns = parse_args(["extract", "--artifact-dir", "/tmp/art", "--artifact-readme", "/tmp/art/README.md"])
    assert ns.command == "extract"
    assert ns.artifact_dir == "/tmp/art"
    assert ns.artifact_readme == "/tmp/art/README.md"


def test_parse_validate_args() -> None:
    ns = parse_args(["validate", "--artifact-dir", "/tmp/art", "--target", "plan"])
    assert ns.command == "validate"
    assert ns.target == "plan"


def test_parse_pipeline_args() -> None:
    ns = parse_args([
        "pipeline",
        "--artifact-dir", "/tmp/art",
        "--artifact-readme", "/tmp/art/README.md",
        "--case-dir", "/tmp/case",
        "--upstream-url", "https://github.com/ex/repo.git",
        "--upstream-ref", "abc123",
    ])
    assert ns.command == "pipeline"
    assert ns.case_dir == "/tmp/case"
    assert ns.upstream_url == "https://github.com/ex/repo.git"


def test_parse_effort_choices() -> None:
    ns = parse_args(["extract", "--artifact-dir", "/tmp", "--artifact-readme", "/tmp/R.md", "--effort", "high"])
    assert ns.effort == "high"


def test_parse_effort_invalid() -> None:
    with pytest.raises(SystemExit):
        parse_args(["extract", "--artifact-dir", "/tmp", "--artifact-readme", "/tmp/R.md", "--effort", "ultra"])



def test_build_spec_basic(tmp_path: Path) -> None:
    art = tmp_path / "artifact"
    art.mkdir()
    readme = art / "README.md"
    readme.write_text("# test\n")

    ns = parse_args([
        "extract",
        "--artifact-dir", str(art),
        "--artifact-readme", str(readme),
        "--max-turns", "40",
        "--max-budget-usd", "2.5",
    ])
    spec = _make_spec(ns, guidance_root=None)
    assert spec.artifact_dir == art.resolve()
    assert spec.max_turns == 40
    assert spec.max_budget_usd == 2.5


def test_build_spec_with_config(tmp_path: Path) -> None:
    art = tmp_path / "artifact"
    art.mkdir()
    readme = art / "README.md"
    readme.write_text("# test\n")

    guidance = tmp_path / "guidance"
    howtos = guidance / "howtos"
    howtos.mkdir(parents=True)

    ns = parse_args(["generate", "--artifact-dir", str(art), "--artifact-readme", str(readme)])
    spec = _make_spec(ns, guidance_root=guidance)
    assert spec.howto_path == howtos.resolve()


def test_build_spec_guidance_dir_overrides(tmp_path: Path) -> None:
    art = tmp_path / "artifact"
    art.mkdir()
    readme = art / "README.md"
    readme.write_text("# test\n")

    guidance = tmp_path / "guidance"
    (guidance / "howtos").mkdir(parents=True)
    (guidance / "primitives").mkdir(parents=True)
    (guidance / "examples").mkdir(parents=True)

    ns = parse_args([
        "generate",
        "--artifact-dir", str(art),
        "--artifact-readme", str(readme),
        "--guidance-dir", str(guidance),
    ])
    spec = _make_spec(ns, guidance_root=None)
    assert spec.howto_path == (guidance / "howtos").resolve()
    assert spec.primitives_dir == (guidance / "primitives").resolve()
    assert spec.examples_dir == (guidance / "examples").resolve()
