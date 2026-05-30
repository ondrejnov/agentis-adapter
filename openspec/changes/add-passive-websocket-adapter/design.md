## Context

Agentis currently calls the adapter through the adapter URL configured on the Agentis adapter entity. The adapter exposes `POST /api` as JSON-RPC 2.0, validates params with Pydantic models, and dispatches into `AgentJsonRpcService`, which then runs the existing worktree, Kubernetes, OpenCode, Claude, and cleanup workflows.

This requires the client-side adapter to be reachable from Agentis. In practice that means a public address, ingress, or an SSH tunnel. The requested passive mode reverses the network direction: the adapter opens a long-lived outbound WebSocket connection to Agentis Cloud, and Agentis sends the same JSON-RPC calls over that connection.

The adapter runtime state remains in memory only. The WebSocket connection registry and routing state belong on the Agentis Cloud side, because Agentis decides which configured adapter should receive each run/message/question/abort request.

## Goals / Non-Goals

**Goals:**

- Let adapters run behind NAT/firewalls without exposing an inbound HTTP port.
- Preserve the existing JSON-RPC method names, params, result/error shape, and service behavior.
- Keep HTTP transport as the default and as a rollback path.
- Authenticate every passive adapter connection and bind it to a stable adapter identity.
- Handle transient disconnects with heartbeats and reconnect/backoff.
- Keep secret values out of logs and JSON-RPC error payloads.

**Non-Goals:**

- Replacing the existing Kubernetes, OpenCode, Claude, worktree, or Agentis event workflows.
- Persisting adapter runtime state in this repository.
- Designing Agentis Cloud high availability beyond the minimum active-connection registry required for routing.
- Multiplexing adapter-to-Agentis event posts over WebSocket in the first implementation; the existing HTTP Agentis client can remain in use.

## Decisions

### Transport-independent JSON-RPC dispatch

Refactor the current HTTP request handling into a reusable dispatcher that accepts a decoded JSON-RPC object, a dispatch table, and an initialized service container. The existing HTTP route and the new WebSocket client both use this dispatcher.

Rationale: this keeps `AgentJsonRpcService` as the source of truth and avoids drift between HTTP and WebSocket behavior.

Alternative considered: duplicate the JSON-RPC validation and error handling inside the WebSocket client. This is rejected because it risks different error codes, params validation, and logging behavior.

### Passive mode is selected at process startup

Add adapter transport configuration, with HTTP remaining the default. A concrete implementation can use `--transport http|websocket`, `ADAPTER_TRANSPORT`, `AGENTIS_WS_ENDPOINT`, and `AGENTIS_ADAPTER_ID`.

In HTTP mode, the CLI continues to run Uvicorn as it does today. In WebSocket mode, the CLI initializes the same adapter services but starts a WebSocket client loop instead of binding a public HTTP listener.

Rationale: transport mode is an operational choice, not a per-request option. Startup selection avoids running an unused inbound server on locked-down client machines.

Alternative considered: always start HTTP and WebSocket together. This is useful for migration but does not meet the "no exposed port" goal by default.

### JSON-RPC 2.0 is the WebSocket message format

Agentis sends standard JSON-RPC request objects over the WebSocket. The adapter replies with the standard JSON-RPC response object using the same `id`. Notifications without an `id` are accepted only for protocol-level control messages that do not require a response.

Rationale: the existing request/response contract can be reused by both sides, including current method names and params models.

Alternative considered: define a custom envelope with separate `request_id`, `type`, and `payload` fields. This is rejected unless a future cloud-side broker requires metadata that cannot fit in JSON-RPC.

### Authentication and identity are part of the WebSocket handshake

The adapter authenticates with the existing Agentis token and presents a stable adapter identity during the WebSocket handshake. The token should be sent as a header, not as a query parameter, to reduce accidental logging. Agentis binds the active socket to the adapter identity and rejects unauthenticated or duplicate-invalid connections.

Rationale: the connection is long-lived and must be authorized before Agentis can route run commands to it.

Alternative considered: register identity as the first WebSocket message. This allows a WebSocket to be established before authorization and makes rejection semantics less clear.

### Connection lifecycle uses heartbeat and reconnect

The WebSocket client sends or responds to ping/heartbeat traffic and reconnects with bounded exponential backoff after transient failures. The adapter logs lifecycle status without logging tokens or full auth headers.

Rationale: client machines and network devices may drop idle connections. Passive mode must recover without manual tunnel restarts.

Alternative considered: fail fast and rely on systemd or Docker restart policies. This handles process crashes but not routine network blips.

### Agentis routes passive adapters through an active connection registry

Agentis adds a passive adapter transport path alongside the existing HTTP `call_jsonrpc` path. For adapters configured as passive, `start`, `add_message`, `question`, and `abort` dispatch through the active WebSocket connection for that adapter identity. If no active connection exists, Agentis records a dispatch failure and returns the same kind of user-visible adapter error it uses for HTTP transport failures.

Rationale: the adapter cannot accept inbound requests in passive mode, so routing must happen where the run is initiated.

Alternative considered: have the adapter poll Agentis for pending work. This avoids WebSockets but increases latency, creates duplicate delivery concerns, and is less direct for interactive question replies.

## Risks / Trade-offs

- WebSocket connection drops during a long `start` call -> Execute each inbound request in a task and send exactly one response when the service call completes; Agentis should treat connection loss before response as an unknown/failed dispatch and allow retry according to existing run semantics.
- Duplicate connections for the same adapter identity -> Agentis should allow only one active connection per adapter identity or deterministically replace the old connection and mark it disconnected.
- Passive routing hides the adapter's local health endpoint from Agentis -> Agentis should track connection heartbeat/last-seen status instead of relying on HTTP health checks for passive adapters.
- Long-running adapter calls can block the event loop if executed directly -> The WebSocket dispatcher should call synchronous service handlers via `asyncio.to_thread`, matching the current HTTP route behavior.
- New dependency increases packaging surface -> Use a small, Python 3.13 compatible WebSocket client dependency and cover it with unit tests around the transport boundary.
- Security regressions through token logging -> Keep auth out of query strings and reuse existing log sanitization patterns for request params and exceptions.

## Migration Plan

1. Add the passive transport implementation while leaving HTTP as the default.
2. Add Agentis Cloud support for passive adapter connections and passive adapter routing.
3. Configure one non-critical adapter with passive transport and verify `start`, feedback messages, question replies, abort, and close behavior.
4. Roll out passive transport to client machines by setting the WebSocket endpoint and adapter identity, without requiring inbound firewall or tunnel setup.
5. Roll back by switching the adapter transport back to HTTP and using the existing adapter URL/tunnel path.

## Open Questions

- The stable adapter identity should preferably use the Agentis adapter entity ID. If that is not available on the client at install time, introduce a generated connection key stored in Agentis adapter options.
- The exact Agentis Cloud WebSocket path can be implementation-specific, but it should be separate from the existing JSON-RPC `/api` HTTP endpoint to keep auth and connection lifecycle handling explicit.
