# Agentis Adapter

Samostatny FastAPI JSON-RPC adapter pro agenta. Projekt nema zadnou databazi a drzi runtime stav pouze v pameti procesu.

## Co umi

- pasivni WebSocket transport prijima JSON-RPC 2.0 metody `start`, `add_message`, `question`, `approve`, `git_merge`, `abort`, `close` z Agentisu pres odchozi spojeni
- aktivitu agenta (komentare, dokoncovaci akce, adapter eventy) adapter cte primo z postupneho vystupu `opencode`/`claude` CLI a forwarduje do endpointu z `AGENTIS_ENDPOINT` — agent runtime uz nedela zadne callbacky zpet do adapteru
- `GET /health` na ASGI aplikaci pro healthcheck (adapter ale neposlucha na zadnem inbound portu)
- `start` vytvori git branch a worktree podle `task_id` a spusti agenta lokalne (environment `local`); s `context.adapter.runtime = "workflow"` se misto toho spusti deklarativni workflow (kroky podle executoru jako Kubernetes Joby pres `kubectl`, nebo jako lokalni bash procesy — viz `WORKFLOW_EXECUTOR`)

## Vyvojarska dokumentace

Podrobny popis architektury, flow adapteru, session lifecycle a checklist pro pridani noveho adapteru je v [`docs/development.md`](docs/development.md).

## Lokalni start

```bash
poetry install
poetry run agentis-adapter --adapter opencode
poetry run agentis-adapter --adapter claude
```

Adaptery:

- `opencode` spousti OpenCode jednorazove (`opencode run <prompt> --format json`), bez web REST API. Streamovany vystup adapter parsuje a forwarduje do Agentisu primo — analogicky k `claude` adapteru.
- `claude` spousti lokalni `claude` CLI.

Pro environment promenne lze vyjit z `.env.example`.

CLI argument `--id` ma prioritu pred `AGENTIS_ADAPTER_ID` z `.env`:

```bash
agentis-adapter --adapter opencode --id dev-opencode
agentis-adapter --adapter claude --id dev-claude
```

### Pasivni WebSocket transport

Adapter se k Agentisu pripojuje odchozim WebSocket spojenim — to je jediny zpusob, jak adapter prijima externi JSON-RPC. Diky tomu nemusi byt adapter dostupny z internetu (zadny inbound port, ingress ani tunel). Nakonfiguruj endpoint, identitu adapteru a token:

```bash
AGENTIS_WS_ENDPOINT=ws://127.0.0.1:8891/api/adapters/passive/ws
AGENTIS_ADAPTER_ID=019e0000-0000-7000-8000-000000000123
AGENTIS_TOKEN=interni-token
poetry run agentis-adapter --adapter opencode
```

V produkci pouzij TLS endpoint:

```bash
AGENTIS_WS_ENDPOINT=wss://agentis.example.com/api/adapters/passive/ws
```

Agentis posila JSON-RPC requesty pres registrovane WebSocket spojeni a adapter vraci odpoved se stejnym `id`. Adapter neposlucha na zadnem inbound portu — externi `POST /api` ani interni `POST /api-internal` endpoint neexistuje. Agent runtime nedela zadne callbacky zpet; adapter cte aktivitu primo z postupneho vystupu CLI.

Pro interni instalaci z git repozitare lze pouzit napr.:

```bash
pipx install git+https://github.com/ondrejnov/agentis-kubernetes-adapter.git
```

## Rate limity provideru

Sber rate limitu a obnova OAuth tokenu byly vyclenene do samostatne aplikace
`agentis-ratelimits` (`/var/www/agentis/ratelimits`). Tento adapter uz usage data
nesynchronizuje.

## Docker

```bash
docker build -t agentis-kubernetes-adapter:latest .
docker run --rm -p 8000:8000 --env-file .env.example agentis-kubernetes-adapter:latest
```

## Produkcni nasazeni

Produkci profil drzi `config/production.env`. Jednoduchy deploy script:

```bash
cp .env.example .env
./scripts/deploy.sh
```

Script umi:

- nacist `.env` a `config/production.env`
- sestavit Docker image adapteru
- volitelne image pushnout do registru (`PUSH_IMAGE=1`)
- vytvorit namespace a nasadit adapter API do Kubernetes

## Konfigurace pres environment

- `AGENTIS_WS_ENDPOINT` WebSocket endpoint Agentisu, napr. lokalne `ws://127.0.0.1:8891/api/adapters/passive/ws`, produkcne `wss://agentis.example.com/api/adapters/passive/ws`
- `AGENTIS_ADAPTER_ID` stabilni identita adapteru; idealne ID adapter entity v Agentisu
- `AGENTIS_WS_HEARTBEAT_INTERVAL` default `30`
- `AGENTIS_WS_MAX_MESSAGE_SIZE` maximalni velikost jedne WebSocket zpravy v bajtech, default `67108864` (64 MiB); zvys pri velkych prilohach
- `AGENTIS_WS_RECONNECT_INITIAL_DELAY` default `1`
- `AGENTIS_WS_RECONNECT_MAX_DELAY` default `30`
- `AGENTIS_WS_RECONNECT_MAX_ATTEMPTS` default `0` znamena neomezene reconnect pokusy
- `ADAPTER_NAMESPACE_PREFIX` default `Task`; pouzije se pro namespace workflow Jobu ve tvaru `<prefix>-<task_number>-<prvnich 20 znaku title>`, vysledek se normalizuje pro Kubernetes DNS label
- `WORKFLOW_EXECUTOR` default `kubernetes`; urcuje, kde bezi kroky workflow runtime (`kubernetes` = Joby pres `kubectl`, `local` = lokalni bash procesy nad worktree); workflow YAML to muze prebit polem `workflow.executor`
- `ADAPTER_WORKSPACE_ROOT` default root tohoto repozitare
- `ADAPTER_MAIN_DIR` default hodnota `ADAPTER_WORKSPACE_ROOT`
- `ADAPTER_PUBLIC_URL` optional verejna nebo clusterova base URL adapteru; pokud chybi, adapter zkusi slozit cluster DNS z `K8S_SERVICE_NAME` a `K8S_NAMESPACE`
- `AGENTIS_ENDPOINT` default `http://10.0.0.205:8891`
- `AGENTIS_TOKEN` default `1234`
- projektovy rezim zapnes pres `context.adapter.scope = "project"`; adapter pouzije namespace podle `project_slug`, aktualni git worktree z `context.working_dir` a nebude vytvaret task branch/worktree
- kdyz `context.working_dir` chybi, adapter vytvori worktree v sourozenecke ceste `../worktree/<task_id>`
- `ADAPTER_LOG_LEVEL` default `info`
- `DOCKER_IMAGE` image name pro deploy script
- `K8S_NAMESPACE` namespace pro deploy script
- `K8S_DEPLOYMENT_NAME` deployment name pro deploy script
- `K8S_SERVICE_NAME` service name pro deploy script
- `K8S_INGRESS_HOST` optional ingress hostname pro deploy script
- `CONTAINER_PORT` default `8000`

## Priklad requestu

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "start",
  "params": {
    "context": {
      "run_id": "run-1",
      "task_id": "task-1",
      "title": "Implementace adapteru",
      "project_slug": "agentis",
      "working_dir": "/var/www/worktree/task-1",
      "adapter": {
        "agent": "build",
        "model": "gpt-5.4"
      }
    }
  }
}
```

## Testy

```bash
poetry run pytest -q
poetry run ruff check .
```
