# Agentis Adapter Guidelines

## Project Overview

Agentis Adapter is a Python 3.13 service that receives Agentis JSON-RPC requests over an adapter-initiated WebSocket connection, prepares a project workspace, and runs workflows through the external `agentis-workflow` engine CLI. The workflow engine (YAML schema, scheduler, executors, artifacts) lives in its own project at `/var/www/agentis-workflow`; this repository only contains the Agentis-specific layer. A small FastAPI server exposes read-only health, status, and log endpoints; it is not the production task transport.

## Repository Map

- `app/cli.py`: `agentis-adapter` CLI entrypoint and WebSocket/status-server lifecycle. Workflow developer commands live in the engine CLI (`agentis-workflow`).
- `app/adapter_api.py`: thin FastAPI application and JSON-RPC dispatch table.
- `common/config.py`: environment-backed immutable settings, including `AGENTIS_WORKFLOW_COMMAND`. Clear the `get_settings()` cache in tests after changing environment variables.
- `common/models.py`: Pydantic request, execution-context, and run payloads.
- `common/rpc/dispatcher.py`: transport-independent JSON-RPC validation, dispatch, and error mapping.
- `common/rpc/jsonrpc.py`: behavior for `start`, `add_message`, `abort`, and `undo`.
- `common/rpc/passive_websocket.py`: outbound WebSocket connection, reconnects, and graceful shutdown.
- `common/git_adapter.py`: worktree, branch, snapshot, and project-scope workspace behavior.
- `common/workflow/manager.py`: `WorkflowManager` — selects the workflow file, namespace, run directory and labels; writes `prompt.md`, `context.json`, `spec.json`; calls the engine (`check`, `run --spec`, `abort`) as a subprocess; translates JSONL events into `run.adapter_event` and the final `result.json` into Agentis comments, attachments, artifacts, session IDs and followup actions; owns approval runs.
- `common/workflow/engine_cli.py`: the only module that knows the engine command line and its JSON contract (spec, check summary, JSONL, `result.json`, exit codes). It must not import the engine.
- `common/artifacts/`: source snapshots, change diffs, screenshots, and output artifacts (adapter side).
- `common/namespaces.py`, `common/attachments.py`, `common/prompt_context.py`, `common/status.py`, `common/agentis.py`: namespace names, attachments, prompt/role blocks, observability registry, and the Agentis HTTP client.
- `workflows/`: bundled Agentis workflows (`default.yaml`, `project.yaml`, `approval*.yaml`, `_base.yaml`) used when a project has no matching `.agentis/workflows/*.yaml` file. They are written in the engine's YAML format and must validate with `agentis-workflow validate`.
- `tests/fake_agentis_workflow.py`: fake engine binary that implements the CLI contract for adapter tests.
- `docs/adapter.md`: adapter protocol and behavior documentation that must stay aligned with behavioral changes.
- `docs/navrh-rozdeleni-agentis-workflow.md`: the decision record for the split between the adapter and the engine.
- `tests/`: pytest coverage for JSON-RPC, transports, workflow manager, artifacts, status, shutdown, and the CLI.

## Architecture Rules

- Keep `app/adapter_api.py` and `app/cli.py` thin. Put payload validation in `common/models.py`, RPC behavior in `common/rpc/`, and workflow behavior in `common/workflow/`.
- Production task methods arrive over the passive WebSocket transport. Do not add an HTTP `/api` route unless the task explicitly changes the transport architecture.
- Keep the HTTP surface read-only: `/health`, `/status`, `/log`, and `/runs/{run_id}/log` are observability endpoints.
- Preserve the JSON-RPC method names, Pydantic parameter models, error codes, and HTTP-status mapping unless an API change is explicitly requested.
- `start` and `add_message` must start the workflow and return promptly; the engine process runs in the background and the adapter reads its events on a reader thread.
- Keep workflow run state in memory. Do not add persistence or cross-process coordination unless explicitly requested.
- Keep Agentis-specific HTTP calls behind `AgentisJsonRpcClient`; the related Agentis backend source is in `/var/www/agentis`, with API code under `/var/www/agentis/backend/api`.

## Workflow Rules

- The workflow engine is the separate project `/var/www/agentis-workflow` (package `agentis_workflow`, CLI `agentis-workflow`). The adapter depends on it only through its command line and JSON contract (`docs/cli.md` in that repository). Never import `agentis_workflow` or any engine module from the adapter, and never import adapter modules from the engine.
- Changing the YAML schema, interpolation tokens, conditions, executors, artifact limits, or the `spec.json` / `result.json` / JSONL contract happens in the engine repository first. Bump the protocol version there when the contract changes, then update `common/workflow/engine_cli.py`, `tests/fake_agentis_workflow.py`, `docs/adapter.md`, and the bundled workflows here.
- A project workflow at `.agentis/workflows/<name>.yaml` takes precedence over the bundled file in `workflows/` with the same basename. The adapter passes both directories as `search_paths` in that order.
- Use `default.yaml` for task/worktree scope, `project.yaml` for project scope, and `<name>.yaml` for `context.adapter.workflow` followup actions.
- The executor is chosen by the adapter runtime (`local` or `docker` forces it), otherwise by workflow YAML, otherwise by `WORKFLOW_EXECUTOR` (passed as `executor_default` in `spec.json`). The engine decides and checks images during `agentis-workflow check`.
- The local executor runs commands directly as the adapter user and is not a sandbox. Docker and Kubernetes executors require an image for every resolved step.
- Workflow files are read by the engine once at run start. Do not make a running workflow depend on later YAML edits.
- Keep one active workflow per task. Preserve approval semantics (`require_vars: ["APPROVED"]`, `APPROVED` must be `0` or `1`, optional `APPROVED_METADATA`), abort via SIGTERM to the engine process group, and graceful shutdown that waits for the engine processes.
- Outputs from skipped or failed steps must not be applied; the engine returns only applicable outputs and followups in `result.json`. Successful `always` step outputs may still report a workflow failure.
- Treat workflow paths and artifact paths as untrusted input. The adapter must keep the artifact containment check (paths inside the run directory) and the file-count/size limits for artifacts it reads.
- Bundled workflows must stay valid for the engine; `tests/test_bundled_workflows.py` validates them with `agentis-workflow validate`.

