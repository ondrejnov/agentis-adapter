# Jak funguje Agentis adapter

## K čemu adapter slouží

Adapter je most mezi ticket systémem **Agentis** a CLI coding agenty (Claude Code, OpenCode, sjednocený wrapper `agentiscode`). Přijímá od Agentisu JSON-RPC příkazy (`start`, `add_message`, `abort`, `undo`), pro task připraví git worktree, spustí v něm agenta a průběžně streamuje jeho aktivitu zpět do Agentisu. Po doběhnutí agenta zapíše do tasku completion komentář s přílohami a nabídkou followup akcí. Commit změn a založení GitHub PR dělá jen workflow runtime (kroky ve workflow YAML), lokální sessions ne.

Klíčové zdrojáky:

| Soubor | Role |
| --- | --- |
| `app/cli.py` | Entrypoint `agentis-adapter` — výběr adapteru, spuštění transportů |
| `common/rpc/passive_websocket.py` | Pasivní WebSocket transport k Agentisu (příjem JSON-RPC) |
| `common/rpc/dispatcher.py` | JSON-RPC dispatch — validace, mapování metod, chybové kódy |
| `common/rpc/jsonrpc.py` | `AgentJsonRpcService` — logika metod `start`/`add_message`/`abort`/`undo` |
| `common/adapter_base.py` | `BaseAdapterService` — lifecycle agenta + reporting do Agentisu |
| `common/git_adapter.py` | `GitAdapterService` — worktree a branch per task |
| `common/cli_adapter.py` | `CliAdapterService` — sdílený lifecycle lokálních CLI adaptérů |
| `common/session_manager.py` | `BaseSessionManager` — background běh CLI agenta a streaming aktivity |
| `common/agentis.py` | `AgentisJsonRpcClient` — HTTP JSON-RPC klient na Agentis backend |
| `common/workflow/` | Workflow režim (viz [docs/workflow.md](workflow.md)) |

## Architektura v kostce

```
            JSON-RPC příkazy (start, add_message, abort, undo)
Agentis ────────────── wss (pasivní WebSocket) ──────────────► agentis-adapter
backend ◄───────────── HTTP JSON-RPC (AgentisJsonRpcClient) ──┤
            run.adapter_event, session.store_activity_log,    │
            task.add_agent_comment, run.store_session_id, …    │
                                                               │
                       ┌───────────────────────────────────────┤
                       ▼                                       ▼
              local CLI runtime                        workflow runtime
        (session manager + CLI proces                (WorkflowManager —
         ve worktree tasku:                           kroky jako K8s Joby
         claude / opencode / agentiscode)             nebo lokální bash)
```

Důležité vlastnosti:

- **Spojení iniciuje adapter** — drží outbound WebSocket na Agentis (`AGENTIS_WS_ENDPOINT`), Agentis do adapteru nevolá žádné HTTP. Adapter tak může běžet za NATem.
- **HTTP server adapteru je jen observabilita** — `/health`, `/status`, `/log`, `/runs/{run_id}/log` pro lokální TUI `agentis-top`. Žádné JSON-RPC přes HTTP.
- **Stav je in-memory** — registry sessions a runů nepřežijí restart procesu, záměrně bez perzistence.

## Vstupní body

| Příkaz | Co dělá |
| --- | --- |
| `agentis-adapter --adapter <name> [--id <adapter-id>]` | Spustí adapter proces: FastAPI app (observabilita) + WebSocket transport |
| `agentiscode …` | Samostatný CLI wrapper nad OpenCode/Claude Code (viz níže) |
| `agentis-top` | Textual TUI dashboard nad `/status` a `/log` endpointy adapteru |

`--adapter` vybírá modul z `app/cli.py:_ADAPTER_MODULES`:

