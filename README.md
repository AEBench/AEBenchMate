# AEBenchMate

AEBenchMate is a small agent pipeline for adding new artifacts to [AEBench](https://github.com/bastoica/aebench). Given a research artifact and its README, it generates a standalone `_agent_eval/` oracle package that AEBench can later use to evaluate AI agents on artifact evaluation tasks.

## 1. Overview

The pipeline has three stages:

1. **Extractor** — reads the artifact README and produces a structured `artifact_plan.json` describing what needs to be checked (dependencies, build steps, datasets, expected outputs, etc.)

2. **Generator** — takes the artifact, the plan, a HOWTO guide, and a few example oracle directories, then generates the `_agent_eval/` package containing four oracle modules and a `common.py`

3. **Validator** — checks that the generated package is well-formed: files exist, Python compiles, required classes present, no forbidden calls, referenced ref files exist, plan coverage is adequate

Each stage can run independently or as part of the full pipeline. The extractor and generator call Claude via the SDK; the validator is deterministic and fast.

## 2. Getting started

### Prerequisites

- Python 3.10+
- [uv](https://docs.astral.sh/uv/)
- Docker (for containerized runs)
- `ANTHROPIC_API_KEY` in the environment (for extractor and generator)

### Setup

```bash
cd AEBenchMate
uv sync
export ANTHROPIC_API_KEY=sk-ant-...
```

Users can also put environment variables in a `.env` file at the repo root. The CLI loads it automatically on startup.

## 3. Repository structure

```plaintext
src/
├── cli.py            # unified CLI: extract, generate, validate, pipeline
├── extractor.py      # stage 1: README → artifact_plan.json
├── generator.py      # stage 2: plan + guidance → _agent_eval/
├── validator.py      # stage 3: validates plan and oracle package
├── policies.py       # tool/path restrictions for the Claude agent
├── run_docker.py     # thin Docker wrapper (create, copy in, run, copy out)
├── case_builder.py   # assembles an AEBench case bundle from _agent_eval/
├── utils.py          # EvalSpec, JSON/text helpers, budget tracking, logging
└── ui.py             # Rich console output (banners, colored messages)

tests/
├── unit/             # pure logic, no network or Docker
│   ├── test_utils.py
│   ├── test_utils_io.py
│   ├── test_validator.py
│   ├── test_case_builder.py
│   ├── test_policies.py
│   └── test_docker_helpers.py
└── functional/       # real filesystem, imported modules, no API calls
    ├── test_extractor.py
    ├── test_cli_args.py
    ├── test_plan_loading.py
    ├── test_validator_deep.py
    └── test_case_bundle.py
```

### Configuration

`EvalSpec` (in `utils.py`) is the shared config object passed to all stages. It carries the artifact directory, README path, plan path, guidance paths, model/effort settings, budget, max turns, max repairs, and verbosity.

When running locally, the CLI resolves guidance paths from three sources (in priority order):
1. Explicit CLI flags (`--howto-path`, `--oracle-primitives-dir`, `--oracle-examples-dir`)
2. `--guidance-dir` (auto-discovers `howtos/`, `primitives/`, `examples/` subdirectories)
3. `benchconductor.toml` `[guidance]` section (if present)

## 4. Running locally

The unified CLI supports running stages individually or as a full pipeline.

### Extract only

```bash
uv run python -u src/cli.py extract \
  --artifact-dir /path/to/artifact \
  --artifact-readme /path/to/artifact/README.md \
  --report-path /tmp/extract-report.json \
  --model claude-sonnet-4-5 \
  --max-budget-usd 1.0 \
  --verbose
```

This reads the README, queries Claude to produce a structured checklist, and writes `artifact_plan.json` next to the artifact (or under `--case-dir` if set).

### Generate only

```bash
uv run python -u src/cli.py generate \
  --artifact-dir /path/to/artifact \
  --artifact-readme /path/to/artifact/README.md \
  --howto-path /path/to/guidance/howtos \
  --oracle-primitives-dir /path/to/guidance/primitives \
  --oracle-examples-dir /path/to/guidance/examples \
  --artifact-plan-path /path/to/artifact/artifact_plan.json \
  --report-path /tmp/generate-report.json \
  --model claude-sonnet-4-5 \
  --max-budget-usd 1.0 \
  --verbose
```

### Validate only

```bash
uv run python -u src/cli.py validate \
  --artifact-dir /path/to/artifact \
  --artifact-plan-path /path/to/artifact/artifact_plan.json \
  --target all \
  --report-path /tmp/validate-report.json
```

The `--target` flag accepts `plan`, `oracle`, or `all`.

### Full pipeline

```bash
uv run python -u src/cli.py pipeline \
  --artifact-dir /path/to/artifact \
  --artifact-readme /path/to/artifact/README.md \
  --guidance-dir /path/to/guidance \
  --report-path /tmp/pipeline-report.json \
  --model claude-sonnet-4-5 \
  --max-budget-usd 1.0 \
  --max-repairs 2 \
  --verbose
```

The pipeline runs extract → validate plan → generate → validate oracle, with automatic retries (controlled by `--max-repairs`) on validation failures. Each retry writes a feedback file so the next attempt knows what went wrong.

## 5. Running in Docker

`run_docker.py` is a thin wrapper that creates a container, copies inputs in, runs the CLI inside it, and copies outputs back. It does not re-implement the pipeline.

```bash
uv run python -u src/run_docker.py \
  --workspace-dir /path/to/workspace \
  --artifact-dir /path/to/workspace/artifact \
  --image-ref bastoica/ae-agent-ubuntu24.04:latest \
  --stage pipeline \
  --model claude-sonnet-4-5 \
  --max-budget-usd 1.0 \
  --overwrite-host-output \
  --verbose
```

The `--stage` flag accepts `extract`, `generate`, `validate`, or `pipeline`.

## 6. Expected outputs

For a workspace like:

```plaintext
workspace/asplos25_golf/
└── golftool/          # original artifact
```

A normal pipeline run produces:

```plaintext
workspace/asplos25_golf/
├── golftool/
├── artifact_plan.json
├── _agent_eval/
│   ├── env_setup.py
│   ├── artifact_build.py
│   ├── benchmark_prep.py
│   ├── experiment_runs.py
│   ├── common.py              # shared helpers
│   └── refs/                  # reference data (optional)
└── _agent_internals/
    └── <run_id>/
        ├── metadata.json
        ├── docker_exec.log
        ├── pipeline_report.json
        ├── artifact_plan.json
        ├── oracle_plan.json
        ├── .claude/
        └── _agent_eval_full/
```

`_agent_eval/` is the clean deliverable — the four oracle modules plus optional ref files. `_agent_internals/` is for debugging and contains the raw agent output, logs, and intermediate files.

### Building a case bundle

After a successful pipeline run, users can assemble an AEBench case bundle:

```bash
uv run python -u src/cli.py pipeline \
  --artifact-dir /path/to/artifact \
  ... \
  --upstream-url https://github.com/author/repo.git \
  --upstream-ref abc123def456 \
  --case-dir /path/to/cases/venue24_paper
```

This copies the oracle files and refs into the case directory and generates `case.toml`.

## 7. Policy enforcement

The generator runs Claude with restricted permissions to prevent unintended side effects:

- The agent can only write under `_agent_eval/` and `.claude/`
- Artifact source files are read-only
- Dangerous bash commands are blocked (`rm -rf`, `curl | bash`, `sudo`, etc.)
- Secret files (`.env`, `credentials.json`) cannot be read
- Source code directories outside the workspace are blocked

These rules are defined in `policies.py` and enforced via the Claude SDK permission handler.

## 8. Running the tests

The test suite uses [pytest](https://docs.pytest.org/). Install dependencies and run:

```bash
uv sync --dev
uv run python -m pytest
```

Expected output:
```
169 passed in ~0.7s
```

### Unit tests

Pure logic with filesystem via `tmp_path`. No network, no Docker, no API keys.

```bash
uv run python -m pytest tests/unit/
```

| File | What it covers |
|---|---|
| `test_utils.py` | `EvalSpec` output_dir, resolved paths, budget tracking, `strip_code_fences` |
| `test_utils_io.py` | `load_guidance_root`, `load_env_file`, `resolve_log_file`, JSON/text helpers, `EvalSpec.resolved()` auto-discovery |
| `test_validator.py` | `validate()`: valid package, missing files, compile errors, forbidden calls, hardcoded paths. `validate_artifact_plan()`: valid plan, bad JSON, missing stages, duplicate IDs. `create_missing_refs()` |
| `test_case_builder.py` | `write_case_toml()`: TOML fields, escaping. `build_case_bundle()`: file copy, refs, optional common.py |
| `test_policies.py` | Permission checks: bash filtering (rm -rf, curl, sudo), write/read path validation, source code and secret file blocking |
| `test_docker_helpers.py` | Container naming, run ID format, cost extraction from reports, spec resolution and validation |

### Functional tests

Import real modules and exercise component-level behavior. Extractor tests need `claude_agent_sdk` importable (auto-skipped otherwise).

```bash
uv run python -m pytest tests/functional/
```

| File | What it covers |
|---|---|
| `test_extractor.py` | Plan JSON parsing, item normalization, ID assignment, prompt building |
| `test_cli_args.py` | Argument parsing for all subcommands, spec construction, guidance path resolution |
| `test_plan_loading.py` | Artifact plan loading, stage item extraction, plan summarization |
| `test_validator_deep.py` | Realistic oracle packages with forbidden calls, hardcoded paths, ref extraction |
| `test_case_bundle.py` | TOML validity (parseable by tomllib), bundle assembly with refs, escaping |

### What is not tested

- **Claude SDK calls** (`extract_artifact_plan`, `generate`): needs an API key and costs money
- **Docker container lifecycle** (`run_docker.py`): needs a Docker daemon. Spec validation and helpers are tested; the subprocess plumbing isnt
- **Console output** (`ui.py`): display-only, no testable logic

### Running a single test

```bash
uv run python -m pytest tests/unit/test_policies.py
uv run python -m pytest tests/unit/test_policies.py::test_bash_rm_rf_denied
uv run python -m pytest -k "plan"
```

## 9. Troubleshooting

Check `_agent_internals/<run_id>/` first. It contains:

- `docker_exec.log` — full stdout/stderr from the container
- `pipeline_report.json` — structured report with status and cost
- `artifact_plan.json` — what the extractor produced
- `oracle_plan.json` — the generators reasoning about oracle structure
- `_agent_eval_full/` — raw agent output before cleanup
- `.claude/` — Claude SDK state (settings, CLAUDE.md)

> [!NOTE]
> Budget is enforced using the Claude SDK's reported session cost. If cost reporting is delayed or inaccurate, the pipeline may slightly exceed `--max-budget-usd`. The `--max-turns` flag provides a hard cap on conversational turns as a secondary safeguard.

## 10. Known limitations

- The extractor does not run any commands or fetch from the network. It relies entirely on the README text, which means artifacts with incomplete or misleading READMEs may produce poor plans.
- The generator writes oracle code but does not execute it. Validation checks syntax and structure but cant verify runtime correctness without actually running the oracles against a real workspace.
- Retry feedback is written to the artifact directory by default. In AEBench-integrated Docker runs where the artifact checkout is mounted read-only, retries may fail. Use `--case-dir` to route outputs to a writable location.
