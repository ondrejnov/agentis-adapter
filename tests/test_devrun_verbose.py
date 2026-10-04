import io

from common.workflow.devrun import ConsoleReporter


def _event(status: str, job: str = "step-1") -> dict:
    return {"kind": "workflow_step", "status": status, "message": "Send", "data": {"step": "Send", "step_index": 1, "job": job}}


def test_verbose_prints_step_log_on_success(tmp_path) -> None:
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "step-1.log").write_text("newsletter odeslán\n", encoding="utf-8")
    out = io.StringIO()
    reporter = ConsoleReporter(out, color=False, verbose=True, log_dir=lambda: tmp_path)

    reporter("run.adapter_event", _event("started"))
    reporter("run.adapter_event", _event("success"))

    assert "│ newsletter odeslán" in out.getvalue()


def test_step_log_hidden_without_verbose(tmp_path) -> None:
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "step-1.log").write_text("secret line\n", encoding="utf-8")
    out = io.StringIO()
    reporter = ConsoleReporter(out, color=False, log_dir=lambda: tmp_path)

    reporter("run.adapter_event", _event("success"))

    assert "secret line" not in out.getvalue()
