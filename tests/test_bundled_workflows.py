"""Bundled workflow v `workflows/` musí projít validací skutečného enginu (`agentis-workflow validate`)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.support import find_engine_command

ROOT = Path(__file__).resolve().parents[1]


def test_bundled_workflows_validate_with_engine() -> None:
    command = find_engine_command()
    if command is None:
        pytest.skip("agentis-workflow is not installed in this environment")
    files = sorted(path for path in (ROOT / "workflows").glob("*.yaml") if not path.stem.startswith("_"))
    assert files, "no bundled workflows found"

    completed = subprocess.run(
        [*command, "validate", *map(str, files)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
