from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePath


@dataclass(frozen=True)
class DeployConfig:
    path: Path
    manifest_path: Path


class DeployConfigError(ValueError):
    pass


def _validate_relative_manifest_path(value: str) -> Path:
    manifest = value.strip()
    if not manifest:
        raise DeployConfigError("deploy manifest path must not be empty")

    path = PurePath(manifest)
    if path.is_absolute() or "\\" in manifest or any(part in {"", ".", ".."} for part in path.parts):
        raise DeployConfigError("deploy manifest path must be a relative path inside the repository")

    return Path(*path.parts)


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _manifest_values(config_text: str) -> dict[tuple[str, ...], str]:
    section_stack: list[tuple[int, str]] = []
    values: dict[tuple[str, ...], str] = {}

    for raw_line in config_text.splitlines():
        line_without_comment = raw_line.split("#", 1)[0].rstrip()
        if not line_without_comment.strip():
            continue

        indent = len(line_without_comment) - len(line_without_comment.lstrip(" "))
        stripped = line_without_comment.strip()
        key, separator, value = stripped.partition(":")
        if not separator:
            continue

        key = key.strip()
        value = value.strip()
        while section_stack and section_stack[-1][0] >= indent:
            section_stack.pop()

        if value:
            if key in {"manifest", "manifest_path"}:
                values[tuple(section for _, section in section_stack) + (key,)] = _unquote(value)
            continue

        section_stack.append((indent, key))

    return values


def _extract_manifest_value(values: dict[tuple[str, ...], str], scope: str) -> str | None:
    candidates = (
        ("deploy", scope, "manifest"),
        ("deploy", scope, "manifest_path"),
        (scope, "manifest"),
        (scope, "manifest_path"),
        ("deploy", "manifest"),
        ("deploy", "manifest_path"),
        ("kubernetes", "manifest"),
        ("kubernetes", "manifest_path"),
        ("manifest",),
        ("manifest_path",),
    )

    for candidate in candidates:
        value = values.get(candidate)
        if value is not None:
            return value

    return None


def load_deploy_config(
    workspace_path: Path,
    config_path: str = ".agentis/deploy.yaml",
    scope: str = "worktree",
) -> DeployConfig | None:
    source = workspace_path / config_path
    if not source.is_file():
        return None

    config_text = source.read_text(encoding="utf-8")
    if not config_text.strip():
        raise DeployConfigError(f"deploy config {source} is empty")

    values = _manifest_values(config_text)
    if not values:
        raise DeployConfigError(f"deploy config {source} must define manifest")

    manifest = _extract_manifest_value(values, scope)
    if manifest is None:
        return None

    manifest_path = workspace_path / _validate_relative_manifest_path(manifest)
    return DeployConfig(path=source, manifest_path=manifest_path)
