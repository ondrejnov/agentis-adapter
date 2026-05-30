from __future__ import annotations

import uvicorn

from common.config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run("opencode.api:app", host=settings.host, port=settings.port, reload=False)
