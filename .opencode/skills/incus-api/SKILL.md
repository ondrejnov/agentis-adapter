---
name: incus-api
description: Incus hypervisor REST API control, instances, images, profiles, networks, storage, projects, operations, and cluster management. Use when the user asks to manage Incus/LXD-like virtualization through the Incus API, generate API calls, diagnose Incus API responses, or automate hypervisor operations.
---

# Incus API Skill

Use this skill when working with the Incus external REST API to inspect or control an Incus hypervisor, cluster, containers, virtual machines, images, networks, storage, profiles, projects, certificates, or operations.

Primary API reference:
`https://raw.githubusercontent.com/api-evangelist/incus/main/collections/incus.opencollection.json`

## Operating Rules

- Treat Incus as infrastructure control plane access. Prefer read-only discovery before writes.
- Ask for explicit confirmation before destructive or high-impact actions: deleting instances, deleting storage volumes, deleting images, deleting networks, stopping many instances, bulk state changes, evacuating cluster members, replacing certificates, or changing cluster/server configuration.
- Never invent hostnames, tokens, certificates, project names, pool names, network names, or instance names. Discover them or ask the user.
- Do not print private keys, client certificates, trust tokens, join tokens, image secrets, or operation websocket secrets.
- Prefer `PATCH` for partial configuration updates unless the user explicitly wants to replace an entire resource with `PUT`.
- Include `project=<name>` on project-scoped endpoints. Default to `project=default` only when the user has not specified a project and discovery confirms it is acceptable.
- For cluster-specific operations, include `target=<member>` only when the operation is member-specific or the user asks for a specific cluster member.
- For operations returning background operation objects, follow up by polling `/1.0/operations/{uuid}` or waiting on `/1.0/operations/{uuid}/wait`.

## Authentication

Incus commonly uses HTTPS with client certificate authentication.

Expected environment variables for examples:

- `INCUS_BASE_URL`: base URL, for example `https://incus.example.com:8443`
- `INCUS_CERT`: path to client certificate PEM
- `INCUS_KEY`: path to client private key PEM
- `INCUS_CA`: optional CA certificate path if the server certificate is not publicly trusted

Use curl like this:

```bash
curl --silent --show-error \
  --cert "$INCUS_CERT" \
  --key "$INCUS_KEY" \
  --cacert "$INCUS_CA" \
  "$INCUS_BASE_URL/1.0"
```

If `INCUS_CA` is not available, ask before using `--insecure` and explain the risk.

## Response Model

Incus API responses are typically JSON envelopes. Inspect these fields:

- `type`: response type, commonly `sync`, `async`, or `error`
- `status` and `status_code`: human and numeric status
- `metadata`: response payload or operation metadata
- `operation`: URL of a created background operation, commonly `/1.0/operations/{uuid}`
- `error` and `error_code`: error details

When `type` is `async`, extract the operation UUID and monitor it:

```bash
curl --silent --show-error \
  --cert "$INCUS_CERT" \
  --key "$INCUS_KEY" \
  --cacert "$INCUS_CA" \
  "$INCUS_BASE_URL/1.0/operations/$OPERATION_UUID/wait"
```

## Discovery Flow

Start with discovery unless the user gives exact known targets.

1. Check supported API versions: `GET /`
2. Check server environment and auth state: `GET /1.0`
3. Check resources: `GET /1.0/resources`
4. List projects: `GET /1.0/projects?recursion=1`
5. List instances: `GET /1.0/instances?project=<project>&recursion=1`
6. List operations: `GET /1.0/operations?project=<project>`

## Common Endpoints

Server:

- `GET /` lists supported API versions.
- `GET /1.0` returns server environment and configuration.
- `PATCH /1.0` partially updates server configuration.
- `GET /1.0/resources` returns hardware/resource information.
- `GET /1.0/events?project=<project>&type=logging,lifecycle,operation` opens a websocket event stream.

Instances:

- `GET /1.0/instances?project=<project>` lists instance URLs.
- `GET /1.0/instances?project=<project>&recursion=1` lists instance structs.
- `POST /1.0/instances?project=<project>` creates an instance.
- `GET /1.0/instances/{name}?project=<project>` gets an instance.
- `PATCH /1.0/instances/{name}?project=<project>` partially updates instance config.
- `PUT /1.0/instances/{name}/state?project=<project>` changes state.
- `GET /1.0/instances/{name}/state?project=<project>` gets runtime state.
- `POST /1.0/instances/{name}/exec?project=<project>` executes a command.
- `GET /1.0/instances/{name}/files?project=<project>&path=<path>` reads a file.
- `POST /1.0/instances/{name}/snapshots?project=<project>` creates a snapshot.
- `GET /1.0/instances/{name}/snapshots?project=<project>&recursion=1` lists snapshots.
- `POST /1.0/instances/{name}/backups?project=<project>` creates a backup.
- `DELETE /1.0/instances/{name}?project=<project>` deletes an instance.

