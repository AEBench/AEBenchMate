"""File I/O and config helpers."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from utils import (
    EvalSpec,
    load_env_file,
    load_guidance_root,
    read_json_if_exists,
    read_text_if_exists,
    resolve_log_file,
    strip_code_fences,
    write_json_file,
)


def test_load_guidance_root_no_config(tmp_path: Path) -> None:
    assert load_guidance_root(tmp_path) is None


def test_load_guidance_root_no_guidance_section(tmp_path: Path) -> None:
    (tmp_path / "aebenchmate.toml").write_text("[build]\nopt = 1\n")
    assert load_guidance_root(tmp_path) is None


def test_load_guidance_root_returns_resolved(tmp_path: Path) -> None:
    (tmp_path / "aebenchmate.toml").write_text('[guidance]\nroot = "my_guidance"\n')
    result = load_guidance_root(tmp_path)
    assert result == (tmp_path / "my_guidance").resolve()


def test_load_guidance_root_relative_path(tmp_path: Path) -> None:
    (tmp_path / "aebenchmate.toml").write_text('[guidance]\nroot = "./sub/dir"\n')
    result = load_guidance_root(tmp_path)
    assert result == (tmp_path / "sub" / "dir").resolve()
    assert result.is_absolute()


def test_load_env_ignores_comments(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("# comment\nTEST_KEY_1=hello\n")
    monkeypatch.delenv("TEST_KEY_1", raising=False)
    load_env_file(env_file)
    assert os.environ["TEST_KEY_1"] == "hello"
    monkeypatch.delenv("TEST_KEY_1", raising=False)


def test_load_env_ignores_blank_lines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("\n\nBLANK_TEST=yes\n\n")
    monkeypatch.delenv("BLANK_TEST", raising=False)
    load_env_file(env_file)
    assert os.environ["BLANK_TEST"] == "yes"
    monkeypatch.delenv("BLANK_TEST", raising=False)


def test_load_env_export_syntax(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("export EXPORT_KEY=exported_val\n")
    monkeypatch.delenv("EXPORT_KEY", raising=False)
    load_env_file(env_file)
    assert os.environ["EXPORT_KEY"] == "exported_val"
    monkeypatch.delenv("EXPORT_KEY", raising=False)


def test_load_env_single_quotes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("SQ_KEY='single quoted'\n")
    monkeypatch.delenv("SQ_KEY", raising=False)
    load_env_file(env_file)
    assert os.environ["SQ_KEY"] == "single quoted"
    monkeypatch.delenv("SQ_KEY", raising=False)


def test_load_env_double_quotes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text('DQ_KEY="double quoted"\n')
    monkeypatch.delenv("DQ_KEY", raising=False)
    load_env_file(env_file)
    assert os.environ["DQ_KEY"] == "double quoted"
    monkeypatch.delenv("DQ_KEY", raising=False)


def test_load_env_no_overwrite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PREEXIST_KEY", "original")
    env_file = tmp_path / ".env"
    env_file.write_text("PREEXIST_KEY=overwritten\n")
    load_env_file(env_file)
    assert os.environ["PREEXIST_KEY"] == "original"


def test_load_env_missing_file(tmp_path: Path) -> None:
    load_env_file(tmp_path / "nonexistent.env")


def test_resolve_log_file_none() -> None:
    assert resolve_log_file(Path("/base"), None) is None


def test_resolve_log_file_empty() -> None:
    assert resolve_log_file(Path("/base"), "") is None


def test_resolve_log_file_absolute() -> None:
    result = resolve_log_file(Path("/base"), "/var/log/app.log")
    assert result == Path("/var/log/app.log").resolve()


def test_resolve_log_file_relative() -> None:
    result = resolve_log_file(Path("/base"), "logs/app.log")
    assert result == Path("/base/logs/app.log").resolve()


def test_read_text_if_exists_found(tmp_path: Path) -> None:
    f = tmp_path / "file.txt"
    f.write_text("hello world", encoding="utf-8")
    assert read_text_if_exists(f) == "hello world"


def test_read_text_if_exists_missing(tmp_path: Path) -> None:
    assert read_text_if_exists(tmp_path / "nope.txt") is None


def test_read_json_if_exists_valid(tmp_path: Path) -> None:
    f = tmp_path / "data.json"
    f.write_text('{"a": 1}', encoding="utf-8")
    assert read_json_if_exists(f) == {"a": 1}


def test_read_json_if_exists_missing(tmp_path: Path) -> None:
    assert read_json_if_exists(tmp_path / "nope.json") is None


def test_read_json_if_exists_invalid(tmp_path: Path) -> None:
    f = tmp_path / "bad.json"
    f.write_text("{invalid", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        read_json_if_exists(f)


def test_write_json_creates_parents(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b" / "out.json"
    write_json_file(target, {"x": 1})
    assert target.exists()
    content = target.read_text("utf-8")
    assert content.endswith("\n")
    assert json.loads(content) == {"x": 1}


def test_write_json_sort_keys(tmp_path: Path) -> None:
    target = tmp_path / "sorted.json"
    write_json_file(target, {"b": 2, "a": 1}, sort_keys=True)
    content = target.read_text("utf-8")
    keys = list(json.loads(content).keys())
    assert keys == ["a", "b"]


def test_write_json_indent(tmp_path: Path) -> None:
    target = tmp_path / "indented.json"
    write_json_file(target, {"k": "v"}, indent=4)
    content = target.read_text("utf-8")
    assert "    " in content


def test_eval_spec_auto_discover_plan_from_output_base_dir(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "artifact"
    artifact_dir.mkdir()
    output_base = tmp_path / "output"
    output_base.mkdir()
    plan = output_base / "artifact_plan.json"
    plan.write_text('{"steps": []}')
    spec = EvalSpec(
        artifact_id="test",
        artifact_dir=artifact_dir,
        output_base_dir=output_base,
    )
    resolved = spec.resolved()
    assert resolved.artifact_plan_path == plan.resolve()


def test_eval_spec_auto_discover_plan_fallback_to_artifact_dir(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "artifact"
    artifact_dir.mkdir()
    output_base = tmp_path / "output"
    output_base.mkdir()
    plan = artifact_dir / "artifact_plan.json"
    plan.write_text('{"steps": []}')
    spec = EvalSpec(
        artifact_id="test",
        artifact_dir=artifact_dir,
        output_base_dir=output_base,
    )
    resolved = spec.resolved()
    assert resolved.artifact_plan_path == plan.resolve()


def test_eval_spec_resolved_example_oracle_dirs(tmp_path: Path) -> None:
    d = tmp_path / "examples" / "case1"
    d.mkdir(parents=True)
    spec = EvalSpec(
        artifact_id="test",
        artifact_dir=tmp_path,
        example_oracle_dirs=(d,),
    )
    resolved = spec.resolved()
    assert all(p.is_absolute() for p in resolved.example_oracle_dirs)


def test_strip_code_fences_language_tag() -> None:
    text = '```python\nprint("hi")\n```'
    assert strip_code_fences(text) == 'print("hi")'


def test_strip_code_fences_inner_block_preserved() -> None:
    text = '```\nouter\n```\ninner\n```\nmore\n```'
    result = strip_code_fences(text)
    assert "inner" in result
