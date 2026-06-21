Opraveno.

Změna:
- `common/agentis_telemetry.py`: `agentiscode` už neposílá `run.adapter_event(kind="idle")`, pokud běží s externím `--run-id` z workflow.
- Samostatně spuštěný `agentiscode`, který si run založí přes `task.start_run`, se dál ukončuje jako dřív.
- `WorkflowManager` tak zůstává jediný, kdo workflow run uzavře, a dělá to až po `_apply_outputs()` / závěrečném `task.add_agent_comment`.

Test:
- Přidán `test_telemetry_does_not_finish_existing_run_id`.
- `poetry run pytest tests/test_agentis_telemetry.py -q` prošlo: `12 passed`.
- `poetry run ruff check common/agentis_telemetry.py tests/test_agentis_telemetry.py` prošlo.
- `poetry run ruff check .` prošlo.
- `poetry run pytest -q` má 3 existující/nepříbuzná selhání mimo změnu: `test_mock_workflow_request.py` očekává `slack` vs aktuální `test`, a `test_source_snapshot.py` očekává `_clone_file`, který v modulu není.
