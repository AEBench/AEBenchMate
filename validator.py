from __future__ import annotations

import ast
import logging
import py_compile
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from utils import read_json_if_exists, write_json_file

logger = logging.getLogger("validator")

AGENT_EVAL = "_agent_eval"

REQUIRED_ORACLE_FILES = {
    "env_setup.py": "OracleEnvSetup",
    "artifact_build.py": "OracleArtifactBuild",
    "benchmark_prep.py": "OracleBenchmarkPrep",
    "experiment_runs.py": "OracleExperimentRuns",
}

FORBIDDEN_CALLS = {"input", "getpass", "getpass.getpass", "os.system", "pdb.set_trace"}


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    severity: str
    path: str
    message: str


@dataclass(frozen=True)
class ValidationReport:
    ok: bool
    issues: tuple[ValidationIssue, ...]


def _issue(code: str, severity: str, path: Path | str, message: str) -> ValidationIssue:
    return ValidationIssue(code=code, severity=severity, path=str(path), message=message)


def validate(root: Path) -> ValidationReport:
    root = root.resolve()
    issues: list[ValidationIssue] = []

    for name in REQUIRED_ORACLE_FILES:
        p = root / AGENT_EVAL / name
        if not p.exists():
            issues.append(_issue("missing_file", "error", p, "required file missing"))

    plan_path = root / AGENT_EVAL / "oracle_plan.json"
    if not plan_path.exists():
        issues.append(_issue("missing_plan", "warning", plan_path, "oracle_plan.json missing"))

    eval_dir = root / AGENT_EVAL
    if eval_dir.is_dir():
        for path in sorted(eval_dir.glob("*.py")):
            issues.extend(_check_python_file(path))

    issues.extend(_check_oracle_plan(root))
    issues.extend(_check_ref_files(root))

    ok = not any(i.severity == "error" for i in issues)
    return ValidationReport(ok=ok, issues=tuple(issues))


def create_missing_refs(root: Path) -> list[Path]:
    root = root.resolve()
    try:
        plan = read_json_if_exists(root / AGENT_EVAL / "oracle_plan.json")
    except Exception:
        return []

    if not isinstance(plan, dict):
        return []
    templates = plan.get("reference_templates")
    if not isinstance(templates, dict):
        return []

    created: list[Path] = []
    for ref_path in _extract_ref_paths(root):
        if ref_path.exists():
            continue
        template = templates.get(ref_path.name)
        if template is None:
            continue
        write_json_file(ref_path, template, indent=2, sort_keys=True)
        created.append(ref_path)
    return created


def _check_python_file(path: Path) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []

    try:
        py_compile.compile(str(path), doraise=True)
    except Exception as exc:
        return [_issue("compile_error", "error", path, str(exc))]

    try:
        source = path.read_text("utf-8")
        tree = ast.parse(source, filename=str(path))
    except Exception as exc:
        return [_issue("ast_error", "error", path, str(exc))]

    expected_class = REQUIRED_ORACLE_FILES.get(path.name)
    if expected_class:
        class_names = {n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
        if expected_class not in class_names:
            issues.append(
                _issue("missing_class", "error", path, f"expected class {expected_class} not found")
            )

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _get_call_name(node.func)
        short = name.split(".")[-1]
        if name in FORBIDDEN_CALLS or short in FORBIDDEN_CALLS:
            issues.append(_issue("forbidden_call", "error", path, f"forbidden call: {name}"))
        if name in {"subprocess.Popen", "Popen"} or short == "Popen":
            issues.append(_issue("forbidden_popen", "error", path, "Popen not allowed"))

    for pattern in (r"/home/[^/]+", r"/Users/[^/]+"):
        m = re.search(pattern, source)
        if m:
            issues.append(
                _issue("hardcoded_path", "warning", path, f"hardcoded path: {m.group(0)}")
            )

    return issues


def _check_oracle_plan(root: Path) -> list[ValidationIssue]:
    path = root / AGENT_EVAL / "oracle_plan.json"
    if not path.exists():
        return []

    try:
        payload = read_json_if_exists(path)
    except Exception as exc:
        return [_issue("invalid_oracle_plan_json", "warning", path, str(exc))]

    if not isinstance(payload, dict):
        return [_issue("oracle_plan_not_object", "warning", path, "should be a JSON object")] if payload is not None else []

    issues: list[ValidationIssue] = []
    for key in (
        "howto_rules",
        "example_patterns",
        "borrowed_structure",
        "artifact_specific_adaptations",
        "reference_templates",
        "plan_summary",
        "step_coverage",
        "step_gaps",
        "step_augmentations",
    ):
        if key not in payload:
            issues.append(_issue("oracle_plan_key_missing", "warning", path, f"missing key {key!r}"))

    templates = payload.get("reference_templates")
    if templates is not None and not isinstance(templates, dict):
        issues.append(
            _issue("oracle_plan_templates_type", "warning", path, "reference_templates should be a dict")
        )

    return issues


def _check_ref_files(root: Path) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for ref_path in _extract_ref_paths(root):
        if not ref_path.exists():
            issues.append(_issue("missing_ref", "error", ref_path, "referenced ref file missing"))
            continue
        try:
            data = read_json_if_exists(ref_path)
            if data is None:
                issues.append(_issue("missing_ref", "error", ref_path, "ref file is empty"))
        except Exception as exc:
            issues.append(_issue("invalid_ref_json", "error", ref_path, str(exc)))
    return issues


def _get_call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _get_call_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


def _collect_string_literals(node: ast.AST) -> list[str]:
    return [
        item.value
        for item in ast.walk(node)
        if isinstance(item, ast.Constant) and isinstance(item.value, str)
    ]


def _extract_ref_paths(root: Path) -> list[Path]:
    path = root / AGENT_EVAL / "main.py"
    if not path.exists():
        return []
    try:
        tree = ast.parse(path.read_text("utf-8"), filename=str(path))
    except Exception:
        return []

    results: set[Path] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _get_call_name(node.func).split(".")[-1] != "EntryConfig":
            continue
        for kw in node.keywords:
            if kw.arg != "ground_truth_paths" or not isinstance(kw.value, ast.Dict):
                continue
            for val in kw.value.values:
                parts = _collect_string_literals(val)
                if "refs" in parts:
                    for p in parts:
                        if p.endswith(".json"):
                            results.add(root / AGENT_EVAL / "refs" / Path(p).name)
    return sorted(results)