| Adapter | Modul | Agent |
| --- | --- | --- |
| `agentiscode` | `agentiscode.api` | CLI `agentiscode` (wrapper, sám si volí opencode/claude) |
| `claude` / `claudecode` | `claude.api` | `claude --print --output-format stream-json` |
| `claude-p` | `claude_p.api` | `claude-p ... --output-format stream-json` — stejný engine jako `claude`, ale prokládaný transkript na výstupu (`ClaudePClient` ho normalizuje na stejné eventy) |
| `opencode` | `opencode.api` | `opencode run --format json` |
| `slack` | `slack.api` | Pozůstatek — modul Slack ingestion adapteru byl z repa odstraněn; Slack integrace dnes běží přes workflow `.agentis/workflows/slack.yaml` + `scripts/slack_stream.py` |

Každý modul `*.api` definuje `create_app()` (FastAPI app se službami na `app.state`) a tabulku `_DISPATCH` (JSON-RPC metody → handler). Všechny agentí adaptéry sdílí stejnou `AgentJsonRpcService`; liší se jen `adapter_factory` (která třída adapteru a session manageru se použije). CLI navíc podporuje ingestion adaptéry s vlastní foreground smyčkou (hook `run_adapter` místo WebSocket transportu) — tasky do Agentisu posílají, JSON-RPC nepřijímají.

## Transport: pasivní WebSocket

`PassiveWebSocketClient` (`common/rpc/passive_websocket.py`):

- připojuje se na `AGENTIS_WS_ENDPOINT` s hlavičkami `Authorization: Bearer <AGENTIS_TOKEN>` a `X-Agentis-Adapter-Id: <AGENTIS_ADAPTER_ID>`; pro ne-localhost vyžaduje `wss://`,
- každou přijatou zprávu parsne jako JSON-RPC 2.0, zvaliduje parametry přes Pydantic model z `_DISPATCH` a handler spustí v threadu (`asyncio.to_thread`); odpověď posílá zpět jen pokud request měl `id`,
- při výpadku reconnectuje s exponenciálním backoffem (konfigurovatelné `AGENTIS_WS_RECONNECT_*`),
- **graceful shutdown**: první SIGTERM/SIGINT zavře WebSocket (žádné nové zprávy), rozpracovaný dispatch doběhne a pak se čeká na běžící agenty a workflow až `ADAPTER_SHUTDOWN_GRACE_PERIOD` sekund (0 = bez limitu). Druhý signál ukončí proces okamžitě.

## JSON-RPC metody

| Metoda | Parametry | Co dělá |
| --- | --- | --- |
| `start` | `context` (+ `fork_from_session_id`) | Připraví worktree a spustí agenta / workflow; vrací `run` + provedené adapter kroky |
| `add_message` | `run_id`, `context`, `message`, `attachments` | Pošle follow-up prompt do existující session (`--resume`), resp. spustí workflow run nad zprávou |
| `abort` | `context` | Zabije běžící CLI session (celou process group), resp. zruší běžící workflow |
| `undo` | `context` | Vrátí worktree do source snapshotu pořízeného před posledním během |

Chyby vrací `AgentJsonRpcException` s kódem, který dispatcher mapuje na HTTP-like status (`404` → not found, `>=500`/`-32603` → internal, jinak 400). Nevalidní parametry = standardní `-32602 Invalid params`.

Centrální vstup do metod je `AgentJsonRpcService` (`common/rpc/jsonrpc.py`). Ta na začátku každé metody rozhodne mezi dvěma běhovými režimy (viz níže) a u local runtime postupně volá lifecycle kroky adapteru přes `_run_adapter_step()` — každý krok hlásí `started`/`success`/`failed` event do Agentisu (`run.adapter_event`) a do lokálního status registru.

## Dva běhové režimy

### 1. Local CLI runtime (default)

Agent běží jako lokální proces na hostu, řízený session managerem. Lifecycle `start`:

