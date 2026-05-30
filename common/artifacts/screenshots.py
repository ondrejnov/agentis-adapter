from __future__ import annotations

import base64
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any


_SCREENSHOT_ARTIFACT_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".webm"}


def _read_artifact_bytes(path: Path) -> bytes:
    if path.suffix.lower() == ".webm":
        return _trim_idle_webm_best_effort(path)
    return path.read_bytes()


def _trim_idle_webm_best_effort(path: Path) -> bytes:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return path.read_bytes()

    original = path.read_bytes()
    with tempfile.TemporaryDirectory() as tmpdir:
        output = Path(tmpdir) / path.name
        result = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(path),
                "-vf",
                "mpdecimate,setpts=N/FRAME_RATE/TB",
                "-an",
                "-c:v",
                "libvpx-vp9",
                "-b:v",
                "0",
                "-crf",
                "35",
                str(output),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode != 0 or not output.is_file():
            return original

        trimmed = output.read_bytes()
        return trimmed or original


def collect_screenshot_images(project_root: str | Path | None) -> list[dict[str, Any]]:
    """Return Agentis upload payloads for screenshot image/video artifacts."""
    if not project_root:
        return []

    screenshots_dir = Path(project_root) / ".screenshots"
    if not screenshots_dir.is_dir():
        return []

    images: list[dict[str, Any]] = []
    for path in sorted(screenshots_dir.iterdir(), key=lambda item: item.name.lower()):
        if not path.is_file() or path.suffix.lower() not in _SCREENSHOT_ARTIFACT_EXTENSIONS:
            continue
        images.append(
            {
                "name": path.name,
                "content": base64.b64encode(_read_artifact_bytes(path)).decode("ascii"),
            }
        )
    return images
