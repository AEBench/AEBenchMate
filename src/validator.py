#!/usr/bin/env python3
from __future__ import annotations

import ast
import py_compile
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, NamedTuple

import logging

from utils import read_json_if_exists

logger = logging.getLogger("validator")

ORACLES_DIR = "oracles"

REQUIRED_ORACLE_FILES = {
    "env_setup.py": "OracleEnvSetup",
    "artifact_build.py": "OracleArtifactBuild",
    "benchmark_prep.py": "OracleBenchmarkPrep",
    "experiment_runs.py": "OracleExperimentRuns",
}

STAGE_ID_PREFIX = {
    "env_setup": "env-",
    "build_artifact": "build-",
    "benchmark_prep": "prep-",
    "experiment_runs": "run-",
}

FORBIDDEN_CALLS = {"input", "getpass", "getpass.getpass", "os.system", "pdb.set_trace"}


class PlanItem(NamedTuple):
    stage_name: str
    item_id: str
    category: str
    name: str


@dataclass
class ValidationIssue:
    code: str
    severity: str
    path: str | Path
    message: str

    def __post_init__(self) -> None:
        self.path = str(self.path)


@dataclass(frozen=True)
class ValidationReport:
    ok: bool
    issues: tuple[ValidationIssue, ...]


def report_to_dict(report: ValidationReport) -> dict[str, Any]:
    return {"ok": report.ok, "issues": [asdict(i) for i in report.issues]}


def validate_oracle_package(root: Path) -> ValidationReport:
    """Check oracles/ under root for required files, AST issues, plan coverage."""
    root = root.resolve()
    issues: list[ValidationIssue] = []

    for name in REQUIRED_ORACLE_FILES:
        oracle_path = root / ORACLES_DIR / name
        if not oracle_path.exists():
            issues.append(ValidationIssue(code="missing_file", severity="error", path=oracle_path, message="required file missing"))

    plan_path = root / ORACLES_DIR / "oracle_plan.json"
    if not plan_path.exists():
        issues.append(ValidationIssue(code="missing_plan", severity="warning", path=plan_path, message="oracle_plan.json missing"))

    oracles_dir = root / ORACLES_DIR
    if oracles_dir.is_dir():
        for path in sorted(oracles_dir.glob("*.py")):
            issues.extend(_check_python_file(path))

    issues.extend(_check_oracle_plan(root))
    issues.extend(_check_plan_coverage(root))

    ok = not any(i.severity == "error" for i in issues)
    return ValidationReport(ok=ok, issues=tuple(issues))


def validate_artifact_plan(root_or_path: Path) -> ValidationReport:
    """Check artifact_plan.json for required keys and stage structure."""
    path = root_or_path.resolve()
    if path.is_dir():
        path = path / "artifact_plan.json"

    issues: list[ValidationIssue] = []

    if not path.exists():
        return ValidationReport(ok=False, issues=(
            ValidationIssue(code="missing_artifact_plan", severity="error", path=path, message="artifact_plan.json missing"),
        ))

    try:
        payload = read_json_if_exists(path)
    except Exception as exc:
        return ValidationReport(ok=False, issues=(
            ValidationIssue(code="invalid_artifact_plan_json", severity="error", path=path, message=str(exc)),
        ))

    if not isinstance(payload, dict):
        return ValidationReport(ok=False, issues=(
            ValidationIssue(code="artifact_plan_not_object", severity="error", path=path, message="must be a JSON object"),
        ))

    if payload.get("schema_version") != 1:
        issues.append(ValidationIssue(code="artifact_plan_schema_version", severity="warning", path=path, message="schema_version should be 1"))

    for key in ("artifact_name", "source_file", "summary", "stages"):
        if key not in payload:
            issues.append(ValidationIssue(code="artifact_plan_key_missing", severity="error", path=path, message=f"missing key {key!r}"))
        elif key != "stages" and not isinstance(payload[key], str):
            issues.append(ValidationIssue(code="artifact_plan_type", severity="error", path=path, message=f"{key} must be a string"))

    if "stages" in payload and not isinstance(payload.get("stages"), dict):
        issues.append(ValidationIssue(code="artifact_plan_type", severity="error", path=path, message="stages must be a JSON object"))

    stages = payload.get("stages", {})
    if isinstance(stages, dict):
        issues.extend(_check_artifact_plan_stages(path, stages))

    ok = not any(i.severity == "error" for i in issues)
    return ValidationReport(ok=ok, issues=tuple(issues))


