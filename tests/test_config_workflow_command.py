from __future__ import annotations

from pathlib import Path

from common import config


def test_workflow_command_comes_from_env_with_shell_quoting(monkeypatch) -> None:
    monkeypatch.setenv("AGENTIS_WORKFLOW_COMMAND", "uv run 'agentis workflow'")

    assert config._workflow_command() == ("uv", "run", "agentis workflow")


def test_workflow_command_prefers_sibling_of_running_interpreter(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("AGENTIS_WORKFLOW_COMMAND", raising=False)
    sibling = tmp_path / "agentis-workflow"
    sibling.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(config.sys, "executable", str(tmp_path / "python"))

    assert config._workflow_command() == (str(sibling),)


def test_workflow_command_falls_back_to_path_lookup(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("AGENTIS_WORKFLOW_COMMAND", raising=False)
    monkeypatch.setattr(config.sys, "executable", str(tmp_path / "python"))

    assert config._workflow_command() == ("agentis-workflow",)
