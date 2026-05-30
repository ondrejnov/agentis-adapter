## 1. Shared JSON-RPC Dispatch

- [x] 1.1 Extract transport-independent JSON-RPC request dispatch from `app/adapter_app.py` into a reusable service/module.
- [x] 1.2 Update the existing HTTP `/api` and `/api-internal` routes to use the shared dispatcher without changing response bodies, JSON-RPC error codes, or HTTP status mappings.
- [x] 1.3 Add adapter tests proving existing HTTP JSON-RPC methods still validate params and return the same success/error shapes.

## 2. Adapter Passive WebSocket Client

- [x] 2.1 Add a Python 3.13 compatible WebSocket client dependency to the adapter project.
- [x] 2.2 Add passive transport settings for transport mode, WebSocket endpoint, adapter identity, heartbeat interval, reconnect limits, and auth token reuse.
- [x] 2.3 Extend the `agentis-adapter` CLI with a startup transport option that keeps HTTP as the default and runs no public Uvicorn listener in WebSocket mode.
- [x] 2.4 Implement the passive WebSocket client loop with authenticated handshake headers, heartbeat/ping handling, bounded reconnect backoff, and secret-safe logging.
- [x] 2.5 Dispatch inbound WebSocket JSON-RPC requests through the shared dispatcher and send exactly one correlated JSON-RPC response for each request with an `id`.
- [x] 2.6 Add adapter tests for successful proxied `start`, invalid JSON-RPC errors, response id correlation, auth/config validation, reconnect behavior, and token-safe logs.

## 3. Agentis Cloud Passive Routing

- [x] 3.1 Add Agentis backend adapter configuration for passive WebSocket transport and stable adapter identity, using the existing adapter entity ID where possible.
- [x] 3.2 Add an authenticated Agentis WebSocket endpoint that registers one active connection per passive adapter identity and tracks last-seen/heartbeat status.
- [x] 3.3 Add a JSON-RPC-over-WebSocket client path in Agentis routing that can send requests through an active adapter connection and await correlated responses.
- [x] 3.4 Route `start`, `add_message`, `question`, and `abort` through the passive connection when the adapter is configured for passive transport.
- [x] 3.5 Preserve the existing HTTP `call_jsonrpc` path for adapters configured with HTTP transport.
- [x] 3.6 Add Agentis backend tests for online passive routing, offline passive adapter failures, duplicate connection handling, authentication rejection, and existing HTTP routing compatibility.

## 4. Documentation And Verification

- [x] 4.1 Document passive WebSocket configuration in README and `.env.example`, including local `ws://` and production `wss://` examples.
- [x] 4.2 Document rollback by switching the adapter transport back to HTTP and using the existing adapter URL/tunnel path.
- [ ] 4.3 Run `poetry run pytest -q` and `poetry run ruff check .` in the adapter repository.
- [ ] 4.4 Run the relevant Agentis backend test suite and lint checks for the passive routing changes.