1. **`create_worktree`** — `GitAdapterService` založí (nebo znovu použije) git worktree `<ADAPTER_WORKTREE_ROOT>/<task-safe-id>` na větvi `task-<task_id>` (resp. `context.adapter.branch`) z `context.base_branch`. Pro `context.adapter.scope == "project"` se přeskakuje — běží se přímo v adresáři projektu.
2. **`deploy`** + **`wait_ready`** — u lokálních CLI adaptérů no-opy (zůstávají kvůli jednotnému lifecycle), `wait_ready` vrací URL `local://<runtime_label>`.
3. **`start_session`** — `CliAdapterService` složí initial prompt (`user_prompt` + `description` + komentáře tasku + materializované přílohy v bloku `<attachments>`) a předá ho session manageru. Vrácené `session_id` se uloží do Agentisu (`run.store_session_id`) a do lokální `SessionContextRegistry` (spolu se snapshot klíčem pro `undo`).

`add_message` dělá totéž, ale prompt posílá do existující session (`session_id` z kontextu je povinné) — nový běh CLI s `--resume <session_id>`.

### 2. Workflow runtime

Aktivuje se, když `context.adapter.runtime == "workflow"` nebo kontext nese pojmenované workflow `context.adapter.workflow` (followup akce merge/close — ty jdou přes workflow vždy). Adapter pak žádného agenta sám nespouští: založí worktree, materializuje přílohy a předá řízení `WorkflowManager`u, který na pozadí (daemon thread) vykonává kroky deklarované v `.agentis/workflows/*.yaml` přes zvolený executor (Kubernetes Joby nebo lokální bash). `start`/`add_message` vrací hned, bez `session_id`. Detailně viz [docs/workflow.md](workflow.md).

Per task běží maximálně jedno workflow — souběžný start vrací chybu 409 (busy).

## Session manager — běh agenta a streaming

`BaseSessionManager` (`common/session_manager.py`) je agent-agnostická orchestrace: pro každou session drží jeden řídicí thread, který přes asyncio streamuje výstup CLI agenta. Konkrétní agenti (Claude Code, OpenCode) jen dědí a přepisují hooky (`_AGENT_LABEL`, `_make_mapper`, `_build_client`).

Průběh jednoho běhu:

1. Před spuštěním se pořídí **source snapshot** worktree (klíč se vrací nahoru a slouží metodě `undo`).
2. CLI proces se spustí s `start_new_session=True` (vlastní process group — `abort` pak killuje celou skupinu) a obalený env wrapperem z `local-env.yaml` (`build_local_env_shell_command`, viz Gotchas v CLAUDE.md).
3. Z eventu `session_start` se převezme **skutečné agentí `session_id`** — do té doby je session registrovaná pod pending klíčem; `start` blokuje (max 300 s), dokud session_id nedorazí. Nová session se ohlásí do Agentisu (`session.session_created`).
4. Eventy agenta (text, reasoning, tool cally) průběžně mapuje **activity mapper** na zprávy a posílá je do Agentisu přes `session.store_activity_log`.
5. Po doběhnutí (`_finish_session_actions`, jen pro task scope s GitHub repem) se sestaví přílohy completion komentáře: odkaz na worktree pro IDE (`context.ide`) a diff proti snapshotu. Commit, pull request ani dev server lokální session nedělá — to obstarávají kroky workflow runtime.
6. Finální text agenta se zapíše jako **completion komentář** (`task.add_agent_comment`) se status změnou tasku (`IN_REVIEW`, u project scope `DONE`, lze přepsat `adapter.task_status`), screenshoty, očekávanými artefakty a followup akcemi načtenými z `workflow.followups` sekce workflow YAML.
7. Nakonec odejde adapter event `<label>_idle` s cenou a usage, run se ve status registru označí `success`/`failed`/`aborted`.

### Specifikum `agentiscode` adapteru

`AgentisCodeSessionManager` nepoužívá streaming přes `BaseSessionManager` — spouští CLI `agentiscode --json --task-id … --agentis-api …` a **telemetrii do Agentisu posílá samo CLI** (`common/agentis_telemetry.py`). Session manager jen čte JSON Lines výstup, hlídá session_id/abort a dělá completion akce. Podkladového agenta volí z `context.adapter.runtime`/`model` (`claude` vs. `opencode`).

