# Project Guidelines

## Architecture

- Keep the FastAPI entrypoint thin in `app/main.py`; request validation, run lifecycle logic, and adapter orchestration belong in services and models.
- Treat `app/services/jsonrpc.py` as the source of truth for JSON-RPC method behavior and in-memory run state. This service is intentionally stateless across process restarts; do not add persistence unless a task explicitly requires it.
- Keep API payload schemas in `app/models.py` and validate via Pydantic models instead of ad-hoc dict handling.

## Build And Test

- Install dependencies with `poetry install`.
- Run the app locally with `poetry run uvicorn app.main:app --reload --port 8000`.
- Before finishing code changes, run `poetry run pytest -q` and `poetry run ruff check .`.

## Conventions

- Target the existing Python and tooling stack from `pyproject.toml`: Python 3.13, FastAPI, Pydantic v2, pytest, and Ruff with line length 120.
- Preserve the current JSON-RPC contract in `app/main.py`: keep the existing method names, JSON-RPC error codes, and HTTP status mappings unless the task explicitly changes the API.
- Preserve secret scrubbing in `RunStatePayload.safe_dump()`; never return or log `agentis_token`.
- `start` always executes the adapter workflow; do not add a dry-run mode unless a task explicitly requires it.
- Follow the existing test style in `tests/test_api.py`: end-to-end API assertions through `fastapi.testclient.TestClient`.

## Gotchas

- **Každý** run (`start`, `add_message`, `abort`) jde přes **workflow runtime** — žádný CLI session fallback v RPC vrstvě. Bez workflow souboru (`.agentis/workflows/default.yaml`, pro scope=project `project.yaml`, pro followup `<name>.yaml`) `WorkflowManager.start_workflow` vyhodí `FileNotFoundError` → `AgentJsonRpcService._start_workflow_run` ji mapuje na `AgentJsonRpcException(400)` zpět do Agentisu. `context.adapter.runtime` už neřídí routing, jen executor: runtime `local` vynutí lokální executor (`WorkflowManager._resolve_executor` přebije `workflow.executor` i `WORKFLOW_EXECUTOR`); jinak platí YAML/env. `undo` zůstává: workflow runtime bere per-session source snapshot na startu runu (`snapshot_sources_best_effort`, klíč `build_snapshot_key("workflow", ...)` na `_WorkflowRun.snapshot_key`), `undo` ho přes `WorkflowManager.snapshot_key_for_task` najde a `adapter.restore_snapshot` vrátí worktree zpět — stejný efekt jako dřív, jen klíč nejde z in-process session, ale z workflow runu. In-process session engine byl smazán (`common/session_manager.py`, `common/cli_adapter.py`, `*/session_manager.py`, `claude_p/client.py`, `opencode/activity_mapper.py` + adapter session metody `deploy`/`wait_ready`/`start_session`/`add_message`/`abort`). Konkrétní adaptery (`ClaudeCodeAdapterService`, …) jsou teď tenké podtřídy `GitAdapterService` — workflow runtime z nich volá jen `create_worktree`/`_workspace_path`/`restore_snapshot`/`post_agentis_event`. **Pozor:** `claude/client.py`, `claude/activity_mapper.py`, `opencode/runner.py` zůstávají živé — používá je agent CLI (`app/agentiscode.py`, `common/agentiscode.py`) a telemetrie (`common/agentis_telemetry.py`), ne adapter. Environment `kubernetes` jako runtime hodnota byl odstraněn.
- Workflow runtime spouští kroky podle executoru (`workflow.executor` v YAML, jinak env `WORKFLOW_EXECUTOR`, default `kubernetes`): `kubernetes` = Kubernetes Joby přes `kubectl` (vyžaduje platný kube context a `image`), `local` = lokální bash procesy nad worktree (`common/workflow/local_runtime.py`, K8s pole jako `image`/`volumes` se ignorují).
- Followup akce (git merge, úklid worktree a branche) nejsou samostatné RPC metody. `start` s `context.adapter.workflow = "<name>"` spustí pojmenované workflow `.agentis/workflows/<name>.yaml` (`merge.yaml`, `close.yaml`); run soubory pojmenovaných workflow jdou mimo worktree do `<project_run_root>/<run_id>/<attempt>/`, protože akce může worktree sama smazat.
- Opakované kroky workflow se sdílí přes `workflow.stepTemplates` + `uses` v kroku; šablona `run-agent` žije v `_base.yaml` a dědí se přes `extends`. Krok deklaruje jen odchylky: `env` se merguje po klíčích, ostatní pole (včetně `outputs`) krok přepisuje **celá**. Parametry šablony jsou env proměnné (`RUN_AGENT_FLAGS`, `RUN_AGENT_OUTPUT_DIR`, `RUN_AGENT_STREAM_FILTER`), viz docs/workflow.md.
- Nabídka followup akcí v completion komentáři se konfiguruje v `workflow.followups` sekci workflow YAML (`default.yaml`), nikde v Pythonu; workflow bez sekce (`project.yaml`, `merge.yaml`, `close.yaml`) žádné akce nenabízí. Followup může mít `if` podmínku nad `var` outputs runu (bez built-in hodnot) — v `default.yaml` se „Git merge" nabízí jen při `PR_CREATED`. Lokální sessions čtou sekci best-effort přes `load_workflow_followups()` (`common/workflow/schema.py`) a podmíněné followups přeskakují (žádné `var` outputs k vyhodnocení).
- Prostředí lokálních CLI sessions (PATH s venv, přípravné kroky) deklaruje mini workflow `.agentis/workflows/local-env.yaml` (nahradilo `.agentis/local-setup.sh`); nespouští ho WorkflowManager — při každém spawnu agent CLI z něj `build_local_env_shell_command()` (`common/workflow/local_env.py`) složí bash wrapper `env + kroky + exec agent`. Chybějící/nevalidní soubor = spuštění bez setupu.

##  Agentis
- aplikace komunukuje s ticket systémem Agentis pres json AgentisJsonRpcClient
- jeho zdrojáky jsou v /var/www/agentis
- API endpointy jsou popsané v /var/www/agentis/backend/api