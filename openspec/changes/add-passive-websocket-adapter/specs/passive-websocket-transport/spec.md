## ADDED Requirements

### Requirement: Passive WebSocket startup mode
The adapter SHALL support a startup mode where it opens an outbound WebSocket connection to Agentis Cloud instead of requiring Agentis to connect to the adapter over inbound HTTP.

#### Scenario: Adapter starts in passive mode
- **WHEN** the adapter is launched with passive WebSocket transport enabled and valid WebSocket configuration
- **THEN** it establishes an outbound WebSocket connection to Agentis Cloud
- **AND** it does not require an externally reachable adapter HTTP port for Agentis commands

#### Scenario: HTTP remains default
- **WHEN** the adapter is launched without passive WebSocket transport enabled
- **THEN** it serves the existing HTTP JSON-RPC API with the same default behavior as before

### Requirement: Authenticated adapter registration
The passive WebSocket connection SHALL authenticate with Agentis Cloud and SHALL identify the adapter instance before Agentis routes JSON-RPC requests to it.

#### Scenario: Valid connection is registered
- **WHEN** the adapter connects with a valid Agentis token and a known adapter identity
- **THEN** Agentis Cloud registers the WebSocket as the active connection for that adapter identity

#### Scenario: Invalid connection is rejected
- **WHEN** the adapter connects without a valid token or without a known adapter identity
- **THEN** Agentis Cloud rejects the connection and routes no adapter requests to it

### Requirement: JSON-RPC proxy over WebSocket
The passive transport SHALL carry the same JSON-RPC 2.0 request objects that the HTTP `/api` endpoint accepts, and the adapter SHALL dispatch them through the same validation and service behavior as HTTP.

#### Scenario: Start request is proxied
- **WHEN** Agentis Cloud sends a JSON-RPC `start` request over the registered WebSocket
- **THEN** the adapter validates the params with the existing `StartParams` model and executes the existing start workflow
- **AND** the adapter sends a JSON-RPC response containing either the start result or the existing JSON-RPC error shape

#### Scenario: Feedback request is proxied
- **WHEN** Agentis Cloud sends `add_message`, `question`, `abort`, `close`, or `git_merge` over the registered WebSocket
- **THEN** the adapter dispatches the request to the same handler used by the HTTP transport for that method

### Requirement: Request response correlation
The passive transport SHALL preserve JSON-RPC request identifiers so Agentis Cloud can correlate each proxied request with exactly one adapter response.

#### Scenario: Response keeps request id
- **WHEN** Agentis Cloud sends a JSON-RPC request with `id` equal to `run-123:start`
- **THEN** the adapter response contains `id` equal to `run-123:start`

#### Scenario: Invalid request returns protocol error
- **WHEN** Agentis Cloud sends an invalid JSON-RPC object over the WebSocket
- **THEN** the adapter sends a JSON-RPC error response using the same error code semantics as the HTTP JSON-RPC endpoint where a response is possible

### Requirement: Reconnect and heartbeat
The passive adapter SHALL keep the WebSocket connection alive with heartbeat behavior and SHALL reconnect automatically after transient disconnects.

#### Scenario: Connection is idle
- **WHEN** no JSON-RPC requests are flowing over the WebSocket for the heartbeat interval
- **THEN** the adapter and Agentis Cloud exchange heartbeat traffic or WebSocket ping/pong frames that keep the connection active

#### Scenario: Connection drops
- **WHEN** the WebSocket connection drops because of a transient network failure
- **THEN** the adapter attempts to reconnect with bounded exponential backoff without requiring manual restart

### Requirement: Passive Agentis routing
Agentis Cloud SHALL route commands for adapters configured with passive WebSocket transport through the active WebSocket connection instead of the adapter HTTP URL.

#### Scenario: Passive adapter is online
- **WHEN** a run is started for an adapter configured as passive and that adapter has an active registered WebSocket connection
- **THEN** Agentis Cloud sends the `start` JSON-RPC request through that WebSocket connection

#### Scenario: Passive adapter is offline
- **WHEN** a run command targets a passive adapter with no active registered WebSocket connection
- **THEN** Agentis Cloud records the dispatch as failed and returns a user-visible adapter transport error

### Requirement: Existing HTTP compatibility
The new passive transport SHALL NOT change the existing HTTP JSON-RPC contract, method names, request schemas, response schemas, JSON-RPC error codes, or HTTP status mappings.

#### Scenario: Existing HTTP request still works
- **WHEN** Agentis sends an HTTP JSON-RPC `start` request to `/api` in the existing transport mode
- **THEN** the adapter returns the same response shape and status mapping as before this change

### Requirement: Secret-safe transport logging
The passive transport SHALL NOT log Agentis tokens, WebSocket authentication headers, or other secrets in normal logs or JSON-RPC error payloads.

#### Scenario: Authentication fails
- **WHEN** a passive WebSocket authentication failure is logged
- **THEN** the log entry includes connection status and adapter identity context where safe
- **AND** the log entry does not include the raw Agentis token or authorization header value

### Requirement: Secure WebSocket endpoint configuration
The passive adapter SHALL use a configured WebSocket endpoint and SHALL require `wss://` for non-localhost production endpoints.

#### Scenario: Production endpoint uses TLS
- **WHEN** passive mode is configured with a non-localhost WebSocket endpoint
- **THEN** the endpoint uses the `wss://` scheme

#### Scenario: Local development endpoint is allowed
- **WHEN** passive mode is configured with a localhost or 127.0.0.1 WebSocket endpoint
- **THEN** the endpoint may use the `ws://` scheme for local development
