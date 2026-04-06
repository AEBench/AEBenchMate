#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import logging

from utils import (
    EvalSpec,
    configure_logging,
    load_guidance_root,
    load_env_file,
    read_json_if_exists,
    resolve_log_file,
    write_json_file,
)

from dashboard import (
    print_footer_mode,
    print_info,
    print_paths_summary,
    print_stage_header,
    print_success,
    print_warning,
)

logger = logging.getLogger("docker")

_ARTIFACT_DIR = Path("/workspace/artifact")
_GUIDANCE_DIR = Path("/workspace/guidance")
_RUNNER_DIR = Path("/workspace/runner")
_OUTPUT_DIR = Path("/workspace/output")

# Safety net: kill docker exec if container stalls without producing output.
_DOCKER_EXEC_TIMEOUT_S: float = 6 * 3600


@dataclass(frozen=True)
class DockerArgs:
    artifact_id: str
    workspace_dir: Path
    eval_spec: EvalSpec
    image_ref: str
    stage: str = "pipeline"
    internals_dir: Path | None = None
    docker_bin: str = "docker"
    overwrite_host_output: bool = False
    pull_image: bool = False


@dataclass(frozen=True)
class DockerResult:
    container_name: str
    container_id: str
    run_id: str
    run_dir: Path
    output_agent_eval_dir: Path
    internals_dir: Path
    report_path: Path
    exit_code: int
    metadata_path: Path
    cost_usd: float | None = None


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def run(spec: DockerArgs) -> DockerResult:
    spec = resolve_spec(spec)
    validate_spec(spec)
    ensure_image(spec)

    output_agent_eval_dir = spec.workspace_dir / "_agent_eval"
    if output_agent_eval_dir.exists():
        if spec.overwrite_host_output:
            shutil.rmtree(output_agent_eval_dir)
        else:
            raise FileExistsError(f"Host output already exists: {output_agent_eval_dir}")

    run_id = make_run_id()
    internals_root = spec.internals_dir
    assert internals_root is not None
    run_dir = internals_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    docker_log_path = run_dir / "docker_exec.log"
    report_path = run_dir / f"{spec.stage}_report.json"
    metadata_path = run_dir / "metadata.json"

    container_name = make_container_name(spec.artifact_id, run_id)
    logger.info("artifact_id=%s", spec.artifact_id)
    logger.info("workspace_dir=%s", spec.workspace_dir)
    logger.info("artifact_dir=%s", spec.eval_spec.artifact_dir)
    logger.info("stage=%s", spec.stage)
    logger.info("image_ref=%s", spec.image_ref)
    logger.info("container_name=%s", container_name)

    print_stage_header("docker", spec.artifact_id)
    print_info(f"workspace: {spec.workspace_dir.as_posix()}")
    print_info(f"container: {container_name}")
    print_footer_mode(f"stage: {spec.stage}")

    state: dict[str, object] = {
        "artifact_id": spec.artifact_id,
        "workspace_dir": spec.workspace_dir.as_posix(),
        "artifact_dir": spec.eval_spec.artifact_dir.as_posix(),
        "artifact_readme": (
            spec.eval_spec.artifact_readme.as_posix()
            if spec.eval_spec.artifact_readme is not None
            else None
        ),
        "howto_path": (
            spec.eval_spec.howto_path.as_posix()
            if spec.eval_spec.howto_path is not None
            else None
        ),
        "artifact_plan_path": (
            spec.eval_spec.artifact_plan_path.as_posix()
            if spec.eval_spec.artifact_plan_path is not None
            else None
        ),
        "example_oracle_dirs": [
            path.as_posix() for path in spec.eval_spec.example_oracle_dirs
        ],
        "image_ref": spec.image_ref,
        "stage": spec.stage,
        "run_id": run_id,
        "run_dir": run_dir.as_posix(),
        "container_name": container_name,
        "created_at_epoch": time.time(),
        "status": "created",
    }
    write_json_file(metadata_path, state, indent=2, sort_keys=True)

    exit_code = 1
    container_id = ""
    cost_usd: float | None = None

    try:
        container_id = create_container(spec, container_name)
        state["container_id"] = container_id
        state["status"] = "created_container"
        write_json_file(metadata_path, state, indent=2, sort_keys=True)

        start_container(spec, container_name)
        state["status"] = "running"
        write_json_file(metadata_path, state, indent=2, sort_keys=True)

        prepare_container_dirs(spec, container_name)
        copy_inputs(spec, container_name)

        exit_code = run_stage_in_container(
            spec=spec,
            container_name=container_name,
            log_path=docker_log_path,
            report_path=report_path,
        )

        export_outputs(
            spec=spec,
            container_name=container_name,
            run_dir=run_dir,
            output_agent_eval_dir=output_agent_eval_dir,
        )
        
        cost_usd = read_cost_from_report(report_path, spec.stage)
        state["cost_usd"] = cost_usd

        state["status"] = "completed" if exit_code == 0 else "failed"
        state["exit_code"] = exit_code
        state["report_path"] = report_path.as_posix()
        state["output_agent_eval_dir"] = output_agent_eval_dir.as_posix()
        state["internals_dir"] = run_dir.as_posix()
        write_json_file(metadata_path, state, indent=2, sort_keys=True)

    finally:
        try:
            if container_name:
                stop_container(spec, container_name)
        finally:
            state["finished_at_epoch"] = time.time()
            state["exit_code"] = exit_code
            state["logs"] = {"docker_exec": docker_log_path.as_posix()}
            write_json_file(metadata_path, state, indent=2, sort_keys=True)

    print_paths_summary(
        clean_agent_eval_dir=output_agent_eval_dir,
        internals_dir=run_dir,
        report_path=report_path,
    )
    if exit_code == 0:
        print_success("docker run completed")
    else:
        print_warning("docker run failed")

    return DockerResult(
        container_name=container_name,
        container_id=container_id,
        run_id=run_id,
        run_dir=run_dir,
        output_agent_eval_dir=output_agent_eval_dir,
        internals_dir=run_dir,
        report_path=report_path,
        exit_code=exit_code,
        metadata_path=metadata_path,
        cost_usd=cost_usd,
    )


