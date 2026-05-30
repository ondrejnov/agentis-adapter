## Why

The adapter currently has to be reachable from the internet, often through an SSH tunnel, which makes every client installation harder to operate and exposes an inbound port. A passive WebSocket mode lets each adapter initiate an outbound connection to Agentis Cloud, so client machines do not need a public IP address, ingress, or a manually maintained tunnel.

## What Changes

- Add a passive adapter runtime mode where the adapter connects outbound to Agentis Cloud over WebSocket.
- Use the WebSocket connection as a JSON-RPC proxy so Agentis can route `start`, `add_message`, `question`, `approve`, `git_merge`, `abort`, `close`, and compatible internal/session traffic to the connected adapter.
- Keep the existing HTTP JSON-RPC API available for current deployments; passive WebSocket mode is additive and not a breaking change.
- Add connection identity, authentication, heartbeat, reconnect, and request/response correlation behavior for the long-lived WebSocket transport.
- Add configuration for enabling passive mode and selecting the WebSocket endpoint while preserving existing adapter settings for Kubernetes, worktrees, manifests, and Agentis authentication.

## Capabilities

### New Capabilities
- `passive-websocket-transport`: Adapter-initiated WebSocket transport that proxies Agentis JSON-RPC requests through an outbound connection instead of requiring inbound access to the adapter.

### Modified Capabilities

None.

## Impact

- Adapter CLI and configuration gain a passive/WebSocket mode and WebSocket endpoint settings.
- Runtime code gains a WebSocket client loop with authentication, heartbeats, reconnect/backoff, and JSON-RPC request dispatch into existing services.
- JSON-RPC service behavior remains the source of truth; transport-specific code should reuse the same dispatch, validation, error codes, and secret-scrubbing rules as HTTP.
- Tests need coverage for WebSocket message dispatch, reconnection/error handling, authentication/configuration, and that existing HTTP API behavior remains unchanged.
- Packaging dependencies may need a WebSocket client library compatible with Python 3.13 and the current FastAPI/Uvicorn stack.
