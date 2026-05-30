from __future__ import annotations

import shlex
from pathlib import Path
from typing import Sequence


LOCAL_SETUP_SCRIPT = Path(".agentis/local-setup.sh")


def build_local_setup_shell_command(argv: Sequence[str]) -> str:
    command = " ".join(shlex.quote(arg) for arg in argv)
    setup_script = shlex.quote(str(LOCAL_SETUP_SCRIPT))
    return f"if [ -f {setup_script} ]; then . {setup_script}; fi\nexec {command}"