def resolve_spec(spec: DockerArgs) -> DockerArgs:
    workspace_dir = spec.workspace_dir.resolve()
    eval_spec = spec.eval_spec.resolved()

    internals_dir = spec.internals_dir
    if internals_dir is None:
        internals_dir = workspace_dir / "_agent_internals"
    internals_dir = internals_dir.resolve()

    artifact_id = spec.artifact_id
    if not artifact_id:
        artifact_id = workspace_dir.name

    return DockerArgs(
        artifact_id=artifact_id,
        workspace_dir=workspace_dir,
        eval_spec=eval_spec,
        image_ref=spec.image_ref,
        stage=spec.stage,
        internals_dir=internals_dir,
        docker_bin=spec.docker_bin,
        overwrite_host_output=spec.overwrite_host_output,
        pull_image=spec.pull_image,
    )


def read_cost_from_report(report_path: Path, stage: str) -> float | None:
    payload = read_json_if_exists(report_path)
    if not isinstance(payload, dict):
        return None

    if stage in {"extract", "generate"}:
        value = payload.get("cost_usd")
        if isinstance(value, (int, float)):
            return float(value)
        return None

    if stage == "pipeline":
        generate_payload = payload.get("generate")
        if isinstance(generate_payload, dict):
            value = generate_payload.get("cost_usd")
            if isinstance(value, (int, float)):
                return float(value)

        extract_payload = payload.get("extract")
        if isinstance(extract_payload, dict):
            value = extract_payload.get("cost_usd")
            if isinstance(value, (int, float)):
                return float(value)

        return None

    return None


