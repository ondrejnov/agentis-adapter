from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from common.models import AgentExecutionContextPayload, ExpectedArtifactPayload


def collect_expected_artifacts(
    context: AgentExecutionContextPayload | None,
    project_root: str | Path | None,
) -> list[dict[str, Any]]:
    if context is None or not project_root:
        return []

    root = Path(project_root).resolve()
    specs = _normalize_expected_artifacts(context.expected_artifacts)
    artifacts: list[dict[str, Any]] = []
    seen: set[Path] = set()

    for spec in specs:
        for path in _resolve_expected_paths(root, spec.path):
            if path in seen or not path.is_file():
                continue
            seen.add(path)
            artifacts.append(
                {
                    "name": spec.name or path.relative_to(root).as_posix(),
                    "filename": spec.filename or path.name,
                    "content": base64.b64encode(path.read_bytes()).decode("ascii"),
                }
            )

    return artifacts


def _normalize_expected_artifacts(value: Any) -> list[ExpectedArtifactPayload]:
    if value is None:
        return []

    raw_items: list[Any]
    if isinstance(value, list):
        raw_items = value
    elif isinstance(value, dict):
        items = value.get("items")
        raw_items = items if isinstance(items, list) else [value]
    else:
        return []

    specs: list[ExpectedArtifactPayload] = []
    for item in raw_items:
        try:
            if isinstance(item, str):
                specs.append(ExpectedArtifactPayload(path=item))
            elif isinstance(item, ExpectedArtifactPayload):
                specs.append(item)
            elif isinstance(item, dict):
                specs.append(ExpectedArtifactPayload.model_validate(item))
        except ValueError:
            continue
    return specs


def _resolve_expected_paths(root: Path, pattern: str) -> list[Path]:
    normalized = pattern.strip().lstrip("/")
    if not normalized:
        return []

    has_glob = any(char in normalized for char in "*?[")
    candidates = sorted(root.glob(normalized)) if has_glob else [root / normalized]
    result: list[Path] = []
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved == root or root not in resolved.parents:
            continue
        result.append(resolved)
    return result
