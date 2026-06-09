from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any


def _load_cli_module() -> ModuleType:
    module_path = Path(__file__).resolve().parent.parent / "app" / "agentiscode.py"
    spec = importlib.util.spec_from_file_location("_agentiscode_cli_impl", module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load agentiscode CLI from {module_path}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_impl = _load_cli_module()

run: Any = _impl.run
main: Any = _impl.main
JsonRenderer: Any = _impl.JsonRenderer
TextRenderer: Any = _impl.TextRenderer
OutputRecorder: Any = _impl.OutputRecorder


__all__ = ["run", "main", "JsonRenderer", "TextRenderer", "OutputRecorder"]
