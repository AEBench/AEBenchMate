## 1. Overview

AEBenchMate is an AI agent that helps add new research artifacts to [AEBench](https://github.com/bastoica/aebench), a benchmark for evaluating AI agents on artifact evaluation tasks.
It first reads an artifact's README, identifies the required setup, execution, and evaluation steps, and then generates an evaluation bundle with oracle scripts for the different stages of artifact evaluation. 
Specifically, the result is a code and ground-truth bundle that AEBench can use to score agents for this particular artifact.


## 2. Getting started

To get started, clone this repository, install dependencies, and configure an API key by following these steps:

1. Clone the repository and install dependencies:
```bash
git clone https://github.com/bastoica/AEBenchMate
cd AEBenchMate
uv sync
```

2. Set up the `ANTHROPIC_API_KEY` environment variable:
```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

Alternatively, create a `.env` file at the repo root. The CLI loads it automatically on startup.

> [!NOTE]
> AEBenchMate requires the following:
> * Python >= 3.10
> * [uv](https://docs.astral.sh/uv/)
> * Docker (for containerized runs only)
> * An Anthropic API key (for the extractor and generator stages)

## 3. Running the pipeline

AEBenchMate has three stages: **extract** (README to structured plan), **generate** (plan to oracle code), and **validate** (check that the generated package is well-formed). Each stage can run independently or as part of the full pipeline.

To run the full pipeline:
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

The pipeline runs extract, validate plan, generate, validate oracle, with automatic retries (controlled by `--max-repairs`) on validation failures. Each retry writes a feedback file so the next attempt knows what went wrong.

To run individual stages:
```bash
# Extract only: README to artifact_plan.json
uv run python -u src/cli.py extract \
  --artifact-dir /path/to/artifact \
  --artifact-readme /path/to/artifact/README.md \
  --model claude-sonnet-4-5

# Generate only: plan + guidance to oracles/
uv run python -u src/cli.py generate \
  --artifact-dir /path/to/artifact \
  --artifact-readme /path/to/artifact/README.md \
  --howto-path /path/to/guidance/howtos \
  --artifact-plan-path /path/to/artifact/artifact_plan.json \
  --model claude-sonnet-4-5

# Validate only: check plan and/or oracle package
uv run python -u src/cli.py validate \
  --artifact-dir /path/to/artifact \
  --artifact-plan-path /path/to/artifact/artifact_plan.json \
  --target all
```

## 4. Running in Docker

`run_docker.py` creates a container, copies inputs in, runs the CLI inside it, and copies outputs back:

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

## 5. Expected outputs

For a workspace containing an artifact directory, a normal pipeline run produces:

```plaintext
workspace/asplos25_golf/
├── golftool/                  # original artifact
├── artifact_plan.json         # structured plan (extractor output)
├── oracles/                   # clean deliverable (generator output)
│   ├── env_setup.py
│   ├── artifact_build.py
│   ├── benchmark_prep.py
│   ├── experiment_runs.py
│   ├── common.py
│   └── refs/
└── _agent_internals/          # debugging artifacts
    └── <run_id>/
        ├── metadata.json
        ├── docker_exec.log
        ├── pipeline_report.json
        ├── oracle_plan.json
        ├── .claude/
        └── oracles_full/
```

## 6. Building a case bundle

After a successful pipeline run, the `--case-dir` flag assembles an AEBench-ready case directory:

```bash
uv run python -u src/cli.py pipeline \
  --artifact-dir /path/to/artifact \
  --artifact-readme /path/to/artifact/README.md \
  --guidance-dir /path/to/guidance \
  --upstream-url https://github.com/author/repo.git \
  --upstream-ref abc123def456 \
  --case-dir /path/to/cases/venue24_paper
```

This copies the oracle modules into `oracles/`, the ref files into `refs/`, and generates a `case.toml` manifest:

```plaintext
cases/venue24_paper/
├── case.toml
├── oracles/
│   ├── env_setup.py
│   ├── artifact_build.py
│   ├── benchmark_prep.py
│   ├── experiment_runs.py
│   └── common.py
└── refs/
    └── *.json
```

After generation, review `case.toml` for TODO placeholders, add `__init__.py` files, and register the case in `cases.json`.

## 7. Running the tests

To run the test suite:

```bash
uv sync --dev
uv run python -m pytest
```

If successful, users should see output similar to:
```
152 passed in ~0.7s
```

To run a specific test or test directory:
```bash
uv run python -m pytest tests/unit/
uv run python -m pytest tests/unit/test_policies.py::test_bash_dangerous_denied
uv run python -m pytest -k "plan"
```

> [!NOTE]
> Tests that import `claude_agent_sdk` (policies, extractor, prompt building) are auto-skipped if the package is not installed. No tests make network calls or require an API key.

## 8. Troubleshooting

When debugging a failed run, check `_agent_internals/<run_id>/` first. It contains `docker_exec.log` (full stdout/stderr), `pipeline_report.json` (structured status and cost), `artifact_plan.json` (extractor output), `oracle_plan.json` (generator reasoning), `oracles_full/` (raw output before cleanup), and `.claude/` (SDK state).

> [!NOTE]
> Budget is enforced using the Claude SDK's reported session cost. If cost reporting is delayed or inaccurate, the pipeline may slightly exceed `--max-budget-usd`. The `--max-turns` flag provides a hard cap on conversational turns as a secondary safeguard.

## 9. Known issues

### 9.1 Incomplete or misleading READMEs

The extractor does not run any commands or fetch from the network. It relies entirely on the README text, which means artifacts with incomplete or misleading READMEs may produce poor plans.

### 9.2 No runtime verification of generated oracles

The generator writes oracle code but does not execute it. Validation checks syntax, structure, and required class names, but cannot verify runtime correctness without actually running the oracles against a real workspace.

### 9.3 Read-only artifact directories in Docker

Retry feedback is written to the artifact directory by default. In AEBench-integrated Docker runs where the artifact checkout is mounted read-only, retries may fail. Use `--case-dir` to route outputs to a writable location.


## 10. Selecting and adding agents

Claude remains the default and uses the same Claude Agent SDK and policies as
before. Select an adapter with `--agent NAME` on `extract`, `generate`, or
`pipeline`; Docker's wrapper accepts the same option. Existing commands need
no changes.

The directory-per-agent idea comes from AEBench PR 155. Mate uses a small Python
entry point instead of importing AEBench's runtime or replacing its Claude SDK:

```text
agents/claude/agent.py
agents/<new-agent>/agent.py
```

Each adapter defines the host environment variables it needs in `ENV_VARS`
(use `()` when none are required), and this async entry point:

```python
ENV_VARS = ("MY_AGENT_API_KEY",)

async def run(spec, prompt, *, stage, on_line):
    # Invoke your agent's SDK or CLI with the supplied task and settings.
    # Return (response_text, cost_usd_or_None, usage_dict_or_None).
    raise NotImplementedError("Implement the provider-specific call here")
```

`stage` is `"extract"` or `"generate"`. Extraction returns the requested artifact
plan JSON as response text. Generation writes the oracle files to
`spec.output_dir` and returns a summary. `spec` also supplies the model, paths,
turn limit, budget, and effort; `on_line(text)` displays optional progress.
Raise an exception if agent execution fails. The adapter implements its agent's
authentication, tools, and stage restrictions: extraction uses only the supplied
README, and generation must not modify artifact source files. Claude's adapter
reuses the existing `policies.py` settings and permission handler. Other adapters
must configure equivalent behavior through their own SDK or CLI; prompts alone
are not permission enforcement.

For local runs, configure credentials in the host environment or Mate's `.env`
as before. Docker forwards the selected adapter's declared environment variables
by name and copies the `agents/` directory into the container. Install any new
agent's required packages or executable in the environment/image where it runs.
No registry entry, manifest, or new framework dependency is required.
