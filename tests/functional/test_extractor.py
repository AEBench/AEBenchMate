"""Plan normalization and prompt building."""
from __future__ import annotations

import json

import pytest

claude_agent_sdk = pytest.importorskip("claude_agent_sdk")

from extractor import (
    _assign_item_ids,
    _extraction_prompt,
    _make_empty_plan,
    _normalize_item,
    _normalize_plan,
    _parse_plan_json,
)



def test_parse_valid_json() -> None:
    result = _parse_plan_json('{"stages": {}}')
    assert isinstance(result, dict)
    assert "stages" in result


def test_parse_rejects_non_object() -> None:
    with pytest.raises(ValueError, match="JSON object"):
        _parse_plan_json("[1, 2, 3]")


def test_parse_rejects_garbage() -> None:
    with pytest.raises((ValueError, json.JSONDecodeError)):
        _parse_plan_json("not json at all")



def test_normalize_item_valid() -> None:
    raw = {"kind": "dependency", "name": "python", "requirement": ">=3.10",
           "command": "python --version", "expected_outputs": ["3.10"], "notes": ""}
    item = _normalize_item(raw)
    assert item is not None
    assert item["kind"] == "dependency"
    assert item["name"] == "python"
    assert item["item_id"] == ""  # assigned later


def test_normalize_item_missing_kind() -> None:
    assert _normalize_item({"name": "x"}) is None


def test_normalize_item_missing_name() -> None:
    assert _normalize_item({"kind": "dep"}) is None


def test_normalize_item_not_dict() -> None:
    assert _normalize_item("string") is None



def test_normalize_plan_assigns_ids() -> None:
    payload = {
        "artifact_name": "test",
        "source_file": "README.md",
        "summary": "a test",
        "stages": {
            "env_setup": [
                {"kind": "dependency", "name": "python"},
                {"kind": "env_var", "name": "PATH"},
            ],
            "build_artifact": [{"kind": "command", "name": "make"}],
            "benchmark_prep": [],
            "experiment_runs": [],
        },
    }
    plan = _normalize_plan(payload, readme_path="README.md")

    assert plan["stages"]["env_setup"][0]["item_id"] == "env-001"
    assert plan["stages"]["env_setup"][1]["item_id"] == "env-002"
    assert plan["stages"]["build_artifact"][0]["item_id"] == "build-001"


def test_normalize_plan_preserves_metadata() -> None:
    payload = {
        "artifact_name": "MyArtifact",
        "source_file": "README.md",
        "summary": "Does stuff",
        "stages": {"env_setup": [], "build_artifact": [], "benchmark_prep": [], "experiment_runs": []},
    }
    plan = _normalize_plan(payload, readme_path="README.md")
    assert plan["artifact_name"] == "MyArtifact"
    assert plan["summary"] == "Does stuff"
    assert plan["schema_version"] == 1


def test_normalize_plan_drops_invalid_items() -> None:
    payload = {
        "stages": {
            "env_setup": [
                {"kind": "dep", "name": "good"},
                "not a dict",
                {"kind": "", "name": "missing_kind"},
            ],
        },
    }
    plan = _normalize_plan(payload, readme_path="x")
    assert len(plan["stages"]["env_setup"]) == 1



def test_prompt_contains_readme_text() -> None:
    prompt = _extraction_prompt("This is the README content", "README.md")
    assert "This is the README content" in prompt


def test_prompt_contains_feedback_when_given() -> None:
    prompt = _extraction_prompt("readme", "README.md", feedback_text="fix the stages")
    assert "fix the stages" in prompt


def test_prompt_without_feedback() -> None:
    prompt = _extraction_prompt("readme", "README.md", feedback_text=None)
    assert "feedback" not in prompt.lower()



def test_empty_plan_has_all_stages() -> None:
    plan = _make_empty_plan("README.md")
    assert plan["source_file"] == "README.md"
    for stage in ("env_setup", "build_artifact", "benchmark_prep", "experiment_runs"):
        assert stage in plan["stages"]
        assert plan["stages"][stage] == []
