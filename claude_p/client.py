"""Async wrapper around the `claude-p` CLI (interleaved-log stream-json).

`claude-p` interně volá stejný Claude Code engine jako `claude` a bere stejné
parametry (`--model`, `--output-format stream-json`, `--verbose`, …), ale jeho
stream-json výstup má jiný tvar: je to **prokládaný transkript** (JSONL řádky
jako v session logu) místo čistého event streamu z `claude --print`.

Konkrétní rozdíly oproti `claude`:

  - Žádný ``system``/``subtype:init`` event. Místo něj přijdou hned na začátku
    ``mode`` a ``permission-mode`` řádky, které nesou ``sessionId`` (camelCase).
  - ``session_id`` chodí jako ``sessionId`` (camelCase) na většině řádků a jen
    ve finálním ``result`` jako ``session_id`` (snake_case).
  - ``model`` není v žádném init eventu — objeví se až v ``message.model`` první
    ``assistant`` zprávy.
  - Navíc chodí čistě transkriptové řádky (``file-history-snapshot``,
    ``attachment``, ``user`` echo promptu jako *string* content,
    ``system``/``stop_hook_summary`` / ``turn_duration``), které nás nezajímají.

Tvar ``assistant`` / ``user`` (tool_result) / ``result`` řádků je naopak
identický s `claude`, takže jejich normalizaci přebíráme beze změny z
:class:`claude.client.ClaudeCodeClient`. Tahle třída jen doplní syntetický
``session_start`` event (jinak by session manager nikdy nedostal session_id a
spadl by na timeoutu) a vytáhne ``model`` z první assistant zprávy.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from claude.client import ClaudeCodeClient, ClaudeEvent, ClaudeRunConfig


class ClaudePClient(ClaudeCodeClient):
    """Tenký async wrapper na CLI `claude-p` (prokládaný transkript)."""

    def __init__(self, config: Optional[ClaudeRunConfig] = None, **kwargs: Any) -> None:
        super().__init__(config, **kwargs)
        # `claude-p` nemá init event — session_start si syntetizujeme sami.
        # Pošleme ho jakmile známe session_id a ještě jednou, jakmile dorazí model
        # (poprvé až v první assistant zprávě), ať mapper stihne stampnout modelID.
        self._session_start_emitted = False
        self._model_emitted = False

    def _normalize(self, event: Dict[str, Any]) -> List[ClaudeEvent]:
        etype = event.get("type")

        # session_id chodí camelCase (`sessionId`) skoro všude, snake_case jen ve `result`.
        sid = event.get("sessionId") or event.get("session_id")
        if sid:
            self.session_id = sid

        if etype in ("mode", "permission-mode"):
            return self._emit_session_start(event)

        if etype == "assistant":
            message = event.get("message")
            if isinstance(message, dict) and message.get("model"):
                self.model = message["model"]
            out = self._emit_session_start(event)
            out.extend(super()._normalize(event))
            return out

        if etype == "system" and event.get("subtype") == "init":
            # Kdyby `claude-p` přece jen poslal klasický init, nech ho zpracovat bázi
            # a syntetický session_start už neposílej.
            self._session_start_emitted = True
            if self.model:
                self._model_emitted = True
            return super()._normalize(event)

        return super()._normalize(event)

    def _emit_session_start(self, event: Dict[str, Any]) -> List[ClaudeEvent]:
        """Vydá ``session_start`` — poprvé se session_id, podruhé až s modelem."""
        if not self.session_id:
            return []
        need = False
        if not self._session_start_emitted:
            self._session_start_emitted = True
            need = True
        if self.model and not self._model_emitted:
            self._model_emitted = True
            need = True
        if not need:
            return []
        return [
            ClaudeEvent(
                "session_start",
                {
                    "session_id": self.session_id,
                    "model": self.model,
                    "cwd": event.get("cwd"),
                },
                raw=event,
            )
        ]


__all__ = ["ClaudePClient", "ClaudeRunConfig", "ClaudeEvent"]
