from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from common.workflow.runtime import build_bash_wrapper
from common.workflow.schema import load_workflow_file

#: Falešné `agentiscode`: uloží argv a stdin, ať test vidí, co by agent dostal.
FAKE_AGENTISCODE = """#!/usr/bin/env python3
import json, os, sys
with open(os.environ["FAKE_AGENTISCODE_LOG"], "w", encoding="utf-8") as fh:
    json.dump({"argv": sys.argv[1:], "stdin": sys.stdin.read()}, fh)
"""


def _load(tmp_path: Path, steps_yaml: str, *, task_title: str = "Task"):
    path = tmp_path / "wf.yaml"
    path.write_text(
        "version: 1\nworkflow:\n  executor: local\n  env:\n    TASK_TITLE: \"[%TASK_TITLE%]\"\n  steps:\n" + steps_yaml,
        encoding="utf-8",
    )
    return load_workflow_file(path, {"TASK_TITLE": task_title, "WORKDIR": str(tmp_path)})


def _run_step(tmp_path: Path, script: str, env: dict[str, str]) -> dict:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "agentiscode"
    fake.write_text(FAKE_AGENTISCODE, encoding="utf-8")
    fake.chmod(0o755)
    log = tmp_path / "agentiscode.json"
    full_env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_AGENTISCODE_LOG": str(log),
        "WORKDIR": str(tmp_path),
        **env,
    }
    subprocess.run(
        ["/bin/bash", "-c", build_bash_wrapper(script, workdir=str(tmp_path))], env=full_env, check=True, timeout=30
    )
    return json.loads(log.read_text(encoding="utf-8"))


def _option(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


AGENT_STEP_YAML = """
    - name: Select news
      agent:
        adapter: claude
        model: ${NEWS_MODEL}
        prompt: |
          Vyber zprávy. Nic neexpanduj: $HOME `id` $(id)
        inputs:
          - label: "Historie:"
            path: ${AGENTIS_RUN_DIR}/history.md
            optional: true
          - label: "Položky:"
            path: $AGENTIS_RUN_DIR/items.json
        outputSchema: ${WORKDIR}/.agentis/schemas/selection.schema.json
        schemaRetries: 1
        output: $AGENTIS_RUN_DIR/selection.json
"""


def test_agent_step_generates_run_script(tmp_path: Path) -> None:
    step = _load(tmp_path, AGENT_STEP_YAML).workflow.steps[0]

    assert step.agent is not None
    assert step.run is not None and "agentiscode" in step.run
    assert "--output-schema" in step.run


def test_agent_step_script_passes_prompt_inputs_and_options(tmp_path: Path) -> None:
    step = _load(tmp_path, AGENT_STEP_YAML).workflow.steps[0]
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "items.json").write_text('[{"id": 1}]', encoding="utf-8")

    call = _run_step(
        tmp_path,
        step.run,
        {"AGENTIS_RUN_DIR": str(run_dir), "NEWS_MODEL": "claude-sonnet-5-5", "AGENTIS_RUN_ID": "r1"},
    )

    argv = call["argv"]
    assert _option(argv, "--adapter") == "claude"
    assert _option(argv, "--model") == "claude-sonnet-5-5"
    assert "--effort" not in argv and "--task-id" not in argv
    assert _option(argv, "--run-id") == "r1"
    assert _option(argv, "--session-title") == "Select news"
    assert _option(argv, "--output-schema") == f"{tmp_path}/.agentis/schemas/selection.schema.json"
    assert _option(argv, "--schema-retries") == "1"
    assert _option(argv, "--final-output") == f"{run_dir}/selection.json"
    # Inline prompt je doslovný, volitelný chybějící vstup se přeskočí.
    assert call["stdin"] == 'Vyber zprávy. Nic neexpanduj: $HOME `id` $(id)\n\n\nPoložky:\n[{"id": 1}]'


