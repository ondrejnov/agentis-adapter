"""Deklarativní agentní krok workflow (`steps[].agent`).

Krok s `agent:` popisuje volání `agentiscode` daty (adapter, model, prompt,
vstupy, JSON Schema výstupu) místo ručně psaného bash skriptu. Při validaci
`WorkflowSpec` se z něj vygeneruje `run` skript, takže runtime (Kubernetes,
Docker, local) běží beze změny a vidí obyčejný bash krok.

Hodnoty ze specifikace se do skriptu vkládají jako bash slova v uvozovkách.
Expandují se jen odkazy na env proměnné `$NAME`, `${NAME}` a `${NAME:-text}`;
cokoli jiného (`$(…)`, backticky, uvozovky) zůstane doslovným textem — hodnota
může obsahovat interpolovaný token jako `[%TASK_TITLE%]`. Inline `prompt` se
neexpanduje vůbec (quoted heredoc).
"""

from __future__ import annotations

import json
import re
import shlex

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: Odkaz na env proměnnou, který se v hodnotách `agent:` kroku expanduje.
_ENV_REF_RE = re.compile(
    r"\$(?:\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)(?::-(?P<default>[^}$`\"\\]*))?\}|(?P<bare>[A-Za-z_][A-Za-z0-9_]*))"
)
_DOUBLE_QUOTE_SPECIALS = re.compile(r'([\\"$`])')


class WorkflowAgentInput(BaseModel):
    """Soubor připojený za prompt agenta (data, historie, …)."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1)
    #: Text vypsaný před obsahem souboru (např. „Položky:“).
    label: str | None = None
    #: Chybějící nebo prázdný soubor se přeskočí (jinak krok selže).
    optional: bool = False


class WorkflowAgent(BaseModel):
    """Parametry `agentiscode` volání; z nich se generuje `run` skript kroku."""

    model_config = ConfigDict(extra="forbid")

    adapter: str = Field(min_length=1)
    model: str | None = None
    effort: str | None = None
    #: `AGENTIS_ADAPTER` / `AGENTIS_MODEL` / `AGENTIS_EFFORT` z tasku přepisují hodnoty výše.
    taskOverrides: bool = True
    #: Inline prompt (bez expanze proměnných). Bez `prompt` i `promptFile` se použije prompt tasku.
    prompt: str | None = None
    promptFile: str | None = None
    inputs: list[WorkflowAgentInput] = Field(default_factory=list)
    #: JSON Schema finální odpovědi (`agentiscode --output-schema`).
    outputSchema: str | None = None
    #: Kolik opravných turnů (`--resume`) smí agent dostat při nevalidní odpovědi.
    schemaRetries: int | None = Field(default=None, ge=0)
    #: Soubor s finální odpovědí (`--final-output`); se schématem obsahuje zvalidovaný JSON.
    output: str | None = None
    sessionOutput: str | None = None
    #: Titulek session v Agentisu; default je jméno kroku.
    sessionTitle: str | None = None
    #: Argumenty předané runtime CLI beze změny (`--extra-args-json`).
    extraArgs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_prompt_and_schema(self) -> WorkflowAgent:
        if self.prompt is not None and self.promptFile is not None:
            raise ValueError("agent step accepts either 'prompt' or 'promptFile', not both")
        if self.schemaRetries is not None and self.outputSchema is None:
            raise ValueError("agent step 'schemaRetries' requires 'outputSchema'")
        return self


def shell_word(value: str) -> str:
    """Bash slovo v uvozovkách, ve kterém se expandují jen odkazy na env proměnné."""

    parts: list[str] = []
    pos = 0
    for match in _ENV_REF_RE.finditer(value):
        parts.append(_DOUBLE_QUOTE_SPECIALS.sub(r"\\\1", value[pos : match.start()]))
        name = match.group("braced") or match.group("bare")
        default = match.group("default")
        parts.append(f"${{{name}:-{default}}}" if default is not None else f"${{{name}}}")
        pos = match.end()
    parts.append(_DOUBLE_QUOTE_SPECIALS.sub(r"\\\1", value[pos:]))
    return '"' + "".join(parts) + '"'


def _optional_word(value: str | None) -> str:
    return shell_word(value) if value is not None else '""'


def _heredoc_delimiter(text: str) -> str:
    lines = set(text.splitlines())
    delimiter = "AGENTIS_PROMPT"
    index = 0
    while delimiter in lines:
        index += 1
        delimiter = f"AGENTIS_PROMPT_{index}"
    return delimiter


def render_agent_run(agent: WorkflowAgent, *, step_name: str) -> str:
    """Vygeneruje bash `run` skript, který spustí `agentiscode` podle `agent:` kroku."""

    lines = [
        f"# Vygenerováno z `agent:` kroku {step_name!r}.",
        f"agent_adapter={shell_word(agent.adapter)}",
        f"agent_model={_optional_word(agent.model)}",
        f"agent_effort={_optional_word(agent.effort)}",
    ]
    if agent.taskOverrides:
        lines += [
            'agent_adapter="${AGENTIS_ADAPTER:-$agent_adapter}"',
            'agent_model="${AGENTIS_MODEL:-$agent_model}"',
            'agent_effort="${AGENTIS_EFFORT:-$agent_effort}"',
        ]

    lines.append("{")
    if agent.prompt is not None:
        delimiter = _heredoc_delimiter(agent.prompt)
        lines += [f"cat <<'{delimiter}'", agent.prompt.rstrip("\n"), delimiter]
    else:
        prompt_file = shell_word(agent.promptFile) if agent.promptFile is not None else '"$AGENTIS_PROMPT_FILE"'
        lines.append(f"cat {prompt_file}")
    for item in agent.inputs:
        path = shell_word(item.path)
        label = f"printf '\\n\\n%s\\n' {shell_word(item.label)}" if item.label else "printf '\\n\\n'"
        if item.optional:
            lines.append(f"if [ -s {path} ]; then {label}; cat {path}; fi")
        else:
            lines += [label, f"cat {path}"]

    args = [
        '} | agentiscode --adapter "$agent_adapter" --json',
        '${agent_model:+--model "$agent_model"}',
        '${agent_effort:+--effort "$agent_effort"}',
        '${AGENTIS_RUN_ID:+--run-id "$AGENTIS_RUN_ID"}',
        '${AGENTIS_TASK_ID:+--task-id "$AGENTIS_TASK_ID"}',
        f"--session-title {shell_word(agent.sessionTitle) if agent.sessionTitle else shlex.quote(step_name)}",
    ]
    if agent.outputSchema is not None:
        args.append(f"--output-schema {shell_word(agent.outputSchema)}")
    if agent.schemaRetries is not None:
        args.append(f"--schema-retries {agent.schemaRetries}")
    if agent.output is not None:
        args.append(f"--final-output {shell_word(agent.output)}")
    if agent.sessionOutput is not None:
        args.append(f"--session-output {shell_word(agent.sessionOutput)}")
    if agent.extraArgs:
        args.append(f"--extra-args-json {shlex.quote(json.dumps(agent.extraArgs, ensure_ascii=False))}")
    lines.append(" \\\n  ".join(args))
    return "\n".join(lines) + "\n"


__all__ = ["WorkflowAgent", "WorkflowAgentInput", "render_agent_run", "shell_word"]
