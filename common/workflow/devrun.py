"""Vývojářské spouštění workflow bez Agentisu (`agentis-adapter workflow …`).

`run_workflow` posílá stejný JSON-RPC `start` jako Agentis a vede ho stejnou cestou
(`AgentJsonRpcService` → `WorkflowManager`). Volání zpět do Agentisu (eventy kroků,
komentáře, přílohy) místo HTTP vypisuje do konzole.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TextIO
from uuid import uuid4

from dotenv import dotenv_values

from common.config import Settings, get_settings
from common.models import TaskStatus
from common.workflow.schema import INTERPOLATION_ALLOWLIST, WORKFLOW_DIR_RELPATH, load_workflow_file

WORKFLOW_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
SCOPES = ("project", "task", "worktree")
RUNTIMES = ("local", "docker", "workflow")
DEFAULT_MODEL = "openai/gpt-5.4-mini"
DEFAULT_EFFORT = "low"
COMMENT_PREVIEW_LINES = 60
TASK_STATUS_NAMES = {
    value: name.lower() for name, value in vars(TaskStatus).items() if name.isupper() and isinstance(value, int)
}
LOG_TAIL_LINES = 30


class WorkflowCliError(Exception):
    """Chyba vstupu z CLI; vypíše se bez tracebacku."""


# ----------------------------------------------------------------------
# Hledání projektu a workflow souborů
# ----------------------------------------------------------------------


def find_project(start: Path) -> Path:
    """Nejbližší nadřazený adresář s `.agentis/workflows`; bez nálezu `start`."""

    start = start.resolve()
    for candidate in (start, *start.parents):
        if (candidate / WORKFLOW_DIR_RELPATH).is_dir():
            return candidate
    return start


def resolve_target(target: str, project: Path | None, cwd: Path) -> tuple[Path, str]:
    """Převede jméno workflow nebo cestu k YAML na (adresář projektu, jméno workflow)."""

    if target.endswith((".yaml", ".yml")) or os.sep in target:
        path = (cwd / target).resolve()
        if not path.is_file():
            raise WorkflowCliError(f"Soubor workflow neexistuje: {path}")
        if path.parent.name != "workflows" or path.parent.parent.name != ".agentis":
            raise WorkflowCliError(f"Workflow musí ležet v <projekt>/{WORKFLOW_DIR_RELPATH}/: {path}")
        resolved_project = path.parent.parent.parent
        if project is not None and project.resolve() != resolved_project:
            raise WorkflowCliError(f"Soubor {path} nepatří do projektu {project}")
        return resolved_project, path.stem
    if not WORKFLOW_NAME_RE.match(target):
        raise WorkflowCliError(f"Neplatné jméno workflow {target!r} (povoleno jen [A-Za-z0-9_.-])")
    return (project.resolve() if project is not None else find_project(cwd)), target


def workflow_path(project: Path, name: str, settings: Settings) -> Path | None:
    """Projektový soubor má přednost před zabaleným (stejně jako v manageru)."""

    for candidate in (project / WORKFLOW_DIR_RELPATH / f"{name}.yaml", settings.bundled_workflow_dir / f"{name}.yaml"):
        if candidate.is_file():
            return candidate
    return None


def list_workflows(project: Path, settings: Settings, out: TextIO = sys.stdout) -> int:
    project_dir = project / WORKFLOW_DIR_RELPATH
    project_names = sorted(p.stem for p in project_dir.glob("*.yaml") if not p.stem.startswith("_"))
    bundled_names = sorted(
        p.stem
        for p in settings.bundled_workflow_dir.glob("*.yaml")
        if not p.stem.startswith("_") and p.stem not in project_names
    )
    out.write(f"Projekt: {project}\n")
    for name in project_names:
        out.write(f"  {name}\n")
    if not project_names:
        out.write(f"  (žádná workflow v {project_dir})\n")
    if bundled_names:
        out.write(f"Zabalená v adapteru ({settings.bundled_workflow_dir}):\n")
        for name in bundled_names:
            out.write(f"  {name}\n")
    return 0


# ----------------------------------------------------------------------
# Validace
# ----------------------------------------------------------------------


def validate_file(path: Path, out: TextIO = sys.stdout) -> bool:
    """Načte workflow stejným loaderem jako adapter; navíc ověří existenci `envFiles`."""

    try:
        workflow = load_workflow_file(path, {name: f"<{name}>" for name in INTERPOLATION_ALLOWLIST})
    except Exception as exc:  # noqa: BLE001
        out.write(f"FAIL {path}\n     {type(exc).__name__}: {exc}\n")
        return False

    spec = workflow.workflow
    ok = True
    out.write(f"OK   {path}\n")
    out.write(f"     executor: {spec.executor or '(default adapteru)'}  image: {spec.image or '-'}\n")
    for index, step in enumerate(spec.steps, start=1):
        details = []
        if step.needs is not None:
            details.append(f"needs={step.needs}")
        if step.if_:
            details.append(f"if={step.if_}")
        if step.outputs:
            details.append("outputs=" + ",".join(output.type for output in step.outputs))
        out.write(f"     {index}. {step.name}{'  ' + '  '.join(details) if details else ''}\n")
    for followup in spec.followups:
        out.write(f"     followup: {followup.title} -> {followup.workflow}\n")
    for env_file in spec.envFiles:
        env_path = Path(env_file).expanduser()
        if not env_path.is_file():
            out.write(f"     WARN envFiles: {env_path} neexistuje (run by selhal)\n")
            ok = False
        else:
            keys = sorted(key for key in dotenv_values(env_path, interpolate=False))
            out.write(f"     envFiles: {env_path} ({len(keys)} klíčů)\n")
    return ok


def validate_workflows(targets: list[str], project: Path | None, cwd: Path, out: TextIO = sys.stdout) -> int:
    settings = get_settings()
    paths: list[Path] = []
    if targets:
        for target in targets:
            target_project, name = resolve_target(target, project, cwd)
            path = workflow_path(target_project, name, settings)
            if path is None:
                raise WorkflowCliError(f"Workflow {name!r} nenalezeno v {target_project / WORKFLOW_DIR_RELPATH}")
            paths.append(path)
    else:
        root = project.resolve() if project is not None else find_project(cwd)
        # `_base.yaml` a další rodiče nejsou samostatně spustitelné; ověří se přes `extends` potomků.
        paths = sorted(p for p in (root / WORKFLOW_DIR_RELPATH).glob("*.yaml") if not p.stem.startswith("_"))
        if not paths:
            raise WorkflowCliError(f"V {root / WORKFLOW_DIR_RELPATH} není žádné workflow")
    results = [validate_file(path, out) for path in paths]
    return 0 if all(results) else 1


# ----------------------------------------------------------------------
# Spuštění
# ----------------------------------------------------------------------


def build_start_payload(
    *,
    working_dir: Path,
    prompt: str,
    workflow: str | None,
    scope: str = "project",
    runtime: str = "local",
    model: str | None = DEFAULT_MODEL,
    effort: str | None = DEFAULT_EFFORT,
    agent: str = "build",
    title: str = "Mock workflow request",
    description: str = "",
    task_id: str | None = None,
    run_id: str | None = None,
    task_number: int = 999999,
    session_id: str | None = None,
    project_slug: str | None = None,
    project_title: str | None = None,
    project_github_repo: str | None = None,
    base_branch: str = "master",
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """JSON-RPC `start` ve tvaru, jaký posílá Agentis."""

    working_dir = working_dir.resolve()
    slug = project_slug or working_dir.name
    adapter: dict[str, Any] = {"scope": scope, "runtime": runtime, "agent": agent}
    # Bez modelu/effortu krok nedostane AGENTIS_MODEL/AGENTIS_EFFORT a použije default z workflow.
    if model:
        adapter["model"] = model
    if effort:
        adapter["effort"] = effort
    if workflow:
        adapter["workflow"] = workflow
    context: dict[str, Any] = {
        "run_id": run_id or f"mock-run-{uuid4().hex}",
        "task_id": task_id or f"mock-task-{uuid4().hex}",
        "session_id": session_id,
        "title": title,
        "description": description,
        "user_prompt": prompt,
        "task_number": task_number,
        "headers": headers or None,
        "project_slug": slug,
        "project_title": project_title or slug,
        "project_github_repo": project_github_repo,
        "base_branch": base_branch,
        "working_dir": str(working_dir),
        "adapter": adapter,
    }
    return {"jsonrpc": "2.0", "id": context["run_id"], "method": "start", "params": {"context": context}}


def create_service(
    settings: Settings, *, agentis_callbacks: bool, sink: Any | None = None, runner: Any | None = None
) -> Any:
    """`AgentJsonRpcService` jako v produkci; bez callbacků jdou Agentis volání do `sink`."""

    from common.git_adapter import GitAdapterService
    from common.rpc.jsonrpc import AgentJsonRpcService
    from common.workflow.manager import WorkflowManager

    if not agentis_callbacks:
        settings = replace(settings, agentis_endpoint=None)
    adapter_class = GitAdapterService
    if sink is not None and not agentis_callbacks:

        class _SinkAdapterService(GitAdapterService):
            def post_agentis_event(self, *, kind, status, event_id=None, message=None, data=None) -> None:
                sink(
                    "run.adapter_event",
                    {"run_id": self.context.run_id, "kind": kind, "status": status, "message": message, "data": data},
                )

        adapter_class = _SinkAdapterService
    manager = WorkflowManager(settings, runner=runner, agentis_sink=None if agentis_callbacks else sink)
    return AgentJsonRpcService(
        settings=settings,
        adapter_factory=lambda context: adapter_class(context=context, settings=settings),
        workflow_manager=manager,
    )


async def dispatch_start(payload: dict[str, Any], service: Any) -> tuple[dict[str, Any], int]:
    from app.adapter_api import _DISPATCH
    from common.rpc.dispatcher import dispatch_jsonrpc_payload

    container = SimpleNamespace(agent_jsonrpc_service=service)
    result = await dispatch_jsonrpc_payload(payload, _DISPATCH, container)
    return result.body, result.http_status


class ConsoleReporter:
    """Vypisuje Agentis RPC volání workflow manageru čitelně do terminálu."""

    def __init__(self, out: TextIO = sys.stdout, *, color: bool | None = None, full: bool = False) -> None:
        self.out = out
        self.color = out.isatty() if color is None else color
        self.full = full
        self.step_started: dict[Any, float] = {}
        self.comments = 0

    def _c(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def _line(self, text: str = "") -> None:
        self.out.write(text + "\n")
        self.out.flush()

    def __call__(self, method: str, params: dict[str, Any]) -> None:
        if method == "run.adapter_event":
            self._adapter_event(params)
        elif method == "task.add_agent_comment":
            self._comment(params)
        elif method == "run.store_session_id":
            self._line(self._c("2", f"  session_id: {params.get('session_id')}"))
        else:
            self._line(self._c("2", f"  [{method}]"))

    def _adapter_event(self, params: dict[str, Any]) -> None:
        kind = params.get("kind")
        status = params.get("status")
        data = params.get("data") or {}
        message = params.get("message") or ""
        if kind == "workflow_step":
            index = data.get("step_index")
            step = data.get("step") or message
            if status == "started":
                self.step_started[index] = time.monotonic()
                self._line(f"{self._c('36', '▶')} {step}")
                return
            started = self.step_started.pop(index, None)
            took = f" ({time.monotonic() - started:.1f}s)" if started is not None else ""
            if status == "success":
                self._line(f"{self._c('32', '✓')} {step}{self._c('2', took)}")
            elif status == "skipped":
                self._line(f"{self._c('33', '↷')} {message}")
            elif status == "failed":
                attempts = data.get("attempts")
                suffix = f", pokusů {attempts}" if attempts and attempts > 1 else ""
                tolerated = " — continueOnError" if data.get("continueOnError") else ""
                self._line(f"{self._c('31', '✗')} {message}{self._c('2', took + suffix + tolerated)}")
                tail = (data.get("log_tail") or "").rstrip().splitlines()[-LOG_TAIL_LINES:]
                for line in tail:
                    self._line(self._c("2", f"    │ {line}"))
            return
        if kind == "workflow_outputs":
            for attachment in data.get("attachments") or []:
                self._line(f"  příloha {attachment.get('label')}: {attachment.get('value')}")
            for name in data.get("artifact_names") or []:
                self._line(f"  artefakt: {name}")
            return
        if kind == "idle":
            # Konec workflow shrnuje `run_workflow`.
            return
        if message:
            marker = self._c("31", "✗") if status == "failed" else self._c("2", "·")
            self._line(f"{marker} {self._c('2', message) if status != 'failed' else message}")
            if status == "failed" and data.get("error"):
                self._line(f"    {data['error']}")

    def _comment(self, params: dict[str, Any]) -> None:
        self.comments += 1
        author = params.get("author_name") or "agent"
        status = TASK_STATUS_NAMES.get(params.get("status"), params.get("status"))
        header = f"Komentář do tasku — autor: {author}, status: {status}"
        self._line()
        self._line(self._c("1", f"┌─ {header}"))
        lines = (params.get("body") or "").splitlines()
        shown = lines if self.full else lines[:COMMENT_PREVIEW_LINES]
        for line in shown:
            self._line(f"│ {line}")
        if len(shown) < len(lines):
            self._line(self._c("2", f"│ … dalších {len(lines) - len(shown)} řádků (celé: --full)"))
        for attachment in params.get("attachments") or []:
            self._line(f"│ příloha {attachment.get('label')}: {attachment.get('value')}")
        for artifact in params.get("artifacts") or []:
            self._line(f"│ artefakt: {artifact.get('name')}")
        if params.get("images"):
            self._line(f"│ obrázků: {len(params['images'])}")
        for action in params.get("actions") or []:
            self._line(f"│ followup: [{action.get('title') or action.get('label')}]")
        self._line(self._c("1", "└─"))


def read_prompt(prompt: str | None, prompt_file: str | None, stdin: TextIO = sys.stdin) -> str:
    if prompt is not None and prompt_file is not None:
        raise WorkflowCliError("Zadej prompt buď jako argument, nebo přes --prompt-file, ne obojí")
    if prompt_file == "-":
        return stdin.read()
    if prompt_file is not None:
        path = Path(prompt_file)
        if not path.is_file():
            raise WorkflowCliError(f"Soubor s promptem neexistuje: {path}")
        return path.read_text(encoding="utf-8")
    return prompt or ""


def _declares_comment(path: Path) -> bool:
    try:
        workflow = load_workflow_file(path, {name: f"<{name}>" for name in INTERPOLATION_ALLOWLIST})
    except Exception:  # noqa: BLE001
        return False
    return any(output.type == "agent_comment" for step in workflow.workflow.steps for output in step.outputs)


def run_workflow(
    target: str,
    *,
    prompt: str = "",
    project: Path | None = None,
    cwd: Path | None = None,
    runtime: str = "local",
    scope: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    title: str | None = None,
    agentis_callbacks: bool = False,
    full: bool = False,
    out: TextIO = sys.stdout,
) -> int:
    """Spustí workflow, počká na konec a vrátí exit kód (0 = success)."""

    cwd = cwd or Path.cwd()
    project_dir, name = resolve_target(target, project, cwd)
    settings = get_settings()
    path = workflow_path(project_dir, name, settings)
    if path is None:
        raise WorkflowCliError(
            f"Workflow {name!r} nenalezeno v {project_dir / WORKFLOW_DIR_RELPATH} ani v {settings.bundled_workflow_dir}"
        )

    # `default`/`project` se v Agentisu nevolají jménem — vybírá je scope.
    if name == "default":
        scope, workflow_name = scope or "task", None
    elif name == "project":
        scope, workflow_name = scope or "project", None
    else:
        scope, workflow_name = scope or "project", name

    reporter = ConsoleReporter(out, full=full)
    service = create_service(settings, agentis_callbacks=agentis_callbacks, sink=reporter)
    payload = build_start_payload(
        working_dir=project_dir,
        prompt=prompt,
        workflow=workflow_name,
        scope=scope,
        runtime=runtime,
        model=model,
        effort=effort,
        title=title or f"Test workflow {name}",
        run_id=f"dev-run-{uuid4().hex[:12]}",
        task_id=f"dev-task-{uuid4().hex[:12]}",
    )
    context = payload["params"]["context"]

    out.write(reporter._c("1", f"Workflow {name}") + f"  ({path})\n")
    out.write(
        reporter._c("2", f"projekt {project_dir} · scope {scope} · runtime {runtime} · run {context['run_id']}\n")
    )
    if scope in {"task", "worktree"}:
        out.write(reporter._c("33", "Pozor: task scope vytvoří git worktree a větev v projektu.\n"))
    if agentis_callbacks:
        out.write(reporter._c("33", f"Výsledky se posílají do Agentisu ({settings.agentis_endpoint}).\n"))
    out.write("\n")

    started = time.monotonic()
    body, http_status = asyncio.run(dispatch_start(payload, service))
    if http_status >= 400 or "error" in body:
        error = body.get("error") or {}
        out.write(reporter._c("31", f"✗ Start selhal: {error.get('message') or body}\n"))
        if error.get("data"):
            out.write(f"    {error['data']}\n")
        return 1

    manager = service.workflow_manager
    try:
        while not manager.wait_idle(0.5):
            pass
    except KeyboardInterrupt:
        out.write(reporter._c("33", "\nPřerušeno — zastavuji workflow…\n"))
        from common.models import AgentExecutionContextPayload

        manager.abort(AgentExecutionContextPayload.model_validate(context))
        manager.wait_idle(30)
        return 130

    result = manager.run_result(context["task_id"]) or {}
    status = result.get("status", "unknown")
    elapsed = time.monotonic() - started
    out.write("\n")
    if status == "success":
        out.write(reporter._c("32", f"✓ Workflow doběhlo za {elapsed:.1f}s") + "\n")
    else:
        out.write(reporter._c("31", f"✗ Workflow skončilo stavem {status} po {elapsed:.1f}s") + "\n")
        if result.get("error"):
            out.write(f"    {result['error']}\n")
    run_dir = result.get("run_dir")
    if run_dir is not None:
        out.write(f"  run adresář: {run_dir}\n")
        logs = Path(run_dir) / "logs"
        if logs.is_dir():
            out.write(f"  logy:        {logs}\n")
        outputs = Path(run_dir) / "outputs"
        if outputs.is_dir():
            files = sorted(p.relative_to(outputs) for p in outputs.rglob("*") if p.is_file())
            out.write(f"  outputs:     {outputs}" + (f" ({', '.join(map(str, files[:10]))})" if files else "") + "\n")
    if reporter.comments == 0 and status == "success" and _declares_comment(path):
        out.write(reporter._c("33", "  Workflow nevytvořilo žádný komentář — zkontroluj cesty v outputs.\n"))
    return 0 if status == "success" else 1


__all__ = [
    "ConsoleReporter",
    "WorkflowCliError",
    "build_start_payload",
    "create_service",
    "dispatch_start",
    "find_project",
    "list_workflows",
    "read_prompt",
    "resolve_target",
    "run_workflow",
    "validate_workflows",
]
