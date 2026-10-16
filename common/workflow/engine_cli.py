"""Volání CLI enginu `agentis-workflow` (kontrakt: `docs/cli.md` v repu enginu).

Adapter engine neimportuje — jen spouští příkaz z `settings.workflow_command`. Secrety se do argv ani do
spec.json nedostanou; předávají se přes env child procesu (`pass_env`).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

#: Verze kontraktu spec/result; adapter ji ověří u `check` i u `result.json`.
PROTOCOL_VERSION = 1

EXIT_OK = 0
EXIT_INVALID = 2
EXIT_BUSY = 3

SPEC_FILE_NAME = "spec.json"
RESULT_FILE_NAME = "result.json"

CHECK_TIMEOUT_SECONDS = 300
ABORT_TIMEOUT_SECONDS = 300


class EngineCommandError(RuntimeError):
    """Engine nejde spustit (chybí binárka, nesedí protokol, neočekávaný výstup)."""


def write_spec(run_dir: Path, spec: Mapping[str, Any]) -> Path:
    path = run_dir / SPEC_FILE_NAME
    path.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def check(command: Sequence[str], spec_path: Path) -> subprocess.CompletedProcess[str]:
    """Synchronní `check --spec`; vrací proces (exit 0 = JSON na stdout, 2 = nevalidní, 3 = busy)."""

    return _run([*command, "check", "--spec", str(spec_path)], timeout=CHECK_TIMEOUT_SECONDS)


def check_summary(completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    try:
        summary = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise EngineCommandError(f"agentis-workflow check returned invalid JSON: {exc}") from exc
    if not isinstance(summary, dict) or summary.get("protocol") != PROTOCOL_VERSION:
        raise EngineCommandError(f"agentis-workflow check returned unsupported protocol: {summary!r:.200}")
    return summary


def spawn_run(command: Sequence[str], spec_path: Path, env: Mapping[str, str]) -> subprocess.Popen[str]:
    """`run --spec ... --events jsonl` v nové process group (SIGTERM jde celé skupině)."""

    return subprocess.Popen(
        [*command, "run", "--spec", str(spec_path), "--events", "jsonl"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=dict(env),
        start_new_session=True,
    )


def read_result(run_dir: Path) -> dict[str, Any] | None:
    """`result.json` z run dir; None, pokud engine nic nezapsal nebo je soubor nečitelný / jiné verze."""

    path = run_dir / RESULT_FILE_NAME
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(result, dict) or result.get("protocol") != PROTOCOL_VERSION:
        return None
    return result


def signal_process_group(process: subprocess.Popen[str], signum: int = signal.SIGTERM) -> None:
    try:
        os.killpg(process.pid, signum)
    except ProcessLookupError:
        pass


def abort_by_labels(
    command: Sequence[str],
    *,
    executor: str,
    namespace: str,
    labels: Mapping[str, str],
) -> str:
    """Úklid bez běžícího procesu (restart adapteru, dokončený run): `abort --executor ... --label ...`."""

    argv = [*command, "abort", "--executor", executor]
    if namespace:
        argv += ["--namespace", namespace]
    for key, value in labels.items():
        argv += ["--label", f"{key}={value}"]
    completed = _run(argv, timeout=ABORT_TIMEOUT_SECONDS)
    if completed.returncode != EXIT_OK:
        raise EngineCommandError(f"agentis-workflow abort failed (exit {completed.returncode}): {tail(completed.stderr)}")
    return completed.stdout.strip()


def _run(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise EngineCommandError(f"workflow engine command not found: {argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise EngineCommandError(f"agentis-workflow {argv[1] if len(argv) > 1 else ''} timed out") from exc


def tail(text: str, limit: int = 500) -> str:
    stripped = text.strip()
    return stripped[-limit:] if stripped else "no stderr"
