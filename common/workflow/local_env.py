"""Prostředí lokálního spuštění agent CLI z mini workflow `.agentis/workflows/local-env.yaml`.

Náhrada za dřívější `.agentis/local-setup.sh`: cílový projekt deklaruje prostředí
agent CLI (PATH s venv, přípravné kroky) ve workflow YAML místo ad-hoc shell
skriptu. Soubor se čte best-effort z cwd agenta při každém spawnu — chybějící
nebo nevalidní soubor znamená spuštění bez setupu; rozbitá konfigurace se
projeví varováním na stderr, ne pádem session.

Z workflow spec se použijí jen `workflow.env` (export; hodnoty expanduje bash,
takže `$PATH` v hodnotě zachová PATH hosta), `workflow.envFiles` a `steps[].run`.
Každý krok běží v subshellu — `exit 0` v kroku přeskočí jen zbytek kroku,
neukončí agenta; neúspěšný krok agenta nespustí (`set -e`). Kubernetes pole
a kroková pole `if`/`outputs`/`env` se ignorují (s varováním), stejně jako
v :class:`~common.workflow.local_runtime.LocalProcessRunner`.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Sequence

from common.workflow.schema import WorkflowFile, load_workflow_file, workflow_file_relpath

LOCAL_ENV_WORKFLOW_NAME = "local-env"
LOCAL_ENV_WORKFLOW_RELPATH = workflow_file_relpath(LOCAL_ENV_WORKFLOW_NAME)

_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _main_worktree_dir(cwd: Path) -> Path:
    """Adresář hlavního worktree (rodič git common dir); mimo git repo vrací `cwd`."""

    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return cwd
    common_dir = result.stdout.strip()
    if result.returncode != 0 or not common_dir:
        return cwd
    return Path(common_dir).parent


def _ignored_fields(workflow: WorkflowFile) -> list[str]:
    spec = workflow.workflow
    ignored: list[str] = []
    if spec.image:
        ignored.append("image")
    if spec.imagePullSecrets:
        ignored.append("imagePullSecrets")
    if spec.volumeMounts:
        ignored.append("volumeMounts")
    if workflow.volumes:
        ignored.append("volumes")
    if spec.followups:
        ignored.append("followups")
    if any(step.if_ for step in spec.steps):
        ignored.append("steps[].if")
    if any(step.outputs for step in spec.steps):
        ignored.append("steps[].outputs")
    if any(step.env for step in spec.steps):
        ignored.append("steps[].env")
    if any(step.image for step in spec.steps):
        ignored.append("steps[].image")
    if any(step.resources for step in spec.steps):
        ignored.append("steps[].resources")
    if any(step.workingDir for step in spec.steps):
        ignored.append("steps[].workingDir")
    if any(step.continueOnError for step in spec.steps):
        ignored.append("steps[].continueOnError")
    if any(step.retries for step in spec.steps):
        ignored.append("steps[].retries")
    if any(step.always for step in spec.steps):
        ignored.append("steps[].always")
    return ignored


def _load_workflow(path: Path, base: Path, main_dir: Path) -> WorkflowFile | None:
    values = {"WORKDIR": str(base), "MAIN_DIR": str(main_dir)}
    try:
        return load_workflow_file(path, values)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"[local-env] invalid workflow {path}: {exc!r}\n")
        return None


def load_local_env_workflow(cwd: str | Path | None) -> WorkflowFile | None:
    """Best-effort načte mini workflow s prostředím lokálního agenta z worktree."""

    base = Path(cwd) if cwd else Path.cwd()
    path = base / LOCAL_ENV_WORKFLOW_RELPATH
    if not path.is_file():
        return None
    return _load_workflow(path, base, _main_worktree_dir(base))


def build_local_env_shell_command(argv: Sequence[str], *, cwd: str | Path | None = None) -> str:
    """Bash skript pro spawn agent CLI: env + kroky z local-env workflow, pak `exec`."""

    command = " ".join(shlex.quote(arg) for arg in argv)
    base = Path(cwd) if cwd else Path.cwd()
    path = base / LOCAL_ENV_WORKFLOW_RELPATH
    if not path.is_file():
        return f"exec {command}"
    main_dir = _main_worktree_dir(base)
    workflow = _load_workflow(path, base, main_dir)
    if workflow is None:
        return f"exec {command}"

    spec = workflow.workflow
    ignored = _ignored_fields(workflow)
    if ignored:
        sys.stderr.write(f"[local-env] {LOCAL_ENV_WORKFLOW_RELPATH} ignoruje pole: {', '.join(ignored)}\n")

    lines = ["set -euo pipefail"]
    lines.append(f"export WORKDIR={shlex.quote(str(base))}")
    lines.append(f"export MAIN_DIR={shlex.quote(str(main_dir))}")
    for env_file in spec.envFiles:
        lines.extend(["set -a", f". {env_file}", "set +a"])
    for key, value in spec.env.items():
        if not _ENV_KEY_RE.match(key):
            sys.stderr.write(f"[local-env] přeskočen env klíč {key!r} (není shell identifikátor)\n")
            continue
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'export {key}="{escaped}"')
    for step in spec.steps:
        lines.append(f"(\n{step.run.rstrip()}\n)")
    lines.append(f"exec {command}")
    return "\n".join(lines)


__all__ = [
    "LOCAL_ENV_WORKFLOW_NAME",
    "LOCAL_ENV_WORKFLOW_RELPATH",
    "build_local_env_shell_command",
    "load_local_env_workflow",
]
