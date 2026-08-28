# Agentis Adapter Guidelines

## Project Overview

Agentis Adapter is a Python 3.13 service that receives Agentis JSON-RPC requests over an adapter-initiated WebSocket connection, prepares a project workspace, and runs declarative YAML workflows. A small FastAPI server exposes read-only health, status, and log endpoints; it is not the production task transport.

## Repository Map

- `app/cli.py`: `agentis-adapter` CLI entrypoint and WebSocket/status-server lifecycle.
- `app/adapter_api.py`: thin FastAPI application and JSON-RPC dispatch table.
- `common/config.py`: environment-backed immutable settings. Clear the `get_settings()` cache in tests after changing environment variables.
- `common/models.py`: Pydantic request, execution-context, and run payloads.
- `common/rpc/dispatcher.py`: transport-independent JSON-RPC validation, dispatch, and error mapping.
- `common/rpc/jsonrpc.py`: behavior for `start`, `add_message`, `abort`, and `undo`.
- `common/rpc/passive_websocket.py`: outbound WebSocket connection, reconnects, and graceful shutdown.
- `common/git_adapter.py`: worktree, branch, snapshot, and project-scope workspace behavior.
- `common/workflow/schema.py`: source of truth for workflow YAML schema, interpolation, inheritance, conditions, and followups.
- `common/workflow/manager.py`: background run lifecycle, scheduling, retries, outputs, and Agentis reporting.
- `common/workflow/runtime.py`, `local_runtime.py`, `docker_runtime.py`: Kubernetes, local-process, and Docker step runners.
- `common/artifacts/`: source snapshots, change diffs, screenshots, and output artifacts.
- `workflows/`: bundled fallback workflows used when a project has no matching `.agentis/workflows/*.yaml` file.
- `tests/`: pytest coverage for JSON-RPC, transports, workflows, artifacts, status, shutdown, and CLI behavior.
- `docs/adapter.md` and `docs/workflow.md`: protocol and workflow documentation that must stay aligned with behavioral changes.

## Architecture Rules

- Keep `app/adapter_api.py` and `app/cli.py` thin. Put payload validation in `common/models.py`, RPC behavior in `common/rpc/`, and workflow behavior in `common/workflow/`.
- Production task methods arrive over the passive WebSocket transport. Do not add an HTTP `/api` route unless the task explicitly changes the transport architecture.
- Keep the HTTP surface read-only: `/health`, `/status`, `/log`, and `/runs/{run_id}/log` are observability endpoints.
- Preserve the JSON-RPC method names, Pydantic parameter models, error codes, and HTTP-status mapping unless an API change is explicitly requested.
- `start` and `add_message` must start the workflow and return promptly; the workflow itself runs in a background thread.
- Keep workflow run state in memory. Do not add persistence or cross-process coordination unless explicitly requested.
- Keep Agentis-specific HTTP calls behind `AgentisJsonRpcClient`; the related Agentis backend source is in `/var/www/agentis`, with API code under `/var/www/agentis/backend/api`.

## Workflow Rules

- A project workflow at `.agentis/workflows/<name>.yaml` takes precedence over the bundled file in `workflows/` with the same basename.
- Use `default.yaml` for task/worktree scope, `project.yaml` for project scope, and `<name>.yaml` for `context.adapter.workflow` followup actions.
- The supported executors are `local`, `docker`, and `kubernetes`. Adapter runtime values `local` and `docker` force the matching executor; otherwise workflow YAML wins, followed by `WORKFLOW_EXECUTOR`.
- The local executor runs commands directly as the adapter user and is not a sandbox. Docker and Kubernetes executors require an image for every resolved step.
- Preserve the workflow schema's strict validation and token allowlist. Add new YAML fields, interpolation tokens, conditions, or outputs in `common/workflow/schema.py` before using them at runtime.
- Workflow files are loaded and frozen at run start. Do not make running workflows depend on later YAML edits.
- Keep one active workflow per task. Preserve abort events, timeout handling, retries, `continueOnError`, `always`, dependency scheduling, and graceful shutdown semantics.
- Outputs from skipped or failed steps must not be applied. Successful `always` step outputs may still report a workflow failure.
- Treat workflow paths and artifact paths as untrusted input. Keep containment checks and file-count/size limits intact.
- If workflow behavior or syntax changes, update `docs/workflow.md`, bundled workflows, and focused tests together.

## Security And Data Handling

- Never commit `.env` or expose `AGENTIS_API_TOKEN`, `AGENTIS_TOKEN`, `AGENTIS_SERVICE_TOKEN`, authorization headers, or other credentials in logs, API responses, fixtures, or error details.
- Keep WebSocket authentication in headers and require `wss://` for non-local endpoints.
- Do not weaken validation for workflow names, manifests, branches, paths, mounts, or artifact globs.
- Do not introduce shell interpolation of untrusted values where structured arguments or environment variables can be used.
- Preserve best-effort reporting boundaries: failures while posting status to Agentis should be visible but must not conceal the original workflow result.

## Development Commands

```bash
poetry install
poetry run agentis-adapter
poetry run pytest -q
poetry run ruff check .
```

The adapter requires `AGENTIS_ADAPTER_ID`, `AGENTIS_API_TOKEN` (or `AGENTIS_TOKEN`), and `AGENTIS_WS_ENDPOINT` for a real WebSocket connection. Use `WORKFLOW_EXECUTOR=local` only for trusted local development. The observability server defaults to `0.0.0.0:8001`.

## Testing Conventions

- Run `poetry run pytest -q` and `poetry run ruff check .` before finishing code changes.
- Prefer focused tests while iterating, for example `poetry run pytest -q tests/test_workflow.py`, then run the full suite.
- Follow the existing pytest style: plain test functions, `tmp_path`, `monkeypatch`, and small local fakes rather than broad mocking frameworks.
- Test external JSON-RPC through `tests.support.RpcTestClient`. Its synthetic `POST /api` calls dispatch in-process and do not imply that production exposes `/api` over HTTP.
- Use FastAPI `TestClient` for the real observability endpoints.
- Stub Agentis clients, subprocess runners, Docker, `kubectl`, network calls, and git operations. Tests must not mutate real repositories, contact Agentis, or require container infrastructure.
- Add regression tests at the lowest useful boundary, but cover contract changes through the transport-facing test helpers.
- Keep tests deterministic: wait on explicit events or joins instead of arbitrary sleeps when exercising background workflow threads.

## Code Style

- Target Python `>=3.13,<3.14`, FastAPI, Pydantic v2, pytest, and Ruff with a 120-character line length.
- Follow the existing type-hinted Python style and use `pathlib.Path` for filesystem work.
- Prefer the smallest correct change and reuse existing models, dispatcher helpers, runners, and status/reporting APIs.
- Use Pydantic models for API and YAML payloads instead of ad-hoc validation.
- Keep comments concise and focused on non-obvious lifecycle, concurrency, or compatibility constraints. Existing Czech and English comments are both acceptable; match the surrounding file.
- Do not add a dry-run path to `start`; adapter starts always execute the selected workflow.

## Change Checklist

- Confirm whether a change affects the WebSocket contract, observability HTTP API, Agentis callback API, workflow schema, or executor behavior.
- Update models, implementation, tests, bundled workflow examples, and documentation consistently for the affected boundary.
- Verify secrets and local paths cannot leak through returned payloads or logs.
- Run the full pytest and Ruff checks and report any checks that could not be run.
