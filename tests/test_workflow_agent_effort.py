"""Run the agent templates with a stub CLI to verify optional effort arguments."""

import subprocess
from pathlib import Path

import pytest
import yaml


@pytest.mark.parametrize("workflow_dir", ["workflows", ".agentis/workflows"])
@pytest.mark.parametrize("effort", [None, "", "low", "high", "value with spaces"])
def test_run_agent_optional_effort(tmp_path: Path, workflow_dir: str, effort: str | None) -> None:
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / workflow_dir / "_base.yaml").read_text(encoding="utf-8"))
    script = workflow["workflow"]["stepTemplates"]["run-agent"]["run"]
    env = {
        "PATH": "/usr/bin:/bin",
        "AGENTIS_RUN_DIR": str(tmp_path),
        "AGENTIS_PROMPT_FILE": "/dev/null",
        "AGENTIS_RUN_ID": "run-id",
        "AGENTIS_TASK_ID": "task-id",
        "AGENTIS_PROJECT_ID": "project-id",
    }
    if effort is not None:
        env["AGENTIS_EFFORT"] = effort
    result = subprocess.run(
        ["bash", "-c", 'set -euo pipefail\nagentiscode() { printf "%s\\0" "$@"; }\n' + script],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    args = result.stdout.rstrip("\0").split("\0")
    assert "--model" in args
    if effort:
        assert args[args.index("--effort") + 1] == effort
    else:
        assert "--effort" not in args
        assert "" not in args
