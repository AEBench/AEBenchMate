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


def test_make_container_name() -> None:
    name = make_container_name("My!Art@fact", "20260101-120000")
    assert name == name.lower()
    assert re.match(r"^[a-z0-9._-]+$", name)
    assert "20260101-120000" in name


def test_make_run_id() -> None:
    rid = make_run_id()
    assert len(rid) > 0 and rid[0].isdigit()


@pytest.mark.parametrize("stage, payload, expected", [
    ("extract", {"cost_usd": 0.42}, 0.42),
    ("generate", {"cost_usd": 1.5}, 1.5),
    ("pipeline", {"generate": {"cost_usd": 0.7}}, 0.7),
    ("pipeline", {"extract": {"cost_usd": 0.3}}, 0.3),
    ("extract", {"other": "data"}, None),
    ("unknown", {"cost_usd": 1.0}, None),
])
def test_read_cost_from_report(tmp_path: Path, stage: str, payload: dict, expected: float | None) -> None:
    report = tmp_path / "report.json"
    report.write_text(json.dumps(payload))
    assert read_cost_from_report(report, stage) == expected


def test_read_cost_missing_file(tmp_path: Path) -> None:
    assert read_cost_from_report(tmp_path / "nope.json", "extract") is None


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


def test_resolve_spec_defaults(tmp_path: Path) -> None:
    spec = _make_spec(tmp_path, internals_dir=None)
    result = resolve_spec(spec)
    assert result.internals_dir == (spec.workspace_dir / "_agent_internals").resolve()
    assert result.workspace_dir.is_absolute()


def test_validate_spec_missing_workspace(tmp_path: Path) -> None:
    spec = resolve_spec(_make_spec(tmp_path, workspace_dir=tmp_path / "nonexistent"))
    with pytest.raises(FileNotFoundError, match="Workspace"):
        validate_spec(spec)


def test_validate_spec_invalid_stage(tmp_path: Path) -> None:
    spec = resolve_spec(_make_spec(tmp_path, stage="bogus"))
    with pytest.raises(ValueError, match="Unsupported stage"):
        validate_spec(spec)


def test_validate_spec_missing_api_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    spec = resolve_spec(_make_spec(tmp_path, stage="extract"))
    with pytest.raises(OSError, match="ANTHROPIC_API_KEY"):
        validate_spec(spec)
