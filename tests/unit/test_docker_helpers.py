"""Docker spec utilities."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from run_docker import (
    DockerArgs,
    make_container_name,
    make_run_id,
    resolve_spec,
    read_cost_from_report,
    validate_spec,
)
from utils import EvalSpec


def test_make_container_name_lowercase() -> None:
    name = make_container_name("MyArtifact", "20260101-120000")
    assert name == name.lower()


def test_make_container_name_no_special_chars() -> None:
    name = make_container_name("art!@#$fact", "run-1")
    assert re.match(r"^[a-z0-9._-]+$", name)


def test_make_container_name_contains_parts() -> None:
    name = make_container_name("widget", "20260101-120000")
    assert "widget" in name
    assert "20260101-120000" in name


def test_make_run_id_nonempty() -> None:
    rid = make_run_id()
    assert len(rid) > 0


def test_make_run_id_starts_with_digits() -> None:
    rid = make_run_id()
    assert rid[0].isdigit()


def test_read_spent_usd_extract_stage(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"cost_usd": 0.42}))
    assert read_cost_from_report(report, "extract") == 0.42


def test_read_spent_usd_generate_stage(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"cost_usd": 1.5}))
    assert read_cost_from_report(report, "generate") == 1.5


def test_read_spent_usd_pipeline_generate(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"generate": {"cost_usd": 0.7}}))
    assert read_cost_from_report(report, "pipeline") == 0.7


def test_read_spent_usd_pipeline_extract_fallback(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"extract": {"cost_usd": 0.3}}))
    assert read_cost_from_report(report, "pipeline") == 0.3


def test_read_spent_usd_missing_file(tmp_path: Path) -> None:
    assert read_cost_from_report(tmp_path / "nope.json", "extract") is None


def test_read_spent_usd_no_key(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"other": "data"}))
    assert read_cost_from_report(report, "extract") is None


def test_read_spent_usd_unknown_stage(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"cost_usd": 1.0}))
    assert read_cost_from_report(report, "unknown") is None


def _make_spec(tmp_path: Path, **overrides) -> DockerArgs:
    artifact_dir = tmp_path / "artifact"
    artifact_dir.mkdir(exist_ok=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    readme = artifact_dir / "README.md"
    readme.write_text("# Test")
    howto = tmp_path / "howto"
    howto.mkdir(exist_ok=True)
    defaults = dict(
        artifact_id="test-art",
        workspace_dir=workspace,
        eval_spec=EvalSpec(
            artifact_id="test-art",
            artifact_dir=artifact_dir,
            artifact_readme=readme,
            howto_path=howto,
        ),
        image_ref="test:latest",
        stage="validate",
    )
    defaults.update(overrides)
    return DockerArgs(**defaults)


def test_normalize_spec_defaults_internals_dir(tmp_path: Path) -> None:
    spec = _make_spec(tmp_path, internals_dir=None)
    result = resolve_spec(spec)
    assert result.internals_dir is not None
    assert result.internals_dir == (spec.workspace_dir / "_agent_internals").resolve()


def test_normalize_spec_resolves_workspace(tmp_path: Path) -> None:
    spec = _make_spec(tmp_path)
    result = resolve_spec(spec)
    assert result.workspace_dir.is_absolute()


def test_validate_spec_missing_workspace(tmp_path: Path) -> None:
    spec = _make_spec(tmp_path, workspace_dir=tmp_path / "nonexistent")
    spec = resolve_spec(spec)
    with pytest.raises(FileNotFoundError, match="Workspace"):
        validate_spec(spec)


def test_validate_spec_invalid_stage(tmp_path: Path) -> None:
    spec = _make_spec(tmp_path, stage="bogus")
    spec = resolve_spec(spec)
    with pytest.raises(ValueError, match="Unsupported stage"):
        validate_spec(spec)


def test_validate_spec_missing_api_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    spec = _make_spec(tmp_path, stage="extract")
    spec = resolve_spec(spec)
    with pytest.raises(OSError, match="ANTHROPIC_API_KEY"):
        validate_spec(spec)