`agentiscode` je zároveň samostatně použitelný příkaz (`app/agentiscode.py`): sjednocuje `opencode run` a `claude` do jednoho proudu `AgentEvent` (viz docstring `common/agentiscode.py`), bez `--json` chová se unixově (stdout = odpověď, stderr = aktivita).

## Komunikace s Agentisem

Veškerý reporting jde přes `AgentisJsonRpcClient` (HTTP JSON-RPC na `AGENTIS_ENDPOINT`, Bearer `AGENTIS_TOKEN`). Používané metody:

| Metoda | Kdy |
| --- | --- |
| `run.adapter_event` | Průběh lifecycle kroků a běhu agenta (`kind` + `status` started/success/failed) |
| `run.store_session_id` | Po založení session — Agentis si session přiřadí k runu |
| `session.session_created` | První ohlášení nové agentí session |
| `session.store_activity_log` | Průběžný snapshot aktivity agenta (zprávy/tool cally) |
| `task.add_agent_comment` | Completion komentář s přílohami, artefakty, status změnou a followup akcemi |

Selhání reportingu běh agenta neshazuje (best-effort, loguje se na stderr). `agentis_token` se nikdy nevrací v API odpovědích ani nelogu­je (`RunStatePayload.safe_dump()`).

## Konfigurace (env / `.env`)

| Proměnná | Default | Význam |
| --- | --- | --- |
| `AGENTIS_ENDPOINT` | `http://127.0.0.1:8891` | HTTP JSON-RPC endpoint Agentisu |
| `AGENTIS_TOKEN` | `1234` | Bearer token pro HTTP i WebSocket |
| `AGENTIS_WS_ENDPOINT` | — | `ws(s)://` endpoint pro pasivní WebSocket (povinné) |
| `AGENTIS_ADAPTER_ID` | — | Identita adapteru vůči Agentisu (povinné; lze předat `--id`) |
| `ADAPTER_HOST` / `ADAPTER_PORT` | `0.0.0.0` / `8001` | Status HTTP server |
| `ADAPTER_WORKTREE_ROOT` | `<repo>/worktrees` | Kořen pro task worktrees |
| `ADAPTER_PROJECT_RUN_ROOT` | `/tmp/agentis` | Run soubory pojmenovaných workflow (mimo worktree) |
| `ADAPTER_SHUTDOWN_GRACE_PERIOD` | `0` | Sekundy čekání na doběhnutí práce při shutdownu (0 = bez limitu) |
| `WORKFLOW_EXECUTOR` | `kubernetes` | Executor workflow kroků (`kubernetes` / `local`), pokud ho neurčí YAML |
| `KUBECTL_COMMAND` | `kubectl` | Příkaz pro Kubernetes executor |
| `AGENTISCODE_COMMAND` | `agentiscode` | Příkaz CLI wrapperu |
| `AGENTISCODE_ADAPTER` | `opencode` | Default podkladový agent wrapperu |
| `AGENTIS_WS_HEARTBEAT_INTERVAL`, `AGENTIS_WS_MAX_MESSAGE_SIZE`, `AGENTIS_WS_RECONNECT_*` | viz `common/config.py` | Ladění WebSocket transportu |

## Observabilita

- `GET /health` — liveness.
- `GET /status` — snapshot status registru: stav WebSocket spojení, běžící/dokončené runy, statistiky od startu.
- `GET /log?after=&limit=` — globální log adapteru; `GET /runs/{run_id}/log` — log konkrétního runu.
- `agentis-top` — read-only Textual dashboard, který tyhle endpointy polluje.

## Testy

End-to-end testy JSON-RPC chování jdou přes `fastapi.testclient.TestClient` v `tests/test_api.py` (helper v `tests/support.py` routuje payloady na dispatcher, jako by přišly WebSocketem). Workflow režim pokrývá `tests/test_workflow.py`, jednotliví agenti `tests/test_claudecode.py`, `tests/test_opencode.py`, `tests/test_agentiscode*.py`. Spouštění: `poetry run pytest -q` + `poetry run ruff check .`.