def _check_python_file(path: Path) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []

    try:
        py_compile.compile(str(path), doraise=True)
    except Exception as exc:
        return [ValidationIssue(code="compile_error", severity="error", path=path, message=str(exc))]

    try:
        source = path.read_text("utf-8")
        tree = ast.parse(source, filename=str(path))
    except Exception as exc:
        return [ValidationIssue(code="ast_error", severity="error", path=path, message=str(exc))]

    expected_class = REQUIRED_ORACLE_FILES.get(path.name)
    if expected_class:
        class_names = {n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
        if expected_class not in class_names:
            issues.append(ValidationIssue(code="missing_class", severity="error", path=path, message=f"expected class {expected_class} not found"))

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _get_call_name(node.func)
        base_name = name.split(".")[-1]
        if name in FORBIDDEN_CALLS or base_name in FORBIDDEN_CALLS:
            issues.append(ValidationIssue(code="forbidden_call", severity="error", path=path, message=f"forbidden call: {name}"))
        if name in {"subprocess.Popen", "Popen"} or base_name in {"Popen"}:
            issues.append(ValidationIssue(code="forbidden_popen", severity="error", path=path, message="Popen not allowed"))

    for pattern in (r"/home/[^/]+", r"/Users/[^/]+"):
        m = re.search(pattern, source)
        if m:
            issues.append(ValidationIssue(code="hardcoded_path", severity="warning", path=path, message=f"hardcoded path: {m.group(0)}"))

    return issues


def _check_oracle_plan(root: Path) -> list[ValidationIssue]:
    path = root / ORACLES_DIR / "oracle_plan.json"
    if not path.exists():
        return []

    try:
        payload = read_json_if_exists(path)
    except Exception as exc:
        return [ValidationIssue(code="invalid_oracle_plan_json", severity="warning", path=path, message=str(exc))]

    if not isinstance(payload, dict):
        return [ValidationIssue(code="oracle_plan_not_object", severity="warning", path=path, message="should be a JSON object")] if payload is not None else []

    issues: list[ValidationIssue] = []
    for key in ("howto_rules", "example_patterns", "borrowed_structure",
                "artifact_specific_adaptations", "reference_templates",
                "plan_summary", "step_coverage", "step_gaps", "step_augmentations"):
        if key not in payload:
            issues.append(ValidationIssue(code="oracle_plan_key_missing", severity="warning", path=path, message=f"missing key {key!r}"))

    templates = payload.get("reference_templates")
    if templates is not None and not isinstance(templates, dict):
        issues.append(ValidationIssue(code="oracle_plan_templates_type", severity="warning", path=path, message="reference_templates should be a dict"))

    return issues


def _check_plan_coverage(root: Path) -> list[ValidationIssue]:
    """Verify all artifact_plan items show up in step_coverage or step_gaps."""
    try:
        artifact_plan = read_json_if_exists(root / "artifact_plan.json")
    except Exception:
        return []
    if not isinstance(artifact_plan, dict):
        return []

    stages = artifact_plan.get("stages", {})
    if not isinstance(stages, dict):
        return []

    required: list[PlanItem] = []
    for stage_name in ("env_setup", "build_artifact", "benchmark_prep", "experiment_runs"):
        items = stages.get(stage_name, [])
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            item_id = item.get("item_id", "")
            if isinstance(item_id, str) and item_id:
                required.append(PlanItem(
                    stage_name=stage_name,
                    item_id=item_id,
                    category=str(item.get("kind", "")),
                    name=str(item.get("name", "")),
                ))

    if not required:
        return []

    try:
        oracle_plan = read_json_if_exists(root / ORACLES_DIR / "oracle_plan.json")
    except Exception:
        oracle_plan = {}
    if not isinstance(oracle_plan, dict):
        oracle_plan = {}

    prefixes = tuple(STAGE_ID_PREFIX.values())
    accounted = _find_matching_ids(oracle_plan.get("step_coverage"), prefixes) | _find_matching_ids(oracle_plan.get("step_gaps"), prefixes)

    issues: list[ValidationIssue] = []
    oracle_plan_path = root / ORACLES_DIR / "oracle_plan.json"
    for item in required:
        if item.item_id not in accounted:
            issues.append(ValidationIssue(
                code="uncovered_plan_item", severity="error", path=oracle_plan_path,
                message=f"[{item.stage_name}] {item.item_id!r} (category={item.category!r}, name={item.name!r}) not in step_coverage or step_gaps",
            ))
    return issues


def _find_matching_ids(data: Any, prefixes: tuple[str, ...]) -> set[str]:
    """Recursively find strings matching item ID prefixes."""
    ids: set[str] = set()
    if isinstance(data, str) and data.startswith(prefixes):
        ids.add(data)
    elif isinstance(data, list):
        for entry in data:
            ids.update(_find_matching_ids(entry, prefixes))
    elif isinstance(data, dict):
        for key, entry in data.items():
            if isinstance(key, str) and key.startswith(prefixes):
                ids.add(key)
            ids.update(_find_matching_ids(entry, prefixes))
    return ids


def _check_artifact_plan_stages(path: Path, stages: dict[str, Any]) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []

    for stage_name, prefix in STAGE_ID_PREFIX.items():
        if stage_name not in stages:
            issues.append(ValidationIssue(code="artifact_plan_stage_missing", severity="error", path=path, message=f"missing stage {stage_name!r}"))
            continue

        items = stages[stage_name]
        if not isinstance(items, list):
            issues.append(ValidationIssue(code="artifact_plan_stage_type", severity="error", path=path, message=f"stage {stage_name!r} must be a list"))
            continue

        seen_ids: set[str] = set()
        for idx, item in enumerate(items, 1):
            label = f"{stage_name}[{idx}]"
            if not isinstance(item, dict):
                issues.append(ValidationIssue(code="artifact_plan_item_type", severity="error", path=path, message=f"{label} must be an object"))
                continue

            for field in ("item_id", "kind", "name", "requirement", "command", "expected_outputs", "notes"):
                if field not in item:
                    issues.append(ValidationIssue(code="artifact_plan_item_field_missing", severity="error", path=path, message=f"{label} missing {field!r}"))

            item_id = item.get("item_id")
            if isinstance(item_id, str):
                if item_id in seen_ids:
                    issues.append(ValidationIssue(code="artifact_plan_duplicate_item_id", severity="error", path=path, message=f"duplicate {item_id!r}"))
                seen_ids.add(item_id)
                if not item_id.startswith(prefix):
                    issues.append(ValidationIssue(code="artifact_plan_item_id_prefix", severity="error", path=path, message=f"{label} id {item_id!r} should start with {prefix!r}"))
            elif item_id is not None:
                issues.append(ValidationIssue(code="artifact_plan_item_field_type", severity="error", path=path, message=f"{label} item_id must be a string"))

            for field in ("kind", "name", "requirement", "command", "notes"):
                field_value = item.get(field)
                if field_value is not None and not isinstance(field_value, str):
                    issues.append(ValidationIssue(code="artifact_plan_item_field_type", severity="error", path=path, message=f"{label} {field!r} must be a string"))

            outputs = item.get("expected_outputs")
            if outputs is not None and not isinstance(outputs, list):
                issues.append(ValidationIssue(code="artifact_plan_item_field_type", severity="error", path=path, message=f"{label} expected_outputs must be a list"))
            elif isinstance(outputs, list):
                for output_idx, output_value in enumerate(outputs, 1):
                    if not isinstance(output_value, str):
                        issues.append(ValidationIssue(code="artifact_plan_item_field_type", severity="error", path=path, message=f"{label} expected_outputs[{output_idx}] must be a string"))

    return issues


def _get_call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _get_call_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


