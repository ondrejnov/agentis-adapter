from __future__ import annotations

import shlex
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Sequence

from common.kubernetes.agent_job import AgentJobRunner


_STREAM_READ_CHUNK_SIZE = 64 * 1024


@dataclass
class KubectlExecTarget:
    """Target for running an agent CLI as a one-shot Kubernetes ``Job``.

    The agent CLI no longer ``kubectl exec``-s into a long-running Deployment; it
    runs as a short-lived ``Job`` declared by ``.agentis/run.yaml``. ``selector``
    and ``container`` are kept for backwards compatibility but are unused by the
    Job runner.
    """

    namespace: str
    selector: str = "deployment/opencode"
    container: Optional[str] = "opencode"
    kubectl: str = "kubectl"
    # Job-mode wiring (see common/kubernetes/agent_job.py).
    run_manifest_path: Optional[str] = None
    workspace_path: Optional[str] = None
    agentis_url: Optional[str] = None
    job_name: str = "agent-run"


def agent_job_runner(target: KubectlExecTarget) -> AgentJobRunner:
    """Build an :class:`AgentJobRunner` from a kubectl target."""
    if not target.run_manifest_path:
        raise RuntimeError("kubectl target is missing run_manifest_path for the agent Job")
    if not target.workspace_path:
        raise RuntimeError("kubectl target is missing workspace_path for the agent Job")
    return AgentJobRunner(
        kubectl=target.kubectl,
        namespace=target.namespace,
        run_manifest_path=target.run_manifest_path,
        workspace_path=target.workspace_path,
        agentis_url=target.agentis_url,
        job_name=target.job_name,
    )


def write_agent_prompt_file(cwd: str, namespace: str, prompt: str) -> str:
    """Persist the prompt next to the worktree on the shared ``/var/www`` hostPath.

    The agent Job mounts the same hostPath, so a file written here by the adapter
    is visible inside the Job pod under the identical absolute path. It lives
    outside the git worktree so it is never picked up by the commit step.
    """
    base = Path(cwd).resolve().parent / ".agentis-prompts"
    base.mkdir(parents=True, exist_ok=True)
    prompt_path = base / f"{namespace}-{uuid.uuid4().hex}.md"
    prompt_path.write_text(prompt, encoding="utf-8")
    return str(prompt_path)


def build_agent_command_script(
    *,
    cwd: str,
    argv: Sequence[str],
    env: dict[str, str] | None = None,
    stdin_file: str | None = None,
) -> str:
    """Build the ``bash -lc`` script that the agent Job container runs.

    ``stdin_file`` (used by ``claude --print -``) is redirected into the command;
    agents that read the prompt from a file argument leave it ``None``.
    """
    inner_argv = list(argv)
    if env:
        inner_argv = ["env", *[f"{key}={value}" for key, value in env.items()], *inner_argv]
    command = " ".join(shlex.quote(part) for part in inner_argv)
    if stdin_file:
        command = f"{command} < {shlex.quote(stdin_file)}"
    return f"cd {shlex.quote(cwd)} && exec {command}"


def unbounded_line_reader(stream: Any) -> Callable[[], Awaitable[bytes]]:
    """Return a readline-like coroutine that is not capped by StreamReader's line limit."""

    buffer = bytearray()
    eof = False

    async def read_line() -> bytes:
        nonlocal eof

        while True:
            separator_at = buffer.find(b"\n")
            if separator_at >= 0:
                line = bytes(buffer[: separator_at + 1])
                del buffer[: separator_at + 1]
                return line

            if eof:
                if not buffer:
                    return b""
                line = bytes(buffer)
                buffer.clear()
                return line

            chunk = await stream.read(_STREAM_READ_CHUNK_SIZE)
            if chunk:
                buffer.extend(chunk)
            else:
                eof = True

    return read_line


__all__ = [
    "KubectlExecTarget",
    "agent_job_runner",
    "build_agent_command_script",
    "unbounded_line_reader",
    "write_agent_prompt_file",
]
