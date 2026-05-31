"""Slack ingestion adapter.

Unlike the ``claude``/``opencode`` adapters — which are agent *runtimes* that
receive Agentis JSON-RPC over the passive WebSocket and run a CLI in a worktree
— the Slack adapter is an ingestion *source*. It runs a Slack socket-mode
listener and, whenever the bot is mentioned, turns the mention into an Agentis
task and starts a run on the configured execution adapter (e.g. ``claude``).
"""