def test_agent_step_task_overrides_win_unless_disabled(tmp_path: Path) -> None:
    overrides = {"AGENTIS_MODEL": "task-model", "AGENTIS_EFFORT": "high", "AGENTIS_PROMPT_FILE": str(tmp_path / "p")}
    (tmp_path / "p").write_text("Prompt tasku", encoding="utf-8")
    steps_yaml = """
    - name: Default
      agent:
        adapter: claude
        model: workflow-model
    - name: Pinned
      agent:
        adapter: claude
        model: workflow-model
        taskOverrides: false
"""
    default, pinned = _load(tmp_path, steps_yaml).workflow.steps

    call = _run_step(tmp_path, default.run, overrides)
    assert _option(call["argv"], "--model") == "task-model"
    assert _option(call["argv"], "--effort") == "high"
    assert call["stdin"] == "Prompt tasku"  # bez prompt/promptFile jde prompt tasku

    call = _run_step(tmp_path, pinned.run, overrides)
    assert _option(call["argv"], "--model") == "workflow-model"
    assert "--effort" not in call["argv"]


def test_agent_step_values_cannot_inject_shell(tmp_path: Path) -> None:
    title = 'x" $(touch pwned) `touch pwned2` ${HOME:-"'
    steps_yaml = """
    - name: Agent
      agent:
        adapter: claude
        sessionTitle: "Report [%TASK_TITLE%]"
        promptFile: "$WORKDIR/prompt [%TASK_TITLE%].md"
"""
    step = _load(tmp_path, steps_yaml, task_title=title).workflow.steps[0]
    (tmp_path / f"prompt {title}.md").write_text("ok", encoding="utf-8")

    call = _run_step(tmp_path, step.run, {})

    assert _option(call["argv"], "--session-title") == f"Report {title}"
    assert call["stdin"] == "ok"
    assert not (tmp_path / "pwned").exists() and not (tmp_path / "pwned2").exists()


def test_agent_step_prompt_containing_heredoc_delimiter(tmp_path: Path) -> None:
    steps_yaml = """
    - name: Agent
      agent:
        adapter: claude
        prompt: |
          první
          AGENTIS_PROMPT
          poslední
"""
    step = _load(tmp_path, steps_yaml).workflow.steps[0]

    call = _run_step(tmp_path, step.run, {})

    assert call["stdin"] == "první\nAGENTIS_PROMPT\nposlední\n"


def test_agent_step_extra_args_are_passed_as_json(tmp_path: Path) -> None:
    steps_yaml = """
    - name: Agent
      agent:
        adapter: claude
        prompt: Ahoj
        extraArgs: ["--max-turns", "3 'quoted'"]
"""
    step = _load(tmp_path, steps_yaml).workflow.steps[0]

    call = _run_step(tmp_path, step.run, {})

    assert json.loads(_option(call["argv"], "--extra-args-json")) == ["--max-turns", "3 'quoted'"]


@pytest.mark.parametrize(
    ("agent_yaml", "message"),
    [
        ("adapter: claude\n        prompt: a\n        promptFile: b", "either 'prompt' or 'promptFile'"),
        ("adapter: claude\n        schemaRetries: 1", "'schemaRetries' requires 'outputSchema'"),
        ("adapter: claude\n        unknown: 1", "Extra inputs are not permitted"),
    ],
)
def test_agent_step_validation_errors(tmp_path: Path, agent_yaml: str, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        _load(tmp_path, f"    - name: Agent\n      agent:\n        {agent_yaml}\n")


def test_agent_step_rejects_explicit_run(tmp_path: Path) -> None:
    steps_yaml = "    - name: Agent\n      run: echo hi\n      agent:\n        adapter: claude\n"
    with pytest.raises(ValidationError, match="both 'run' and 'agent'"):
        _load(tmp_path, steps_yaml)


def test_agent_step_overrides_run_from_template(tmp_path: Path) -> None:
    path = tmp_path / "wf.yaml"
    path.write_text(
        """
version: 1
workflow:
  executor: local
  stepTemplates:
    tpl:
      run: echo template
      timeoutSeconds: 120
  steps:
    - name: Agent
      uses: tpl
      agent:
        adapter: claude
        prompt: Ahoj
""",
        encoding="utf-8",
    )
    step = load_workflow_file(path, {}).workflow.steps[0]

    assert "agentiscode" in step.run and "echo template" not in step.run
    assert step.timeoutSeconds == 120
