#!/usr/bin/env python3
"""Zvaliduje Agentis workflow YAML stejným loaderem, jaký používá adapter.

Použití (spouštět v poetry prostředí adapteru):
    cd /var/www/agentis-adapter && poetry run python \
        skills/agentis-workflow/scripts/validate_workflow.py <cesta/k/workflow.yaml> [...]

Ověří: YAML syntaxi, `extends`, `uses`, povolené `[%TOKEN%]`, striktní schema, `needs`, `if` podmínky,
followups. Nespouští žádný krok a nekontroluje dostupnost image, envFiles ani nástrojů v image.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Skill žije v repu adapteru (`skills/agentis-workflow/scripts/`), loader se bere odtud.
ADAPTER_DIR = Path(os.environ.get("AGENTIS_ADAPTER_DIR", Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(ADAPTER_DIR))

from common.workflow.schema import INTERPOLATION_ALLOWLIST, load_workflow_file  # noqa: E402

DUMMY_VALUES = {name: f"<{name}>" for name in INTERPOLATION_ALLOWLIST}


def validate(path: Path) -> bool:
    try:
        workflow = load_workflow_file(path, DUMMY_VALUES)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL {path}\n  {type(exc).__name__}: {exc}")
        return False
    spec = workflow.workflow
    print(f"OK   {path}")
    print(f"  executor: {spec.executor or '(default adapteru)'}  image: {spec.image or '-'}")
    for index, step in enumerate(spec.steps, start=1):
        image = step.image or spec.image
        needs = "" if step.needs is None else f" needs={step.needs}"
        cond = f" if={step.if_!r}" if step.if_ else ""
        outputs = ", ".join(o.type for o in step.outputs) or "-"
        print(f"  {index}. {step.name}{needs}{cond} outputs=[{outputs}]")
        if spec.executor != "local" and not image:
            print("     WARN: krok nemá image (kubernetes/docker executor ji vyžaduje)")
    for followup in spec.followups:
        print(f"  followup: {followup.title} -> {followup.workflow}")
    return True


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    results = [validate(Path(arg)) for arg in argv]
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