## Security And Data Handling

- Never commit `.env` or expose `AGENTIS_API_TOKEN`, `AGENTIS_TOKEN`, `AGENTIS_SERVICE_TOKEN`, authorization headers, or other credentials in logs, API responses, fixtures, or error details.
- Secrets reach the engine only through the environment of its child process. `spec.json` names them in `pass_env` (currently `AGENTIS_SERVICE_TOKEN`) and never contains their values; command lines never contain them.
- Keep WebSocket authentication in headers and require `wss://` for non-local endpoints.
- Do not weaken validation for workflow names, manifests, branches, paths, mounts, or artifact globs.
- Do not introduce shell interpolation of untrusted values where structured arguments or environment variables can be used.
- Preserve best-effort reporting boundaries: failures while posting status to Agentis should be visible but must not conceal the original workflow result.

## Development Commands

```bash
poetry install                                   # also installs ../agentis-workflow in editable mode
poetry run agentis-adapter
poetry run agentis-workflow validate .agentis/workflows   # engine CLI (sibling repository)
poetry run agentis-workflow run <name> "prompt" --workdir . --executor local   # test a workflow locally
poetry run pytest -q
poetry run ruff check .
```

The adapter requires `AGENTIS_ADAPTER_ID`, `AGENTIS_API_TOKEN` (or `AGENTIS_TOKEN`), and `AGENTIS_WS_ENDPOINT` for a real WebSocket connection. `AGENTIS_WORKFLOW_COMMAND` (shlex) overrides the engine command. Use `WORKFLOW_EXECUTOR=local` only for trusted local development. The observability server defaults to `0.0.0.0:8001`.

## Testing Conventions

- Run `poetry run pytest -q` and `poetry run ruff check .` before finishing code changes.
- Prefer focused tests while iterating, for example `poetry run pytest -q tests/test_workflow_manager.py`, then run the full suite.
- Follow the existing pytest style: plain test functions, `tmp_path`, `monkeypatch`, and small local fakes rather than broad mocking frameworks.
- Workflow manager tests run the adapter against `tests/fake_agentis_workflow.py` (`settings.workflow_command = (sys.executable, fake)`); scenarios are selected with `FAKE_ENGINE_*` environment variables. Engine behavior itself is tested in the `agentis-workflow` repository.
- Only `tests/test_workflow_engine_integration.py` and `tests/test_bundled_workflows.py` run the real engine, and they skip when `agentis-workflow` is not installed.
- Test external JSON-RPC through `tests.support.RpcTestClient`. Its synthetic `POST /api` calls dispatch in-process and do not imply that production exposes `/api` over HTTP.
- Use FastAPI `TestClient` for the real observability endpoints.
- Stub Agentis clients, git snapshot operations, network calls, and container infrastructure. Tests must not mutate real repositories, contact Agentis, or require container infrastructure.
- Add regression tests at the lowest useful boundary, but cover contract changes through the transport-facing test helpers.
- Keep tests deterministic: wait on explicit events, joins, or ready markers written by the fake engine instead of arbitrary sleeps when exercising background workflow threads.

## Code Style

- Target Python `>=3.13,<3.14`, FastAPI, Pydantic v2, pytest, and Ruff with a 120-character line length.
- Follow the existing type-hinted Python style and use `pathlib.Path` for filesystem work.
- Prefer the smallest correct change and reuse existing models, dispatcher helpers, and status/reporting APIs.
- Use Pydantic models for API payloads instead of ad-hoc validation.
- Keep comments concise and focused on non-obvious lifecycle, concurrency, or compatibility constraints. Existing Czech and English comments are both acceptable; match the surrounding file.
- Do not add a dry-run path to `start`; adapter starts always execute the selected workflow.

## Change Checklist

- Confirm whether a change affects the WebSocket contract, observability HTTP API, Agentis callback API, the engine CLI contract (`spec.json`, `result.json`, JSONL, exit codes), or the workflow YAML schema (engine repository).
- Update models, implementation, tests, bundled workflow examples, and documentation consistently for the affected boundary.
- Verify secrets and local paths cannot leak through returned payloads, `spec.json`, command lines, or logs.
- Run the full pytest and Ruff checks and report any checks that could not be run.
