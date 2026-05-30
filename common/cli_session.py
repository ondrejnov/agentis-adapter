from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional


_STREAM_READ_CHUNK_SIZE = 64 * 1024


@dataclass
class KubectlExecTarget:
    """Target for running an agent CLI inside a Kubernetes pod via `kubectl exec`."""

    namespace: str
    selector: str = "deployment/opencode"
    container: Optional[str] = "opencode"
    kubectl: str = "kubectl"


def unbounded_line_reader(stream: Any) -> Callable[[], Awaitable[bytes]]:
    """Return a readline-like coroutine that is not capped by StreamReader's line limit."""

    buffer = bytearray()
    eof = False

    async def read_line() -> bytes:
        nonlocal eof

        while True:
            separator_at = buffer.find(b"\n")
            if separator_at >= 0:
                line = bytes(buffer[: separator_at + 1])
                del buffer[: separator_at + 1]
                return line

            if eof:
                if not buffer:
                    return b""
                line = bytes(buffer)
                buffer.clear()
                return line

            chunk = await stream.read(_STREAM_READ_CHUNK_SIZE)
            if chunk:
                buffer.extend(chunk)
            else:
                eof = True

    return read_line


__all__ = ["KubectlExecTarget", "unbounded_line_reader"]
