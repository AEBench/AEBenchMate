#!/usr/bin/env python3
from __future__ import annotations

import ast
import py_compile
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, NamedTuple

import logging

from utils import read_json_if_exists, write_json_file

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
    kind: str
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


def _report_dict(report: ValidationReport) -> dict[str, Any]:
    return {"ok": report.ok, "issues": [asdict(i) for i in report.issues]}


def validate(root: Path) -> ValidationReport:
    """Check oracles/ under root for required files, AST issues, plan coverage."""
    root = root.resolve()
    issues: list[ValidationIssue] = []

    for name in REQUIRED_ORACLE_FILES:
        p = root / ORACLES_DIR / name
        if not p.exists():
            issues.append(ValidationIssue(code="missing_file", severity="error", path=p, message="required file missing"))

    plan_path = root / ORACLES_DIR / "oracle_plan.json"
    if not plan_path.exists():
        issues.append(ValidationIssue(code="missing_plan", severity="warning", path=plan_path, message="oracle_plan.json missing"))

    eval_dir = root / ORACLES_DIR
    if eval_dir.is_dir():
        for path in sorted(eval_dir.glob("*.py")):
            issues.extend(_check_python_file(path))

    issues.extend(_check_main_contract(root))
    issues.extend(_check_oracle_plan(root))
    issues.extend(_check_plan_coverage(root))

    for ref_path in _extract_ref_paths(root):
        if not ref_path.exists():
            issues.append(ValidationIssue(code="missing_ref", severity="error", path=ref_path, message="referenced ref file missing"))
            continue
        try:
            data = read_json_if_exists(ref_path)
            if data is None:
                issues.append(ValidationIssue(code="missing_ref", severity="error", path=ref_path, message="ref file is empty"))
        except Exception as exc:
            issues.append(ValidationIssue(code="invalid_ref_json", severity="error", path=ref_path, message=str(exc)))

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


def create_missing_refs(root: Path) -> list[Path]:
    """Write missing ref files using oracle_plan.json templates."""
    root = root.resolve()
    try:
        plan = read_json_if_exists(root / ORACLES_DIR / "oracle_plan.json")
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
        return [ValidationIssue(code="compile_error", severity="error", path=path, message=str(exc))]

    try:
        source = path.read_text("utf-8")
        tree = ast.parse(source, filename=str(path))
    except Exception as exc:
        return [ValidationIssue(code="ast_error", severity="error", path=path, message=str(exc))]

    rel = path.relative_to(path.parent.parent).as_posix()
    expected_class = REQUIRED_ORACLE_FILES.get(path.name)
    if expected_class:
        class_names = {n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
        if expected_class not in class_names:
            issues.append(ValidationIssue(code="missing_class", severity="error", path=path, message=f"expected class {expected_class} not found"))

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _get_call_name(node.func)
        short = name.split(".")[-1]
        if name in FORBIDDEN_CALLS or short in FORBIDDEN_CALLS:
            issues.append(ValidationIssue(code="forbidden_call", severity="error", path=path, message=f"forbidden call: {name}"))
        if name in {"subprocess.Popen", "Popen"} or short in {"Popen"}:
            issues.append(ValidationIssue(code="forbidden_popen", severity="error", path=path, message="Popen not allowed"))

    for pattern in (r"/home/[^/]+", r"/Users/[^/]+"):
        m = re.search(pattern, source)
        if m:
            issues.append(ValidationIssue(code="hardcoded_path", severity="warning", path=path, message=f"hardcoded path: {m.group(0)}"))

    return issues


def _check_main_contract(root: Path) -> list[ValidationIssue]:
    path = root / ORACLES_DIR / "main.py"
    if not path.exists():
        return []

    try:
        source = path.read_text("utf-8")
    except Exception as exc:
        return [ValidationIssue(code="read_error", severity="error", path=path, message=str(exc))]

    try:
        tree = ast.parse(source, filename=str(path))
    except Exception:
        return []

    issues: list[ValidationIssue] = []
    if not any(isinstance(n, ast.FunctionDef) and n.name == "main" for n in ast.walk(tree)):
        issues.append(ValidationIssue(code="missing_main", severity="error", path=path, message="must define main(argv)"))

    for token in ("EntryConfig", "env_setup", "artifact_build", "benchmark_prep", "experiment_runs"):
        if token not in source:
            issues.append(ValidationIssue(code="main_contract", severity="error", path=path, message=f"missing token {token!r}"))

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
                    kind=str(item.get("kind", "")),
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
    accounted = _collect_ids(oracle_plan.get("step_coverage"), prefixes) | _collect_ids(oracle_plan.get("step_gaps"), prefixes)

    issues: list[ValidationIssue] = []
    oracle_plan_path = root / ORACLES_DIR / "oracle_plan.json"
    for item in required:
        stage_name, item_id, kind, name = item.stage_name, item.item_id, item.kind, item.name
        if item_id not in accounted:
            issues.append(ValidationIssue(
                code="uncovered_plan_item", severity="error", path=oracle_plan_path,
                message=f"[{stage_name}] {item_id!r} (kind={kind!r}, name={name!r}) not in step_coverage or step_gaps",
            ))
    return issues


def _collect_ids(value: Any, prefixes: tuple[str, ...]) -> set[str]:
    """Recursively collect strings matching item ID prefixes."""
    ids: set[str] = set()
    if isinstance(value, str) and value.startswith(prefixes):
        ids.add(value)
    elif isinstance(value, list):
        for v in value:
            ids.update(_collect_ids(v, prefixes))
    elif isinstance(value, dict):
        for k, v in value.items():
            if isinstance(k, str) and k.startswith(prefixes):
                ids.add(k)
            ids.update(_collect_ids(v, prefixes))
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
                v = item.get(field)
                if v is not None and not isinstance(v, str):
                    issues.append(ValidationIssue(code="artifact_plan_item_field_type", severity="error", path=path, message=f"{label} {field!r} must be a string"))

            outputs = item.get("expected_outputs")
            if outputs is not None and not isinstance(outputs, list):
                issues.append(ValidationIssue(code="artifact_plan_item_field_type", severity="error", path=path, message=f"{label} expected_outputs must be a list"))
            elif isinstance(outputs, list):
                for oi, v in enumerate(outputs, 1):
                    if not isinstance(v, str):
                        issues.append(ValidationIssue(code="artifact_plan_item_field_type", severity="error", path=path, message=f"{label} expected_outputs[{oi}] must be a string"))

    return issues


def _get_call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _get_call_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


def _extract_ref_paths(root: Path) -> list[Path]:
    path = root / ORACLES_DIR / "main.py"
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
                parts = [item.value for item in ast.walk(val) if isinstance(item, ast.Constant) and isinstance(item.value, str)]
                if "refs" in parts:
                    for p in parts:
                        if p.endswith(".json"):
                            results.add(root / ORACLES_DIR / "refs" / Path(p).name)
    return sorted(results)