def validate_spec(spec: DockerArgs) -> None:
    if not spec.workspace_dir.is_dir():
        raise FileNotFoundError(f"Workspace directory not found: {spec.workspace_dir}")

    if not spec.eval_spec.artifact_dir.is_dir():
        raise FileNotFoundError(f"Artifact directory not found: {spec.eval_spec.artifact_dir}")

    if spec.eval_spec.artifact_readme is not None:
        if not spec.eval_spec.artifact_readme.is_file():
            raise FileNotFoundError(
                f"Artifact README not found: {spec.eval_spec.artifact_readme}"
            )
        try:
            spec.eval_spec.artifact_readme.relative_to(spec.eval_spec.artifact_dir)
        except ValueError as exc:
            raise ValueError(
                "artifact_readme must be inside artifact_dir"
            ) from exc

    if spec.eval_spec.howto_path is None:
        raise FileNotFoundError(f"HOWTO path not found: {spec.eval_spec.howto_path}")

    if (
        spec.eval_spec.artifact_plan_path is not None
        and not spec.eval_spec.artifact_plan_path.is_file()
    ):
        raise FileNotFoundError(
            f"Artifact plan path not found: {spec.eval_spec.artifact_plan_path}"
        )

    for path in spec.eval_spec.example_oracle_dirs:
        if not path.is_dir():
            raise FileNotFoundError(f"Example oracle dir not found: {path}")

    if spec.stage not in {"pipeline", "extract", "generate", "validate"}:
        raise ValueError(f"Unsupported stage: {spec.stage}")

    if spec.stage in {"pipeline", "extract", "generate"}:
        if "ANTHROPIC_API_KEY" not in os.environ:
            raise OSError("ANTHROPIC_API_KEY is not set in the host environment.")

    if spec.stage in {"pipeline", "extract", "generate"} and spec.eval_spec.artifact_readme is None:
        raise ValueError("artifact_readme is required for this stage")

    if spec.stage in {"pipeline", "generate"} and spec.eval_spec.howto_path is None:
        raise ValueError("howto_path is required for this stage")


def ensure_image(spec: DockerArgs) -> None:
    result = subprocess.run(
        [spec.docker_bin, "image", "inspect", spec.image_ref],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        text=True,
    )
    if result.returncode == 0 and not spec.pull_image:
        return

    run_cmd([spec.docker_bin, "pull", spec.image_ref])


def make_run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.localtime())


def make_container_name(artifact_id: str, run_id: str) -> str:
    safe_artifact = re.sub(r"[^a-zA-Z0-9_.-]+", "-", artifact_id).strip("-")
    return f"ae-{safe_artifact}-{run_id}".lower()


def create_container(spec: DockerArgs, container_name: str) -> str:
    cmd = [
        spec.docker_bin,
        "create",
        "--name",
        container_name,
        "--workdir",
        _RUNNER_DIR.as_posix(),
        spec.image_ref,
        "sleep",
        "infinity",
    ]
    return run_cmd(cmd).strip()


def start_container(spec: DockerArgs, container_name: str) -> None:
    run_cmd([spec.docker_bin, "start", container_name])


