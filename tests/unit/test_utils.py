"""Spec resolution, budget tracking, text helpers."""
from __future__ import annotations

from pathlib import Path

import pytest

from utils import EvalSpec, budget_exhausted, add_cost, strip_code_fences


def test_spec_output_dir_default(tmp_path: Path) -> None:
    spec = EvalSpec(artifact_id="x", artifact_dir=tmp_path)
    assert spec.output_dir == tmp_path / "oracles"


def test_spec_output_dir_with_base(tmp_path: Path) -> None:
    base = tmp_path / "case_bundle"
    base.mkdir()
    spec = EvalSpec(artifact_id="x", artifact_dir=tmp_path, output_base_dir=base)
    assert spec.output_dir == base / "oracles"


def test_spec_resolved_makes_paths_absolute(tmp_path: Path) -> None:
    spec = EvalSpec(artifact_id="x", artifact_dir=Path("relative/path"))
    resolved = spec.resolved()
    assert resolved.artifact_dir.is_absolute()



def test_budget_not_exhausted() -> None:
    spec = EvalSpec(artifact_id="x", artifact_dir=Path("/tmp"), max_budget_usd=5.0)
    spec.cost_usd = 2.0
    assert not budget_exhausted(spec)


def test_budget_exhausted_at_limit() -> None:
    spec = EvalSpec(artifact_id="x", artifact_dir=Path("/tmp"), max_budget_usd=1.0)
    spec.cost_usd = 1.0
    assert budget_exhausted(spec)


def test_update_spent_usd_accumulates() -> None:
    spec = EvalSpec(artifact_id="x", artifact_dir=Path("/tmp"))
    spec.cost_usd = 0.0
    add_cost(spec, 0.5)
    add_cost(spec, 0.3)
    assert abs(spec.cost_usd - 0.8) < 1e-9


def test_update_spent_usd_none_is_noop() -> None:
    spec = EvalSpec(artifact_id="x", artifact_dir=Path("/tmp"))
    spec.cost_usd = 1.0
    add_cost(spec, None)
    assert spec.cost_usd == 1.0



def test_strip_fences_json() -> None:
    text = '```json\n{"key": 1}\n```'
    assert strip_code_fences(text) == '{"key": 1}'


def test_strip_fences_no_fences() -> None:
    text = '{"key": 1}'
    assert strip_code_fences(text) == '{"key": 1}'


def test_strip_fences_plain() -> None:
    text = '```\nhello\nworld\n```'
    assert strip_code_fences(text) == 'hello\nworld'
