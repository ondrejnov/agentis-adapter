"""Testy mini workflow `.agentis/workflows/local-env.yaml` pro lokální spawn agent CLI."""

from __future__ import annotations

import subprocess
from pathlib import Path

from common.workflow.local_env import LOCAL_ENV_WORKFLOW_RELPATH, build_local_env_shell_command


def write_workflow(base: Path, content: str) -> Path:
    path = base / LOCAL_ENV_WORKFLOW_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_missing_workflow_means_plain_exec(tmp_path) -> None:
    command = build_local_env_shell_command(["opencode", "run", "Do X"], cwd=str(tmp_path))

    assert command == "exec opencode run 'Do X'"


def test_workflow_env_steps_and_exec_compose_script(tmp_path) -> None:
    write_workflow(
        tmp_path,
        "version: 1\n"
        "workflow:\n"
        "  envFiles:\n"
        "    - /etc/agentis/agent.env\n"
        "  env:\n"
        '    PATH: "[%WORKDIR%]/.venv/bin:[%MAIN_DIR%]/.venv/bin:$PATH"\n'
        "  steps:\n"
        "    - name: First\n"
        "      run: prepare-venv\n"
        "    - name: Second\n"
        "      run: build-graph\n",
    )

    command = build_local_env_shell_command(["claude", "--print"], cwd=str(tmp_path))

    lines = command.splitlines()
    assert lines[0] == "set -euo pipefail"
    assert f"export WORKDIR={tmp_path}" in lines
    assert f"export MAIN_DIR={tmp_path}" in lines
    assert ". /etc/agentis/agent.env" in lines
    assert f'export PATH="{tmp_path}/.venv/bin:{tmp_path}/.venv/bin:$PATH"' in lines
    assert "(\nprepare-venv\n)" in command
    assert command.index("prepare-venv") < command.index("build-graph")
    assert lines[-1] == "exec claude --print"


def test_invalid_workflow_falls_back_to_plain_exec(tmp_path, capsys) -> None:
    write_workflow(tmp_path, "version: 1\nworkflow:\n  steps: []\n")

    command = build_local_env_shell_command(["claude"], cwd=str(tmp_path))

    assert command == "exec claude"
    assert "[local-env] invalid workflow" in capsys.readouterr().err


def test_ignored_fields_and_invalid_env_keys_warn(tmp_path, capsys) -> None:
    write_workflow(
        tmp_path,
        "version: 1\n"
        "workflow:\n"
        "  image: registry/image:tag\n"
        "  env:\n"
        '    "BAD-KEY": hodnota\n'
        "  steps:\n"
        "    - name: Step\n"
        "      run: prepare-venv\n"
        "      if: ENV_READY != 'true'\n",
    )

    command = build_local_env_shell_command(["claude"], cwd=str(tmp_path))

    err = capsys.readouterr().err
    assert "ignoruje pole: image, steps[].if" in err
    assert "přeskočen env klíč 'BAD-KEY'" in err
    assert "BAD-KEY" not in command
    assert "(\nprepare-venv\n)" in command


def test_step_exit_zero_does_not_skip_agent_exec(tmp_path) -> None:
    write_workflow(
        tmp_path,
        "version: 1\n"
        "workflow:\n"
        "  env:\n"
        "    GREETING: ahoj\n"
        "  steps:\n"
        "    - name: Early exit\n"
        "      run: |\n"
        "        exit 0\n"
        "        echo unreachable\n",
    )

    command = build_local_env_shell_command(["printenv", "GREETING"], cwd=str(tmp_path))
    result = subprocess.run(["bash", "-c", command], capture_output=True, text=True)

    assert result.returncode == 0
    assert result.stdout.strip() == "ahoj"


def test_failed_step_prevents_agent_exec(tmp_path) -> None:
    write_workflow(
        tmp_path,
        "version: 1\nworkflow:\n  steps:\n    - name: Broken\n      run: exit 3\n",
    )

    command = build_local_env_shell_command(["echo", "agent-started"], cwd=str(tmp_path))
    result = subprocess.run(["bash", "-c", command], capture_output=True, text=True)

    assert result.returncode == 3
    assert "agent-started" not in result.stdout
