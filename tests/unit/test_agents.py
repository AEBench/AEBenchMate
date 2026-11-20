"""Exercise adapter selection, the original Claude SDK path, and another agent."""

import asyncio
import json

import pytest

import agents
import cli
from utils import OracleGenConfig


@pytest.fixture
def custom_agents(tmp_path, monkeypatch):
    monkeypatch.setattr(agents, "AGENTS_DIR", tmp_path / "agents")
    agents.load_agent.cache_clear()
    yield agents.AGENTS_DIR
    agents.load_agent.cache_clear()


@pytest.mark.parametrize("name", ["../claude", "/tmp/claude", "claude/other", ""])
def test_agent_name_cannot_escape_directory(name):
    with pytest.raises(ValueError, match="Invalid agent name"):
        agents.load_agent(name)


def test_unknown_or_missing_entry_point(custom_agents):
    with pytest.raises(ValueError, match="Unknown agent"):
        agents.load_agent("missing")
    folder = custom_agents / "broken"
    folder.mkdir(parents=True)
    (folder / "agent.py").write_text("ENV_VARS = ()\n")
    with pytest.raises(ValueError, match="must define async run"):
        agents.load_agent("broken")


@pytest.mark.parametrize("stage", ["extract", "generate", "pipeline", "validate"])
def test_agent_option_is_shared_and_defaults_to_claude(stage, tmp_path):
    args = [stage, "--artifact-dir", str(tmp_path), "--artifact-readme", "README.md"]
    assert cli._make_spec(cli.parse_args(args)).agent == "claude"
    assert cli._make_spec(cli.parse_args(args + ["--agent", "other"])).agent == "other"


@pytest.mark.parametrize("stage", ["extract", "generate"])
def test_claude_keeps_sdk_options_and_permissions(stage, tmp_path, monkeypatch):
    adapter = agents.load_agent("claude")
    readme = tmp_path / "README.md"
    readme.write_text("# Artifact")
    howto = tmp_path / "howto"
    howto.mkdir()
    spec = OracleGenConfig("fixture", tmp_path, readme, howto, model="test-model")
    observed = {}

    class Client:
        def __init__(self, *, options):
            observed["options"] = options

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            pass

        async def query(self, prompt):
            observed["prompt"] = prompt

    async def response(client, *, on_line):
        on_line("progress")
        return "response", 0.1, {"input_tokens": 5}

    monkeypatch.setattr(adapter, "ClaudeSDKClient", Client)
    monkeypatch.setattr(adapter, "read_response", response)
    progress = []
    result = asyncio.run(
        agents.run_agent(spec, "task", stage=stage, on_line=progress.append)
    )
    assert result == ("response", 0.1, {"input_tokens": 5})
    assert observed["prompt"] == "task" and progress == ["progress"]
    options = observed["options"]
    assert options.model == "test-model" and options.max_turns == spec.max_turns
    if stage == "extract":
        assert "Write" in options.disallowed_tools
        assert not (tmp_path / ".claude").exists()
    else:
        assert "Write" in options.allowed_tools
        assert options.can_use_tool is not None
        settings = json.loads((tmp_path / ".claude/settings.json").read_text())
        assert settings["sandbox"]["enabled"]
        decision = asyncio.run(
            options.can_use_tool("Write", {"file_path": str(readme)}, None)
        )
        assert "limited" in decision.message


def test_new_adapter_runs_existing_readme_to_oracle_pipeline(
    custom_agents, tmp_path, monkeypatch
):
    folder = custom_agents / "fixture"
    folder.mkdir(parents=True)
    (folder / "agent.py").write_text("""
import json
ENV_VARS = ()
calls = []

async def run(spec, prompt, *, stage, on_line):
    calls.append(stage)
    on_line("fixture progress")
    if stage == "extract":
        assert "Fixture README" in prompt
        return json.dumps({"artifact_name": "fixture", "stages": {}}), 0.1, None
    assert str(spec.artifact_plan_path) in prompt
    assert json.loads(spec.artifact_plan_path.read_text())["artifact_name"] == "fixture"
    spec.output_dir.mkdir(parents=True)
    for name, cls in {"env_setup": "OracleEnvSetup", "artifact_build": "OracleArtifactBuild",
                      "benchmark_prep": "OracleBenchmarkPrep", "experiment_runs": "OracleExperimentRuns"}.items():
        (spec.output_dir / (name + ".py")).write_text("class " + cls + ":\\n    pass\\n")
    (spec.output_dir / "common.py").write_text("# fixture\\n")
    return "Generated", 0.2, None
""")
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    readme = artifact / "README.md"
    readme.write_text("# Fixture README")
    howto = tmp_path / "howto"
    howto.mkdir()
    report = tmp_path / "report.json"
    monkeypatch.setenv("ARTIFACT_EVAL_DEBUG_LOG", str(tmp_path / "debug.log"))
    assert (
        cli.main(
            [
                "pipeline",
                "--agent",
                "fixture",
                "--artifact-dir",
                str(artifact),
                "--artifact-readme",
                str(readme),
                "--howto-path",
                str(howto),
                "--max-repairs",
                "0",
                "--report-path",
                str(report),
            ]
        )
        == 0
    )
    assert json.loads(report.read_text())["ok"]
    assert agents.load_agent("fixture").calls == ["extract", "generate"]
    assert (artifact / "artifact_plan.json").is_file()
    assert (artifact / "oracles/env_setup.py").is_file()
    assert not (artifact / ".claude").exists()
