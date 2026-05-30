# Project Guidelines

## Architecture

- Keep the FastAPI entrypoint thin in `app/main.py`; request validation, run lifecycle logic, and adapter orchestration belong in services and models.
- Treat `app/services/jsonrpc.py` as the source of truth for JSON-RPC method behavior and in-memory run state. This service is intentionally stateless across process restarts; do not add persistence unless a task explicitly requires it.
- Keep API payload schemas in `app/models.py` and validate via Pydantic models instead of ad-hoc dict handling.
- Keep Kubernetes manifest templating and `kubectl` execution inside `app/services/manifest_parser.py` and `app/services/adapter.py`.

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

- Runtime adapter flows call `kubectl` and require a valid kube context plus a readable manifest path.
- The default manifest is `kubernetes/opencode.yaml`, which relies on placeholder substitution for `[%NAMESPACE%]`, `[%WORKDIR%]`, `[%APP_HOST%]`, and `[%MAIN_DIR%]

##  Agentis
- aplikace komunukuje s ticket systémem Agentis pres json AgentisJsonRpcClient
- jeho zdrojáky jsou v /var/www/agentis
- API endpointy jsou popsané v /var/www/agentis/backend/api