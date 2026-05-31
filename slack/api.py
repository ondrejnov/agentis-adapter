"""Slack adapter wiring and runtime entrypoint.

Like the other adapters this module exposes ``create_app`` (the FastAPI service
container + ``/health``). It does **not** serve the passive WebSocket transport:
the Slack adapter is an ingestion source, not an agent runtime, so instead of a
``_DISPATCH`` of JSON-RPC methods it exposes :func:`run_adapter`, which the CLI
runs to drive the Slack socket-mode listener.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from common.adapter_app import JsonRpcRoute, create_adapter_app
from common.config import Settings, get_settings
from common.rpc.session_registry import SessionContextRegistry
from slack.agentis_tasks import SlackAgentisGateway
from slack.config import SlackSettings
from slack.listener import SlackMentionService


# The Slack adapter receives no inbound Agentis JSON-RPC (runs are dispatched to
# the execution adapter named on the task, e.g. ``claude``), so the dispatch map
# is empty. It is kept for parity with the other ``*.api`` modules.
_DISPATCH: dict[str, JsonRpcRoute] = {}


def _configure_services(app: FastAPI, settings: Settings, session_registry: SessionContextRegistry) -> None:
    app.state.slack_settings = SlackSettings.from_env()


def create_app() -> FastAPI:
    return create_adapter_app(
        title="Agentis Slack Adapter",
        settings=get_settings(),
        configure_services=_configure_services,
    )


def run_adapter(*, settings: Settings, service_container: Any) -> None:
    """Start the Slack socket-mode listener (blocking).

    ``slack_bolt`` is imported lazily so the package (and its tests / ``/health``
    probe) does not require the Slack SDK to be installed.
    """
    from slack_bolt import App
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    slack_settings: SlackSettings = getattr(service_container, "slack_settings", None) or SlackSettings.from_env()
    slack_settings.validate()

    if not settings.agentis_endpoint:
        raise RuntimeError("AGENTIS_ENDPOINT is required for the Slack adapter")

    app = App(token=slack_settings.slack_bot_token)
    bot_user_id = app.client.auth_test().get("user_id")

    gateway = SlackAgentisGateway(endpoint=settings.agentis_endpoint, token=settings.agentis_token)
    service = SlackMentionService(
        settings=slack_settings,
        agentis=gateway,
        slack_client=app.client,
        bot_user_id=bot_user_id,
    )

    @app.event("app_mention")
    def on_app_mention(event, body, ack):  # noqa: ANN001
        ack()
        service.handle_app_mention(event, event_id=body.get("event_id"))

    @app.event("message")
    def on_message(event, ack):  # noqa: ANN001
        ack()
        service.handle_message(event)

    SocketModeHandler(app, slack_settings.slack_app_token).start()
