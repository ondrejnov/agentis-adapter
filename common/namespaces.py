"""Mapování Agentis kontextu na Kubernetes namespace a ingress URL.

Workflow runtime spouští kroky jako Kubernetes Joby v namespace odvozeném
z task/projekt kontextu a dev server je vystavený na ingress hostu se stejným
odvozením. Žádný deploy ani manifest se zde neřeší — jen pojmenování.
"""

from __future__ import annotations

import re
import unicodedata

from common.config import Settings
from common.models import AgentExecutionContextPayload

INGRESS_DOMAIN_SUFFIX = "dev.agentis.cz"


def _is_project_scope(context: AgentExecutionContextPayload) -> bool:
    return bool(context.adapter and context.adapter.scope == "project")


def kubernetes_safe_name(value: str) -> str:
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    sanitized = re.sub(r"[^a-z0-9-]+", "-", ascii_value.lower().strip())
    return re.sub(r"-{2,}", "-", sanitized).strip("-")


def namespace_for_context(context: AgentExecutionContextPayload, settings: Settings) -> str:
    if context.namespace and context.namespace.strip():
        return context.namespace.strip()
    if _is_project_scope(context):
        project_name = kubernetes_safe_name(context.project_slug or context.project_title or "")
        if not project_name:
            raise RuntimeError("project_slug cannot be converted to a Kubernetes namespace")
        namespace = f"project-{project_name}"
        return namespace[:63].strip("-")
    if context.task_number is None:
        namespace = kubernetes_safe_name(context.task_id)
        if not namespace:
            raise RuntimeError("task_id cannot be converted to a Kubernetes namespace")
        return namespace

    prefix = kubernetes_safe_name(settings.namespace_prefix)
    title = kubernetes_safe_name(context.title[:20])
    namespace = "-".join(part for part in (prefix, str(context.task_number), title) if part)
    if not namespace:
        raise RuntimeError("namespace cannot be empty")
    return namespace[:63].strip("-")


def _ingress_host(namespace: str, *, prefix: str | None = None) -> str:
    domain_suffix = f".{INGRESS_DOMAIN_SUFFIX}"
    if prefix:
        return f"{prefix}-{namespace}{domain_suffix}"
    return f"{namespace}{domain_suffix}"


def dev_server_url_for_context(context: AgentExecutionContextPayload, settings: Settings) -> str:
    namespace = namespace_for_context(context, settings)
    return f"http://{_ingress_host(namespace, prefix='app')}"


__all__ = [
    "INGRESS_DOMAIN_SUFFIX",
    "dev_server_url_for_context",
    "kubernetes_safe_name",
    "namespace_for_context",
]
