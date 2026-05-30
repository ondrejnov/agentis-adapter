import base64
from pathlib import Path

from common.artifacts.screenshots import collect_screenshot_images


def test_collect_screenshot_images_reads_images_from_project_root(tmp_path):
    screenshots = tmp_path / ".screenshots"
    screenshots.mkdir()
    (screenshots / "b.jpg").write_bytes(b"jpg-data")
    (screenshots / "a.png").write_bytes(b"png-data")
    (screenshots / "recording.webm").write_bytes(b"webm-data")
    (screenshots / "notes.txt").write_text("ignored", encoding="utf-8")

    assert collect_screenshot_images(tmp_path) == [
        {"name": "a.png", "content": base64.b64encode(b"png-data").decode("ascii")},
        {"name": "b.jpg", "content": base64.b64encode(b"jpg-data").decode("ascii")},
        {"name": "recording.webm", "content": base64.b64encode(b"webm-data").decode("ascii")},
    ]


def test_collect_screenshot_images_returns_empty_when_missing(tmp_path):
    assert collect_screenshot_images(tmp_path) == []


def test_collect_screenshot_images_trims_webm_when_ffmpeg_succeeds(monkeypatch, tmp_path):
    screenshots = tmp_path / ".screenshots"
    screenshots.mkdir()
    recording = screenshots / "recording.webm"
    recording.write_bytes(b"webm-data")

    monkeypatch.setattr("common.artifacts.screenshots.shutil.which", lambda name: "/usr/bin/ffmpeg")

    def fake_run(args, **kwargs):
        output = Path(args[-1])
        output.write_bytes(b"trimmed-webm-data")

        class Result:
            returncode = 0

        return Result()

    monkeypatch.setattr("common.artifacts.screenshots.subprocess.run", fake_run)

    assert collect_screenshot_images(tmp_path) == [
        {"name": "recording.webm", "content": base64.b64encode(b"trimmed-webm-data").decode("ascii")},
    ]


def test_collect_screenshot_images_keeps_webm_when_ffmpeg_fails(monkeypatch, tmp_path):
    screenshots = tmp_path / ".screenshots"
    screenshots.mkdir()
    (screenshots / "recording.webm").write_bytes(b"webm-data")

    monkeypatch.setattr("common.artifacts.screenshots.shutil.which", lambda name: "/usr/bin/ffmpeg")

    class Result:
        returncode = 1

    monkeypatch.setattr("common.artifacts.screenshots.subprocess.run", lambda *args, **kwargs: Result())

    assert collect_screenshot_images(tmp_path) == [
        {"name": "recording.webm", "content": base64.b64encode(b"webm-data").decode("ascii")},
    ]
