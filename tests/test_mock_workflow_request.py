from __future__ import annotations

from scripts.mock_workflow_request import build_payload, parse_args


def test_build_payload_defaults_to_safe_local_test_workflow(tmp_path):
    args = parse_args(["--working-dir", str(tmp_path), "hello from slack"])

    payload = build_payload(args)

    context = payload["params"]["context"]
    assert payload["method"] == "start"
    assert context["user_prompt"] == "hello from slack"
    assert context["working_dir"] == str(tmp_path)
    assert context["headers"] is None
    assert context["adapter"] == {
        "scope": "project",
        "runtime": "local",
        "agent": "build",
        "model": "openai/gpt-5.4-mini",
        "effort": "low",
            "workflow": "test",
    }


def test_build_payload_includes_optional_slack_headers(tmp_path):
    args = parse_args(
        [
            "--working-dir",
            str(tmp_path),
            "--slack-channel",
            "C123",
            "--slack-message-ts",
            "1710000000.000100",
            "--slack-thread-ts",
            "1710000000.000000",
        ]
    )

    payload = build_payload(args)

    assert payload["params"]["context"]["headers"] == {
        "slack_channel": "C123",
        "slack_message_ts": "1710000000.000100",
        "slack_thread_ts": "1710000000.000000",
    }
