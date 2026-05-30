from __future__ import annotations

import json
import subprocess
from typing import Any
from urllib import error, request

from common.config import get_settings
from common.models import AdapterOptionsPayload as AdapterOptions
from common.models import AgentExecutionContextPayload as AgentExecutionContext
from common.kubernetes_runtime import KubernetesAdapterService
from claude.activity_mapper import ClaudeActivityMapper
from claude.client import ClaudeCodeClient, ClaudeRunConfig


API_URL = "http://127.0.0.1:8001/api"
REQUEST_ID = 1
TIMEOUT = 300.0


context = AgentExecutionContext(
    run_id="019d9f97-8716-7bda-b500-b8c183ea4bac",
    task_id="019d9f7b-7cdc-7607-96d9-f92d9bb8beb8",
    title="readme",
    description="Udelej README.md pro tento repozitář",
    project_id=1,
    project_title="Projekt Agentis",
    project_slug="agentis",
    project_github_repo="agentis/agentis",
    working_dir="/var/www/feed",
    base_branch="main",
    adapter=AdapterOptions(agent="build", model="github-copilot/gpt-5.4-mini"),
)


def build_payload() -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": REQUEST_ID,
        "method": "start",
        "params": {
            "context": context.model_dump(exclude_none=True),
        },
    }


def send_request(url: str, payload: dict[str, Any], timeout: float) -> tuple[int, dict[str, Any]]:
    body = json.dumps(payload).encode("utf-8")
    http_request = request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )

    try:
        with request.urlopen(http_request, timeout=timeout) as response:
            raw_body = response.read().decode("utf-8")
            return response.status, json.loads(raw_body)
    except error.HTTPError as exc:
        raw_body = exc.read().decode("utf-8")
        try:
            parsed = json.loads(raw_body)
        except json.JSONDecodeError:
            parsed = {"raw": raw_body}
        return exc.code, parsed


def main():
    payload = {
        "timestamp": "2026-04-23T06:47:07.162996+00:00",
        "level": "INFO",
        "message": "Posting adapter event to Agentis",
        "task_id": "019db90e-0a9c-7f50-8e26-2409716cb269_",
        "run_id": "019db90e-10de-7ff5-a9c3-e232e460c5d3_",
        "kind": "wait_ready",
        "status": "started",
        "event_message": "Čekám na inicializaci podu.",
    }
    context = AgentExecutionContext(
        run_id="019db90e-10de-7ff5-a9c3-e232e460c5d3",
        task_id="019db90e-0a9c-7f50-8e26-2409716cb269",
        title="readme",
        description="Udelej README.md pro tento repozitář",
        project_id=1,
        project_title="Projekt Agentis",
        project_slug="agentis",
        project_github_repo="agentis/agentis",
        working_dir="/var/www/feed",
        base_branch="main",
        adapter=AdapterOptions(agent="build", model="github-copilot/gpt-5.4-mini"),
    )
    settings = get_settings()
    a = KubernetesAdapterService(context, settings)
    a._call_agentis_rpc("run.adapter_event", payload)


async def run_claude() -> None:
    prompt = "vypis git status"
    cwd = "/var/www/agentis-kubernetes-adapter"
    config = ClaudeRunConfig(model="claude-haiku-4-5-20251001", dangerously_skip_permissions=False)
    client = ClaudeCodeClient(cwd=cwd, config=config)
    mapper = ClaudeActivityMapper(prompt=prompt, mode="build", agent="claude", cwd=cwd)

    async for event in client.stream(prompt=prompt):
        if not mapper.consume(event):
            continue
        # Ekvivalent OpenCode `session.idle` payloadu — lze rovnou poslat na
        # `session.store_activity_log`.
        payload = {
            "session_id": mapper.session_id,
            "messages": mapper.snapshot(),
        }
        print(
            f"[{event.type}] msgs={len(payload['messages'])}, parts={sum(len(m['parts']) for m in payload['messages'])}"
        )
        # internal.session_idle → store_activity_log:
        # rpc.call("session.store_activity_log", payload)

    print("---- FINAL TRANSCRIPT ----")
    print(json.dumps(mapper.snapshot(), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    opencode_output = subprocess.run(
        ["/usr/bin/opencode", "run", "--model", "openai/gpt-5.4", "hi"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    print(opencode_output)