Images:

- `GET /1.0/images?project=<project>&recursion=1` lists images.
- `POST /1.0/images?project=<project>` adds/imports an image.
- `GET /1.0/images/{fingerprint}?project=<project>` gets image metadata.
- `PATCH /1.0/images/{fingerprint}?project=<project>` updates image metadata.
- `DELETE /1.0/images/{fingerprint}?project=<project>` deletes an image.
- `GET /1.0/images/aliases?project=<project>&recursion=1` lists aliases.
- `POST /1.0/images/aliases?project=<project>` creates an alias.

Profiles:

- `GET /1.0/profiles?project=<project>&recursion=1` lists profiles.
- `POST /1.0/profiles?project=<project>` creates a profile.
- `GET /1.0/profiles/{name}?project=<project>` gets a profile.
- `PATCH /1.0/profiles/{name}?project=<project>` partially updates a profile.
- `DELETE /1.0/profiles/{name}?project=<project>` deletes a profile.

Projects:

- `GET /1.0/projects?recursion=1` lists projects.
- `POST /1.0/projects` creates a project.
- `GET /1.0/projects/{name}` gets a project.
- `PATCH /1.0/projects/{name}` partially updates a project.
- `DELETE /1.0/projects/{name}` deletes a project.

Storage:

- `GET /1.0/storage-pools?recursion=1` lists pools.
- `POST /1.0/storage-pools` creates a pool.
- `GET /1.0/storage-pools/{name}` gets a pool.
- `PATCH /1.0/storage-pools/{name}` partially updates a pool.
- `DELETE /1.0/storage-pools/{name}` deletes a pool.
- `GET /1.0/storage-pools/{pool}/volumes?project=<project>&recursion=1` lists volumes.
- `POST /1.0/storage-pools/{pool}/volumes?project=<project>` creates a custom volume.
- `DELETE /1.0/storage-pools/{pool}/volumes/{type}/{name}?project=<project>` deletes a volume.

Networks:

- `GET /1.0/networks?project=<project>&recursion=1` lists networks.
- `POST /1.0/networks?project=<project>` creates a network.
- `GET /1.0/networks/{name}?project=<project>` gets a network.
- `PATCH /1.0/networks/{name}?project=<project>` partially updates a network.
- `DELETE /1.0/networks/{name}?project=<project>` deletes a network.
- `GET /1.0/network-zones?project=<project>&recursion=1` lists network zones.
- `GET /1.0/network-acls?project=<project>&recursion=1` lists network ACLs.

Certificates:

- `GET /1.0/certificates?recursion=1` lists trusted certificates.
- `POST /1.0/certificates` adds a trusted certificate.
- `GET /1.0/certificates/{fingerprint}` gets a certificate.
- `PATCH /1.0/certificates/{fingerprint}` partially updates a certificate.
- `DELETE /1.0/certificates/{fingerprint}` removes a certificate.

Cluster:

- `GET /1.0/cluster` gets cluster configuration.
- `PATCH /1.0/cluster` partially updates cluster configuration when supported; otherwise use `PUT` only with a full known config.
- `GET /1.0/cluster/members?recursion=1` lists members.
- `POST /1.0/cluster/members` requests a join token.
- `GET /1.0/cluster/members/{name}` gets a member.
- `PATCH /1.0/cluster/members/{name}` partially updates a member.
- `GET /1.0/cluster/members/{name}/state` gets member state.
- `POST /1.0/cluster/members/{name}/state` evacuates or restores a member.
- `DELETE /1.0/cluster/members/{name}` removes a member.

Operations:

- `GET /1.0/operations?project=<project>` lists operations.
- `GET /1.0/operations/{uuid}` gets operation status.
- `GET /1.0/operations/{uuid}/wait` waits for completion.
- `DELETE /1.0/operations/{uuid}` cancels an operation when supported.

## Request Examples

List instances in a project:

```bash
curl --silent --show-error \
  --cert "$INCUS_CERT" \
  --key "$INCUS_KEY" \
  --cacert "$INCUS_CA" \
  "$INCUS_BASE_URL/1.0/instances?project=default&recursion=1"
```

Start an instance:

