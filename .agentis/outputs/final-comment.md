Odstraněno.

Změny:
- Smazán balík `tui/` včetně `tui.app`.
- Odebrán entrypoint `agentis-top` z `pyproject.toml`.
- Odebrána dependency `textual`.
- Přegenerován `poetry.lock`, zmizely Textual/Rich/Markdown transitive závislosti.
- Upravené komentáře a dokumentace, aby už nezmiňovaly TUI ani `agentis-top`.

Ověření:
- `rtk poetry check` prošel.
- `rtk poetry run ruff check .` prošel.
- Cíleně `rtk poetry run pytest -q tests/test_status.py tests/test_cli.py` prošlo.
- Celé `rtk poetry run pytest -q` má 3 pády mimo tuto změnu: `tests/test_mock_workflow_request.py` a `tests/test_source_snapshot.py`.