def stop_container(spec: DockerArgs, container_name: str) -> None:
    subprocess.run(
        [spec.docker_bin, "stop", container_name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        text=True,
    )


def prepare_container_dirs(spec: DockerArgs, container_name: str) -> None:
    mkdirs = [
        _ARTIFACT_DIR.as_posix(),
        _GUIDANCE_DIR.as_posix(),
        (_GUIDANCE_DIR / "examples").as_posix(),
        _RUNNER_DIR.as_posix(),
        (_RUNNER_DIR / "src").as_posix(),
        _OUTPUT_DIR.as_posix(),
    ]

    for path in spec.eval_spec.example_oracle_dirs:
        mkdirs.append(
            (
                _GUIDANCE_DIR
                / "examples"
                / path.parent.name
                / "_agent_eval"
            ).as_posix()
        )

    run_cmd(
        [
            spec.docker_bin,
            "exec",
            container_name,
            "sh",
            "-lc",
            "mkdir -p " + " ".join(shlex.quote(path) for path in mkdirs),
        ]
    )


def copy_inputs(spec: DockerArgs, container_name: str) -> None:
    root = project_root()

    docker_cp_dir(
        spec,
        spec.eval_spec.artifact_dir,
        f"{container_name}:{_ARTIFACT_DIR.as_posix()}",
    )

    if spec.eval_spec.howto_path is not None:
        if spec.eval_spec.howto_path.is_dir():
            docker_cp_dir(
                spec,
                spec.eval_spec.howto_path,
                f"{container_name}:{(_GUIDANCE_DIR / 'howtos').as_posix()}",
            )
        else:
            docker_cp_file(
                spec,
                spec.eval_spec.howto_path,
                f"{container_name}:{(_GUIDANCE_DIR / 'HOWTO.md').as_posix()}",
            )

    if spec.eval_spec.primitives_dir is not None and spec.eval_spec.primitives_dir.is_dir():
        docker_cp_dir(
            spec,
            spec.eval_spec.primitives_dir,
            f"{container_name}:{(_GUIDANCE_DIR / 'primitives').as_posix()}",
        )

    if spec.eval_spec.examples_dir is not None and spec.eval_spec.examples_dir.is_dir():
        docker_cp_dir(
            spec,
            spec.eval_spec.examples_dir,
            f"{container_name}:{(_GUIDANCE_DIR / 'examples').as_posix()}",
        )

    for path in spec.eval_spec.example_oracle_dirs:
        dest = (
            _GUIDANCE_DIR
            / "examples"
            / path.parent.name
            / "_agent_eval"
        )
        docker_cp_dir(spec, path, f"{container_name}:{dest.as_posix()}")

    docker_cp_dir(
        spec,
        root / "src",
        f"{container_name}:{(_RUNNER_DIR / 'src').as_posix()}",
    )

    docker_cp_file(
        spec,
        root / "pyproject.toml",
        f"{container_name}:{(_RUNNER_DIR / 'pyproject.toml').as_posix()}",
    )

    uv_lock = root / "uv.lock"
    if uv_lock.exists():
        docker_cp_file(
            spec,
            uv_lock,
            f"{container_name}:{(_RUNNER_DIR / 'uv.lock').as_posix()}",
        )

    if spec.eval_spec.artifact_plan_path is not None:
        docker_cp_file(
            spec,
            spec.eval_spec.artifact_plan_path,
            f"{container_name}:{(_ARTIFACT_DIR / 'artifact_plan.json').as_posix()}",
        )

    host_output_agent_eval_dir = spec.workspace_dir / "_agent_eval"
    if spec.stage == "validate" and host_output_agent_eval_dir.is_dir():
        docker_cp_dir(
            spec,
            host_output_agent_eval_dir,
            f"{container_name}:{(_ARTIFACT_DIR / '_agent_eval').as_posix()}",
        )


def run_stage_in_container(
    spec: DockerArgs,
    container_name: str,
    log_path: Path,
    report_path: Path,
) -> int:
    argv = build_container_cli_argv(spec)
    command = "export PATH=/root/.local/bin:/usr/local/bin:$PATH; uv run " + shlex.join(argv)

    docker_cmd = [
        spec.docker_bin,
        "exec",
        "-e",
        "PYTHONUNBUFFERED=1",
    ]
    if "ANTHROPIC_API_KEY" in os.environ:
        docker_cmd.extend(["-e", f"ANTHROPIC_API_KEY={os.environ['ANTHROPIC_API_KEY']}"])

    docker_cmd.extend(
        [
            "-w",
            _RUNNER_DIR.as_posix(),
            container_name,
            "sh",
            "-lc",
            command,
        ]
    )

    logger.info("running %s inside container", spec.stage)
    print_info(f"running {spec.stage} inside container")

    with log_path.open("w", encoding="utf-8") as log_file:
        proc = subprocess.Popen(
            docker_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert proc.stdout is not None

        # Kill the process after the timeout so this never hangs indefinitely.
        def _timeout_kill() -> None:
            logger.warning("docker exec timed out after %.0fs — killing", _DOCKER_EXEC_TIMEOUT_S)
            proc.kill()

        timer = threading.Timer(_DOCKER_EXEC_TIMEOUT_S, _timeout_kill)
        timer.start()
        try:
            for line in proc.stdout:
                log_file.write(line)
                log_file.flush()
                if spec.eval_spec.verbose:
                    sys.stderr.write(line)
                    sys.stderr.flush()
        finally:
            timer.cancel()

        exit_code = int(proc.wait())

    try_docker_cp_from_container(
        spec,
        f"{container_name}:{(_OUTPUT_DIR / f'{spec.stage}_report.json').as_posix()}",
        report_path,
    )

    return exit_code


def build_container_cli_argv(spec: DockerArgs) -> list[str]:
    argv: list[str] = [
        "python",
        "-u",
        "src/cli.py",
        spec.stage,
        "--artifact-dir",
        _ARTIFACT_DIR.as_posix(),
        "--artifact-id",
        spec.artifact_id,
        "--output-dir-name",
        spec.eval_spec.output_dir_name,
        "--artifact-plan-path",
        (_ARTIFACT_DIR / "artifact_plan.json").as_posix(),
        "--report-path",
        (_OUTPUT_DIR / f"{spec.stage}_report.json").as_posix(),
        "--max-turns",
        str(spec.eval_spec.max_turns),
        "--max-repairs",
        str(spec.eval_spec.max_repairs),
        "--max-budget-usd",
        str(spec.eval_spec.max_budget_usd),
        "--effort",
        str(spec.eval_spec.effort),
    ]

    if spec.eval_spec.model:
        argv.extend(["--model", spec.eval_spec.model])

    if spec.eval_spec.artifact_readme is not None:
        rel_readme = spec.eval_spec.artifact_readme.relative_to(spec.eval_spec.artifact_dir)
        argv.extend(
            [
                "--artifact-readme",
                (_ARTIFACT_DIR / rel_readme).as_posix(),
            ]
        )

    if spec.eval_spec.howto_path is not None:
        if spec.eval_spec.howto_path.is_dir():
            container_howto = _GUIDANCE_DIR / "howtos"
        else:
            container_howto = _GUIDANCE_DIR / "HOWTO.md"
        argv.extend(["--howto-path", container_howto.as_posix()])

    if spec.eval_spec.primitives_dir is not None:
        argv.extend(["--oracle-primitives-dir", (_GUIDANCE_DIR / "primitives").as_posix()])

    if spec.eval_spec.examples_dir is not None:
        argv.extend(["--oracle-examples-dir", (_GUIDANCE_DIR / "examples").as_posix()])

    for path in spec.eval_spec.example_oracle_dirs:
        argv.extend(
            [
                "--example-oracle-dir",
                (
                    _GUIDANCE_DIR
                    / "examples"
                    / path.parent.name
                    / "_agent_eval"
                ).as_posix(),
            ]
        )

    if spec.eval_spec.verbose:
        argv.append("--verbose")

    return argv


def export_outputs(
    spec: DockerArgs,
    container_name: str,
    run_dir: Path,
    output_agent_eval_dir: Path,
) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)

    full_agent_eval_dir = run_dir / "_agent_eval_full"
    artifact_plan_path = run_dir / "artifact_plan.json"
    report_path = run_dir / f"{spec.stage}_report.json"
    claude_dir = run_dir / ".claude"

    if full_agent_eval_dir.exists():
        shutil.rmtree(full_agent_eval_dir)
    if claude_dir.exists():
        shutil.rmtree(claude_dir)

    try_docker_cp_from_container(
        spec,
        f"{container_name}:{(_ARTIFACT_DIR / '_agent_eval').as_posix()}",
        full_agent_eval_dir,
    )
    try_docker_cp_from_container(
        spec,
        f"{container_name}:{(_ARTIFACT_DIR / 'artifact_plan.json').as_posix()}",
        artifact_plan_path,
    )
    try_docker_cp_from_container(
        spec,
        f"{container_name}:{(_ARTIFACT_DIR / '.claude').as_posix()}",
        claude_dir,
    )
    try_docker_cp_from_container(
        spec,
        f"{container_name}:{(_OUTPUT_DIR / f'{spec.stage}_report.json').as_posix()}",
        report_path,
    )

    oracle_plan_src = full_agent_eval_dir / "oracle_plan.json"
    oracle_plan_dest = run_dir / "oracle_plan.json"
    if oracle_plan_src.is_file():
        shutil.copy2(oracle_plan_src, oracle_plan_dest)

    if full_agent_eval_dir.is_dir():
        copy_oracle_files(
            full_agent_eval_dir=full_agent_eval_dir,
            output_agent_eval_dir=output_agent_eval_dir,
        )


def copy_oracle_files(
    full_agent_eval_dir: Path,
    output_agent_eval_dir: Path,
) -> None:
    required_files = (
        "env_setup.py",
        "artifact_build.py",
        "benchmark_prep.py",
        "experiment_runs.py",
        "common.py",
    )

    if output_agent_eval_dir.exists():
        shutil.rmtree(output_agent_eval_dir)
    output_agent_eval_dir.mkdir(parents=True, exist_ok=True)

    for name in required_files:
        src = full_agent_eval_dir / name
        if src.is_file():
            shutil.copy2(src, output_agent_eval_dir / name)

    refs_src = full_agent_eval_dir / "refs"
    refs_dest = output_agent_eval_dir / "refs"
    if refs_src.is_dir():
        shutil.copytree(refs_src, refs_dest)


def default_example_oracle_dirs(root: Path) -> tuple[Path, ...]:
    examples_root = root / "examples"
    if not examples_root.is_dir():
        return ()

    paths: list[Path] = []
    for artifact_dir in examples_root.glob("*"):
        candidate = artifact_dir / "_agent_eval"
        if candidate.is_dir():
            paths.append(candidate)

    return tuple(sorted(paths))


def docker_cp_file(spec: DockerArgs, src: Path, dest: str) -> None:
    run_cmd([spec.docker_bin, "cp", src.as_posix(), dest])


def docker_cp_dir(spec: DockerArgs, src: Path, dest: str) -> None:
    run_cmd([spec.docker_bin, "cp", src.as_posix() + "/.", dest])


def try_docker_cp_from_container(
    spec: DockerArgs,
    src: str,
    dest: Path,
) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [spec.docker_bin, "cp", src, dest.as_posix()],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        text=True,
    )
    return result.returncode == 0


