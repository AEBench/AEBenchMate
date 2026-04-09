"""OracleGenConfig, I/O helpers, budget tracking, text utilities."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from utils import (
    OracleGenConfig,
    load_env_file,
    load_guidance_root,
    read_json_if_exists,
    read_text_if_exists,
    resolve_log_file,
    strip_code_fences,
    write_json_file,
)


def test_spec_output_dir_default(tmp_path: Path) -> None:
    spec = OracleGenConfig(artifact_id="x", artifact_dir=tmp_path)
    assert spec.output_dir == tmp_path / "oracles"


def test_spec_output_dir_with_base(tmp_path: Path) -> None:
    base = tmp_path / "case_bundle"
    base.mkdir()
    spec = OracleGenConfig(artifact_id="x", artifact_dir=tmp_path, output_base_dir=base)
    assert spec.output_dir == base / "oracles"


def test_spec_resolved_makes_paths_absolute(tmp_path: Path) -> None:
    spec = OracleGenConfig(artifact_id="x", artifact_dir=Path("relative/path"))
    resolved = spec.resolve_paths()
    assert resolved.artifact_dir.is_absolute()


def test_spec_auto_discover_plan_from_output_base_dir(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "artifact"
    artifact_dir.mkdir()
    output_base = tmp_path / "output"
    output_base.mkdir()
    plan = output_base / "artifact_plan.json"
    plan.write_text('{"steps": []}')
    spec = OracleGenConfig(artifact_id="test", artifact_dir=artifact_dir, output_base_dir=output_base)
    assert spec.resolve_paths().artifact_plan_path == plan.resolve()


def test_spec_auto_discover_plan_fallback_to_artifact_dir(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "artifact"
    artifact_dir.mkdir()
    output_base = tmp_path / "output"
    output_base.mkdir()
    plan = artifact_dir / "artifact_plan.json"
    plan.write_text('{"steps": []}')
    spec = OracleGenConfig(artifact_id="test", artifact_dir=artifact_dir, output_base_dir=output_base)
    assert spec.resolve_paths().artifact_plan_path == plan.resolve()


def test_spec_resolved_example_oracle_dirs(tmp_path: Path) -> None:
    d = tmp_path / "examples" / "case1"
    d.mkdir(parents=True)
    spec = OracleGenConfig(artifact_id="test", artifact_dir=tmp_path, example_oracle_dirs=(d,))
    assert all(p.is_absolute() for p in spec.resolve_paths().example_oracle_dirs)


def test_over_budget(tmp_path: Path) -> None:
    spec = OracleGenConfig(artifact_id="x", artifact_dir=tmp_path, max_budget_usd=1.0)
    spec.cost_usd = 0.5
    assert not spec.over_budget
    spec.cost_usd = 1.0
    assert spec.over_budget


def test_add_cost_accumulates_and_ignores_none(tmp_path: Path) -> None:
    spec = OracleGenConfig(artifact_id="x", artifact_dir=tmp_path)
    spec.add_cost(0.5)
    spec.add_cost(0.3)
    assert abs(spec.cost_usd - 0.8) < 1e-9
    spec.add_cost(None)
    assert abs(spec.cost_usd - 0.8) < 1e-9


@pytest.mark.parametrize("text, expected", [
    ('```json\n{"key": 1}\n```', '{"key": 1}'),
    ('{"key": 1}', '{"key": 1}'),
    ('```\nhello\nworld\n```', 'hello\nworld'),
    ('```python\nprint("hi")\n```', 'print("hi")'),
])
def test_strip_code_fences(text: str, expected: str) -> None:
    assert strip_code_fences(text) == expected


def test_load_guidance_root_missing_or_empty(tmp_path: Path) -> None:
    assert load_guidance_root(tmp_path) is None
    (tmp_path / "aebenchmate.toml").write_text("[build]\nopt = 1\n")
    assert load_guidance_root(tmp_path) is None


def test_load_guidance_root_resolves_relative(tmp_path: Path) -> None:
    (tmp_path / "aebenchmate.toml").write_text('[guidance]\nroot = "./sub/dir"\n')
    result = load_guidance_root(tmp_path)
    assert result == (tmp_path / "sub" / "dir").resolve()
    assert result.is_absolute()


def test_load_env_parses_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("# comment\n\nKEY1=plain\nexport KEY2=exported\nKEY3='single'\nKEY4=\"double\"\n")
    for k in ("KEY1", "KEY2", "KEY3", "KEY4"):
        monkeypatch.delenv(k, raising=False)
    load_env_file(env_file)
    assert os.environ["KEY1"] == "plain"
    assert os.environ["KEY2"] == "exported"
    assert os.environ["KEY3"] == "single"
    assert os.environ["KEY4"] == "double"
    for k in ("KEY1", "KEY2", "KEY3", "KEY4"):
        monkeypatch.delenv(k, raising=False)


def test_load_env_no_overwrite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PREEXIST_KEY", "original")
    (tmp_path / ".env").write_text("PREEXIST_KEY=overwritten\n")
    load_env_file(tmp_path / ".env")
    assert os.environ["PREEXIST_KEY"] == "original"


def test_load_env_missing_file(tmp_path: Path) -> None:
    load_env_file(tmp_path / "nonexistent.env")


@pytest.mark.parametrize("value, expected_none", [
    (None, True),
    ("", True),
])
def test_resolve_log_file_returns_none(value, expected_none) -> None:
    assert (resolve_log_file(Path("/base"), value) is None) == expected_none


def test_resolve_log_file_absolute() -> None:
    assert resolve_log_file(Path("/base"), "/var/log/app.log") == Path("/var/log/app.log").resolve()


def test_resolve_log_file_relative() -> None:
    assert resolve_log_file(Path("/base"), "logs/app.log") == Path("/base/logs/app.log").resolve()


def test_read_text_if_exists(tmp_path: Path) -> None:
    assert read_text_if_exists(tmp_path / "nope.txt") is None
    f = tmp_path / "file.txt"
    f.write_text("hello world", encoding="utf-8")
    assert read_text_if_exists(f) == "hello world"


def test_read_json_if_exists(tmp_path: Path) -> None:
    assert read_json_if_exists(tmp_path / "nope.json") is None
    f = tmp_path / "data.json"
    f.write_text('{"a": 1}', encoding="utf-8")
    assert read_json_if_exists(f) == {"a": 1}


def test_read_json_if_exists_invalid(tmp_path: Path) -> None:
    (tmp_path / "bad.json").write_text("{invalid", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        read_json_if_exists(tmp_path / "bad.json")


def test_write_json_creates_parents_and_options(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b" / "out.json"
    write_json_file(target, {"b": 2, "a": 1}, sort_keys=True)
    content = target.read_text("utf-8")
    assert content.endswith("\n")
    assert list(json.loads(content).keys()) == ["a", "b"]