```bash
curl --silent --show-error \
  --request PUT \
  --cert "$INCUS_CERT" \
  --key "$INCUS_KEY" \
  --cacert "$INCUS_CA" \
  --header 'Content-Type: application/json' \
  --data '{"action":"start","timeout":30,"force":false}' \
  "$INCUS_BASE_URL/1.0/instances/my-instance/state?project=default"
```

Stop an instance cleanly:

```bash
curl --silent --show-error \
  --request PUT \
  --cert "$INCUS_CERT" \
  --key "$INCUS_KEY" \
  --cacert "$INCUS_CA" \
  --header 'Content-Type: application/json' \
  --data '{"action":"stop","timeout":60,"force":false}' \
  "$INCUS_BASE_URL/1.0/instances/my-instance/state?project=default"
```

Create a container from an image alias:

```bash
curl --silent --show-error \
  --request POST \
  --cert "$INCUS_CERT" \
  --key "$INCUS_KEY" \
  --cacert "$INCUS_CA" \
  --header 'Content-Type: application/json' \
  --data '{"name":"demo","type":"container","source":{"type":"image","alias":"images:debian/12"},"profiles":["default"]}' \
  "$INCUS_BASE_URL/1.0/instances?project=default"
```

Create a VM from an image alias:

```bash
curl --silent --show-error \
  --request POST \
  --cert "$INCUS_CERT" \
  --key "$INCUS_KEY" \
  --cacert "$INCUS_CA" \
  --header 'Content-Type: application/json' \
  --data '{"name":"demo-vm","type":"virtual-machine","source":{"type":"image","alias":"images:debian/12"},"profiles":["default"]}' \
  "$INCUS_BASE_URL/1.0/instances?project=default"
```

Patch instance config:

```bash
curl --silent --show-error \
  --request PATCH \
  --cert "$INCUS_CERT" \
  --key "$INCUS_KEY" \
  --cacert "$INCUS_CA" \
  --header 'Content-Type: application/json' \
  --data '{"config":{"limits.cpu":"2","limits.memory":"2GiB"}}' \
  "$INCUS_BASE_URL/1.0/instances/my-instance?project=default"
```

Create a snapshot:

```bash
curl --silent --show-error \
  --request POST \
  --cert "$INCUS_CERT" \
  --key "$INCUS_KEY" \
  --cacert "$INCUS_CA" \
  --header 'Content-Type: application/json' \
  --data '{"name":"snap0","stateful":false}' \
  "$INCUS_BASE_URL/1.0/instances/my-instance/snapshots?project=default"
```

## Python Client Pattern

Use `httpx` with client certificates when generating automation.

```python
import os

import httpx

base_url = os.environ["INCUS_BASE_URL"].rstrip("/")
cert = (os.environ["INCUS_CERT"], os.environ["INCUS_KEY"])
verify = os.environ.get("INCUS_CA", True)

with httpx.Client(base_url=base_url, cert=cert, verify=verify, timeout=30) as client:
    response = client.get("/1.0/instances", params={"project": "default", "recursion": 1})
    response.raise_for_status()
    envelope = response.json()
    instances = envelope.get("metadata", [])
```

For async operations, build a helper that polls the operation URL from `operation` or waits on `/wait`, and fails if the final operation metadata reports an error.

## Payload Guidance

When constructing payloads, use the current Incus API shape from discovery or the linked OpenCollection spec. Common shapes:

Instance state change:

```json
{"action":"start","timeout":30,"force":false}
```

Instance resource limits patch:

```json
{"config":{"limits.cpu":"2","limits.memory":"2GiB"}}
```

Snapshot create:

```json
{"name":"snap0","stateful":false}
```

Profile create:

```json
{"name":"default","config":{},"devices":{}}
```

## Troubleshooting

- `403` or `not authorized`: verify the client certificate is trusted in `/1.0/certificates`.
- TLS verification errors: check `INCUS_CA`, server certificate SANs, and base URL hostname.
- `404` on a resource that exists: check `project`, `all-projects`, and cluster `target` query parameters.
- `412` or ETag/precondition issues: refetch the resource and retry with current state.
- Async operation failed: inspect `/1.0/operations/{uuid}` metadata and related event logs.
- Empty lists: retry with `recursion=1`, the correct `project`, or `all-projects=true` if the endpoint supports it.

## Output Style

When answering Incus API tasks:

- State the endpoint, method, and expected impact.
- Show a ready-to-run request only after required variables are known.
- For risky writes, first show the exact target and ask for confirmation.
- Mention how to verify the result with a follow-up `GET`.
- For automation code, keep credentials in environment variables or secret stores, never inline secrets.