def run_cmd(cmd: list[str]) -> str:
    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Command failed ({result.returncode}): {' '.join(cmd)}\n{result.stderr.strip()}"
        )
    return result.stdout.strip()


def parse_args(argv: list[str]) -> argparse.Namespace:
    root = project_root()

    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace-dir", required=True)
    parser.add_argument("--artifact-id", required=True)
    parser.add_argument("--artifact-dir", required=True)
    parser.add_argument("--artifact-readme")
    parser.add_argument("--artifact-plan-path")
    parser.add_argument("--howto-path")
    parser.add_argument("--example-oracle-dir", action="append", default=[])
    parser.add_argument("--internals-dir")
    parser.add_argument(
        "--stage",
        choices=("pipeline", "extract", "generate", "validate"),
        default="pipeline",
    )
    parser.add_argument(
        "--image-ref",
        default="bastoica/ae-agent-ubuntu24.04:latest",
    )
    parser.add_argument("--docker-bin", default="docker")
    parser.add_argument("--model")
    parser.add_argument(
        "--effort",
        choices=("low", "medium", "high", "max"),
    )
    parser.add_argument("--max-turns", type=int)
    parser.add_argument("--max-repairs", type=int)
    parser.add_argument("--max-budget-usd", type=float)
    parser.add_argument("--overwrite-host-output", action="store_true")
    parser.add_argument("--pull-image", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def spec_from_args(args: argparse.Namespace) -> DockerArgs:
    root = project_root()
    guidance_root = load_guidance_root(root)

    workspace_dir = Path(args.workspace_dir)
    artifact_dir = Path(args.artifact_dir)

    artifact_readme = None
    if args.artifact_readme:
        artifact_readme = Path(args.artifact_readme)
    elif (artifact_dir / "README.md").is_file():
        artifact_readme = artifact_dir / "README.md"

    artifact_plan_path = None
    if args.artifact_plan_path:
        artifact_plan_path = Path(args.artifact_plan_path)

    howto_path: Path | None = None
    if args.howto_path:
        howto_path = Path(args.howto_path)
    elif guidance_root is not None and (guidance_root / "howtos").is_dir():
        howto_path = guidance_root / "howtos"

    primitives_dir: Path | None = None
    if guidance_root is not None and (guidance_root / "primitives").is_dir():
        primitives_dir = guidance_root / "primitives"

    examples_dir: Path | None = None
    if guidance_root is not None and (guidance_root / "examples").is_dir():
        examples_dir = guidance_root / "examples"

    example_dirs: tuple[Path, ...]
    if args.example_oracle_dir:
        example_dirs = tuple(Path(path) for path in args.example_oracle_dir)
    else:
        example_dirs = default_example_oracle_dirs(root)

    eval_spec = EvalSpec(
        artifact_id=args.artifact_id,
        artifact_dir=artifact_dir,
        artifact_readme=artifact_readme,
        howto_path=howto_path,
        artifact_plan_path=artifact_plan_path,
        example_oracle_dirs=example_dirs,
        primitives_dir=primitives_dir,
        examples_dir=examples_dir,
        output_dir_name="_agent_eval",
        max_turns=args.max_turns or int(os.environ.get("ARTIFACT_EVAL_MAX_TURNS", "80")),
        max_repairs=args.max_repairs or int(os.environ.get("ARTIFACT_EVAL_MAX_REPAIRS", "2")),
        max_budget_usd=(
            args.max_budget_usd
            if args.max_budget_usd is not None
            else float(os.environ.get("ARTIFACT_EVAL_MAX_BUDGET_USD", "1.0"))
        ),
        model=args.model or os.environ.get("ANTHROPIC_MODEL"),
        effort=args.effort or os.environ.get("ARTIFACT_EVAL_EFFORT", "medium"),
        verbose=args.verbose,
    )

    artifact_id = args.artifact_id or workspace_dir.name

    internals_dir = None
    if args.internals_dir:
        internals_dir = Path(args.internals_dir)

    return DockerArgs(
        artifact_id=artifact_id,
        workspace_dir=workspace_dir,
        eval_spec=eval_spec,
        image_ref=args.image_ref,
        stage=args.stage,
        internals_dir=internals_dir,
        docker_bin=args.docker_bin,
        overwrite_host_output=args.overwrite_host_output,
        pull_image=args.pull_image,
    )


def main(argv: list[str] | None = None) -> int:
    root = project_root()
    load_env_file(root / ".env")

    args = parse_args(argv if argv is not None else sys.argv[1:])
    log_file = resolve_log_file(root, os.environ.get("ARTIFACT_EVAL_DEBUG_LOG"))
    configure_logging(verbose=args.verbose, log_file=log_file)

    result = run(spec_from_args(args))
    print(json.dumps(asdict(result), indent=2, sort_keys=True, default=str), flush=True)
    return 0 if result.exit_code == 0 else result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())