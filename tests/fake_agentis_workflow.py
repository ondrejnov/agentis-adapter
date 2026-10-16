"""Falešná binárka `agentis-workflow` pro testy adapteru.

Mluví stejný CLI kontrakt jako skutečný engine (`check --spec`, `run --spec --events jsonl`, `abort`), ale žádné
kroky nespouští. Chování řídí proměnné prostředí `FAKE_ENGINE_*`, které test nastaví přes `monkeypatch`
(child proces je dědí). Skutečný engine testuje `test_workflow_engine_integration.py`.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

PROTOCOL = 1
STEPS = ["Prepare", "Agent", "Deploy"]
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_INVALID = 2
EXIT_BUSY = 3
EXIT_SIGTERM = 143


def main(argv: list[str]) -> int:
    command = argv[0] if argv else ""
    if command == "check":
        return _check(argv)
    if command == "run":
        return _run(argv)
    if command == "abort":
        return _abort(argv)
    sys.stderr.write(f"fake agentis-workflow: unsupported command {command!r}\n")
    return EXIT_INVALID


def _spec(argv: list[str]) -> dict[str, Any]:
    path = Path(argv[argv.index("--spec") + 1])
    return json.loads(path.read_text(encoding="utf-8"))


def _check(argv: list[str]) -> int:
    mode = os.environ.get("FAKE_ENGINE_CHECK", "ok")
    if mode == "invalid":
        sys.stderr.write("invalid workflow: unknown key 'nope'\n")
        return EXIT_INVALID
    if mode == "busy":
        sys.stderr.write("busy: jobs with the same labels are already running\n")
        return EXIT_BUSY
    spec = _spec(argv)
    summary = {
        "protocol": PROTOCOL,
        "workflow_file": str(Path(spec["search_paths"][0]) / f"{spec['workflow']}.yaml"),
        "executor": spec.get("executor") or spec.get("executor_default"),
        "namespace": spec["namespace"],
        "steps": [{"name": name, "needs": [], "image": None} for name in STEPS],
    }
    sys.stdout.write(json.dumps(summary) + "\n")
    return EXIT_OK


def _emit(event: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _write_result(run_dir: Path, result: dict[str, Any]) -> None:
    target = run_dir / "result.json"
    tmp = run_dir / "result.json.tmp"
    tmp.write_text(json.dumps({"protocol": PROTOCOL, **result}), encoding="utf-8")
    os.replace(tmp, target)


def _run(argv: list[str]) -> int:
    spec = _spec(argv)
    run_dir = Path(spec["run_dir"])
    run_dir.mkdir(parents=True, exist_ok=True)
    scenario = os.environ.get("FAKE_ENGINE_SCENARIO", "success")
    approval = "APPROVED" in spec.get("require_vars", [])
    token_seen = "yes" if os.environ.get("AGENTIS_SERVICE_TOKEN") else "no"

    _emit({"type": "workflow_started", "attempt": "fake", "namespace": spec["namespace"], "executor": "local",
           "steps": STEPS})
    if scenario == "no_result":
        # Ukončení bez result.json (pád enginu) — adapter musí run označit jako failed.
        return EXIT_FAILED
    if scenario == "block":
        return _block(run_dir, spec)
    if scenario == "failed":
        return _failed(run_dir)

    # Neparsovatelný řádek na stdout nesmí zahodit výsledek runu.
    sys.stdout.write("not a json line\n")
    _emit({"type": "step_started", "index": 0, "step": "Prepare", "needs": [], "job": "fake-prepare"})
    _emit({"type": "step_succeeded", "index": 0, "step": "Prepare", "needs": [], "job": "fake-prepare",
           "attempts": 1, "duration_ms": 1, "vars": {}})
    _emit({"type": "step_started", "index": 1, "step": "Agent", "needs": ["Prepare"], "job": "fake-agent"})
    _emit({"type": "step_succeeded", "index": 1, "step": "Agent", "needs": ["Prepare"], "job": "fake-agent",
           "attempts": 1, "duration_ms": 1, "vars": {"STEP_VAR": "x"}})
    _emit({"type": "step_skipped", "index": 2, "step": "Deploy", "needs": ["Agent"], "reason": "condition",
           "condition": "STEP_VAR == 'y'", "vars": {"STEP_VAR": "x"}})
    _emit({"type": "warning", "message": "artifact glob matched no files", "data": {}})

    vars_: dict[str, str] = {"STEP_VAR": "x"}
    outputs: dict[str, Any] = {
        "comments": [{"body": f"Hotovo; token={token_seen}", "status": 4, "author_name": "Bot"}],
        "session_id": None,
        "attachments": [],
        "artifacts": [],
    }
    followups: list[dict[str, Any]] = []
    if approval:
        vars_["APPROVED"] = os.environ.get("FAKE_ENGINE_APPROVED", "1")
        metadata = os.environ.get("FAKE_ENGINE_APPROVED_METADATA")
        if metadata is not None:
            vars_["APPROVED_METADATA"] = metadata
        outputs = {"comments": [], "session_id": None, "attachments": [], "artifacts": []}
    else:
        outputs["session_id"] = "sess-1"
        outputs["attachments"] = [{"label": "Preview", "value": "https://example.test/preview", "type": "url"}]
        artifact_path = os.environ.get("FAKE_ENGINE_ARTIFACT_PATH", "artifact-staging/report.txt")
        target = run_dir / artifact_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("report-body", encoding="utf-8")
        outputs["artifacts"] = [
            {"name": "report.txt", "filename": "report.txt", "path": artifact_path, "size": len("report-body")}
        ]
        followups = [{"title": "Git merge", "prompt": "Merge it", "workflow": "merge", "continue_previous_run": False}]

    _emit({"type": "workflow_finished", "status": "success", "failed_step": None})
    _write_result(run_dir, {"status": "success", "failed_step": None, "vars": vars_, "outputs": outputs,
                            "followups": followups, "warnings": []})
    return EXIT_OK


def _failed(run_dir: Path) -> int:
    _emit({"type": "step_started", "index": 0, "step": "Prepare", "needs": [], "job": "fake-prepare"})
    _emit({"type": "step_succeeded", "index": 0, "step": "Prepare", "needs": [], "job": "fake-prepare",
           "attempts": 1, "duration_ms": 1, "vars": {}})
    _emit({"type": "step_started", "index": 1, "step": "Agent", "needs": ["Prepare"], "job": "fake-agent"})
    _emit({"type": "step_failed", "index": 1, "step": "Agent", "needs": ["Prepare"], "job": "fake-agent",
           "result": "failed", "attempts": 2, "duration_ms": 1, "log_tail": "boom", "continue_on_error": False})
    _emit({"type": "workflow_finished", "status": "failed", "failed_step": "Agent"})
    _write_result(run_dir, {
        "status": "failed",
        "failed_step": "Agent",
        "vars": {},
        "outputs": {"comments": [{"body": "Failure comment", "status": 4, "author_name": None}],
                    "session_id": None, "attachments": [], "artifacts": []},
        "followups": [],
        "warnings": [],
    })
    return EXIT_FAILED


def _block(run_dir: Path, spec: dict[str, Any]) -> int:
    """Běží, dokud nepřijde SIGTERM; pak zapíše `aborted` result (jako skutečný engine)."""

    stopped: list[int] = []

    def on_term(signum: int, _frame: object) -> None:
        stopped.append(signum)

    signal.signal(signal.SIGTERM, on_term)
    _emit({"type": "step_started", "index": 0, "step": "Prepare", "needs": [], "job": "fake-prepare"})
    (run_dir / "ready").write_text("ready", encoding="utf-8")
    while not stopped:
        time.sleep(0.01)
    _emit({"type": "workflow_finished", "status": "aborted", "failed_step": None})
    _write_result(run_dir, {"status": "aborted", "failed_step": None, "vars": {}, "outputs": {}, "followups": [],
                            "warnings": []})
    return EXIT_SIGTERM


def _abort(argv: list[str]) -> int:
    sys.stdout.write("fake abort: " + " ".join(argv[1:]) + "\n")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
