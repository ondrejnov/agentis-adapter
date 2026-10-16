# Návrh: rozdělení na `agentis-adapter` a `agentis-workflow`

Stav: **implementováno** (2026-10-16). Adapter volá engine výhradně přes CLI; odchylky od návrhu jsou shrnuté v sekci [Implementace a odchylky](#implementace-a-odchylky).

## Cíl

- `agentis-workflow` (`/var/www/agentis-workflow`) — samostatný workflow engine. Spustí deklarativní
  YAML workflow nad adresářem bez jakékoli znalosti Agentisu, WebSocketu, tasků a gitu.
  Kdo chce „statické workflow pro agenta“, nainstaluje jen tohle.
- `agentis-adapter` (tento repozitář) — jen napojení na Agentis: WebSocket + JSON-RPC, příprava
  worktree, překlad Agentis kontextu na vstup enginu, spuštění enginu **přes command line**
  a překlad jeho výsledku zpět na Agentis callbacky.

Závislost je jednosměrná: adapter → engine (binárka + verze protokolu). Engine o adapteru neví.

## Co dnes kde je a kam to půjde

| Dnešní modul | Cíl | Poznámka |
|---|---|---|
| `common/workflow/schema.py` | engine | Nutné odpojit `from common.models import TaskStatus` (status jako string literal). |
| `common/workflow/agent_step.py` | engine | `agentiscode` je samostatné CLI; příkaz konfigurovatelný. |
| `common/workflow/runtime.py`, `local_runtime.py`, `docker_runtime.py` | engine | `Settings` → vlastní `EngineSettings` (kubectl/docker příkaz, default executor). |
| `common/workflow/manager.py` — scheduler (`_run_workflow`, retries, deps, `always`, `continueOnError`, timeouty, abort, `envFiles`, `var` outputs, čtení output souborů, staging artefaktů, `deleteNamespace`) | engine | Jádro nového projektu, ~60 % manageru. |
| `common/workflow/manager.py` — `start_workflow`/`abort`/`approve` bookkeeping per task, výběr souboru workflow, namespace, status registry, `_apply_outputs` → Agentis, `_emit_adapter_event` | adapter | Zůstane jako tenký `WorkflowManager`, který místo threadu se schedulerem drží child proces. |
| `common/workflow/devrun.py` | engine | Stane se z něj `agentis-workflow run` s konzolovým výpisem. V adapteru zůstane max. `workflow run` přes WS payload (nebo zmizí). |
| `common/artifacts/source_snapshot.py`, `work_snapshot.py`, `screenshots.py` | adapter | Changes diff a screenshoty jsou Agentis feature; dělá se před/po běhu enginu. |
| `common/rpc/*`, `app/*`, `git_adapter.py`, `attachments.py`, `prompt_context.py`, `namespaces.py`, `status.py`, `agentis.py`, `models.py`, `shutdown.py` | adapter | Beze změny odpovědnosti. |
| `workflows/default.yaml`, `project.yaml`, `approval*.yaml`, `_base.yaml` | adapter | Agentis-specifické (agentiscode, followupy, approval). Engine dostane `examples/`. |
| `skills/agentis-workflow/`, `vscode-agentis-workflow/`, `docs/workflow.md`, `docs/testovani-workflow.md` | engine | Popisují syntaxi YAML, ne Agentis. Validátor skillu pak volá `agentis-workflow validate`. |
| testy `test_workflow*.py`, `test_workflow_devrun.py`, `test_devrun_verbose.py` | engine (většina) | V adapteru zůstanou testy překladu kontextu → spec a výsledku → Agentis callbacky. |

## CLI kontrakt enginu

Navrhuji jedno vstupní JSON (run spec) místo desítek flagů — adapter ho zapíše do run adresáře.
Pro ruční použití existují zkratky přes flagy, které spec jen skládají.

```bash
# strojové volání z adapteru
agentis-workflow run --spec <run_dir>/spec.json --events jsonl   # události na stdout, výsledek do result.json

# ruční / statické použití bez Agentisu
agentis-workflow run report --workdir . --prompt "..." --var TASK_TITLE="Denní report" --executor local
agentis-workflow validate .agentis/workflows/*.yaml [--json]
agentis-workflow list [--workdir .]
agentis-workflow abort --executor kubernetes --namespace task-12-foo --label agentis.task_id=...   # úklid po restartu
agentis-workflow --version            # obsahuje i verzi protokolu
```

### Vstup: `spec.json`

```jsonc
{
  "protocol": 1,
  "workflow": "default",                         // basename nebo cesta k souboru
  "search_paths": [".agentis/workflows", "/opt/agentis-adapter/workflows"],  // pořadí = precedence
  "workdir": "/worktrees/task-12",
  "run_dir": "/worktrees/task-12/.agentis/runs/0193ab...",
  "output_root": "/worktrees/task-12",           // proti čemu se resolvují bodyFrom/valueFrom/artifact globy
  "prompt_file": "prompt.md",                    // relativně k run_dir; obsah zapisuje volající
  "context_file": "context.json",                // opaque pro engine, jen se vystaví v env
  "executor": "kubernetes",                      // vynucený executor (adapter už vyřešil runtime > YAML > default)
  "executor_default": "kubernetes",              // použije se, když executor není vynucen a YAML nic neříká
  "namespace": "task-12-foo",
  "labels": {"agentis.task_id": "...", "agentis.run_id": "...", "agentis.attempt": "0193ab..."},
  "vars": {"RUN_ID": "...", "TASK_ID": "...", "BRANCH": "...", "TASK_TITLE": "..."},   // interpolační tokeny
  "env": {"AGENTIS_MODEL": "...", "AGENTIS_AUTO_MERGE": "false"},                   // nesecretní env pro kroky
  "pass_env": ["AGENTIS_SERVICE_TOKEN", "AGENTIS_ENDPOINT"],                          // secrety jen z env procesu
  "require_vars": ["APPROVED"]                   // volitelně: validace approval workflow před startem
}
```

Secrety (`AGENTIS_SERVICE_TOKEN`, …) **nikdy** ve spec ani v argv (viditelné v `ps`, zůstávají na disku):
adapter je předá v environmentu child procesu a ve spec je jen jejich jméno v `pass_env`.

### Výstup 1: události (JSONL na stdout)

```jsonc
{"type":"workflow_started","attempt":"...","namespace":"...","executor":"kubernetes","steps":["prepare","agent"]}
{"type":"step_started","index":0,"step":"prepare","needs":[]}
{"type":"step_succeeded","index":0,"step":"prepare","job":"...","attempts":1,"duration_ms":1234,"vars":{"FOO":"1"}}
{"type":"step_failed","index":1,"step":"agent","attempts":3,"log_tail":"...","continue_on_error":false}
{"type":"step_skipped","index":2,"step":"deploy","reason":"condition","condition":"FOO == '1'"}
{"type":"warning","message":"artifact glob matched no files","data":{...}}
{"type":"workflow_finished","status":"failed","failed_step":"agent"}
```

Adapter z nich 1:1 dělá dnešní `run.adapter_event` volání a záznamy ve status registry — tedy
stejné eventy, jaké posílá dnešní `_emit_step_*`. Diagnostika enginu jde na stderr (adapter ji loguje).

### Výstup 2: `result.json` v run dir (finální, atomicky zapsaný)

```jsonc
{
  "protocol": 1,
  "status": "success | failed | aborted",
  "failed_step": null,
  "vars": {"APPROVED": "1"},
  "outputs": {                                    // jen z úspěšných, nepřeskočených kroků
    "comments": [{"body": "...", "status": "in_review", "author_name": null}],
    "session_id": "...",
    "attachments": [{"label": "Preview", "value": "https://...", "type": "url"}],
    "artifacts": [{"name": "report.pdf", "filename": "report.pdf", "path": "artifact-staging/...", "size": 1234}]
  },
  "followups": [{"label": "Merge", "workflow": "merge", ...}],   // už vyhodnocené `if`, prázdné při failure
  "warnings": []
}
```

### Exit kódy

`0` success · `1` workflow failed · `2` nevalidní workflow / spec / usage · `3` busy (aktivní joby
se stejnými labely) · `130`/`143` aborted (SIGINT/SIGTERM).

### Abort

Adapter pošle `SIGTERM` procesu (skupině). Engine nastaví `abort_event`, zruší běžící kroky
(k8s Joby, Docker kontejnery, lokální procesy), zapíše `result.json` se `status: aborted` a skončí `143`.
Když adapter proces nemá (restart), použije `agentis-workflow abort --label ...` stejně jako dnešní fallback.

## Jak bude vypadat adapter po rozdělení

```
start / add_message
  → git worktree, přílohy, prompt (beze změny)
  → vybrat namespace, run_dir, attempt; zapsat prompt.md, context.json, spec.json
  → snapshot zdrojáků (changes diff)
  → subprocess.Popen(["agentis-workflow", "run", "--spec", ...], env=...)   # vrací hned
  → čtecí thread: JSONL události → status registry + run.adapter_event
  → po skončení: result.json → task.add_agent_comment / run.store_session_id / artefakty (base64),
    + changes diff, screenshoty, IDE „Directory“ attachment, followup akce
abort → SIGTERM / agentis-workflow abort
approve → synchronní `agentis-workflow run approval --spec ...` s timeoutem, čte vars.APPROVED(_METADATA)
```

Zachované invarianty: jedno aktivní workflow per task (adapter drží `Popen` per task), rychlá odpověď
`start`, graceful shutdown čeká na child procesy, chyby reportingu do Agentisu nezakryjí výsledek workflow,
workflow YAML se zmrazí při startu (engine ho načte jednou, v procesu).

## Agentis-specifické části YAML schématu

Některá pole schématu jsou dnes vázaná na Agentis: `outputs[].status` (task status), `agent_comment`,
`session_id`, `workflow.followups`, interpolační tokeny `TASK_ID`, `TASK_NUMBER`, `BRANCH`, `GITHUB_REPO`…

Doporučení pro v1: **ponechat schéma beze změny** (zpětná kompatibilita všech `.agentis/workflows/*.yaml`).
Engine tyto hodnoty validuje, ale neinterpretuje — jen je vrátí v `result.json`. Tokeny zůstávají
v allowlistu; když je volající nedodá ve `vars`, interpolují se na prázdný string (stejně jako dnes
`TASK_NUMBER` bez čísla). Env proměnné `AGENTIS_RUN_DIR`, `AGENTIS_PROMPT_FILE`, `AGENTIS_WORKFLOW_STATUS`…
engine nastavuje dál pod stejným jménem.

Později (v2) je možné přidat `workflow.inputs:` pro deklaraci vlastních tokenů a neutrální aliasy
(`WORKFLOW_RUN_DIR`), aby statická workflow nemusela nést „agentis“ názvosloví.

## Postup migrace

1. **Odpojení uvnitř tohoto repa** (bez změny chování): nový balíček `agentis_workflow/` v rootu repa,
   který neimportuje nic z `common.*`. Rozdělit `WorkflowManager` na `WorkflowEngine.run(spec, on_event) -> RunResult`
   a adapterový manager. Testy musí projít beze změny kontraktu.
2. **CLI enginu**: `agentis-workflow run --spec` + JSONL + `result.json`; adapterový manager přepnout
   na subprocess. V testech adapteru fake binárka (malý Python skript) místo skutečného enginu.
3. **Přesun** `agentis_workflow/`, skillu, VS Code extension, `docs/workflow.md` a testů do
   `/var/www/agentis-workflow` (s historií přes `git filter-repo --path ...`).
4. **Závislost**: adapter instaluje engine jako balíček (git tag / interní index, nebo `pipx`);
   cesta k binárce v `AGENTIS_WORKFLOW_COMMAND` (default `agentis-workflow`). Adapter při startu ověří
   `agentis-workflow --version` a podporovanou verzi `protocol`. Docker image adapteru obsahuje obojí.

## Rozhodnutí (2026-10-16)

1. Kontrakt mezi projekty je **jen CLI** (adapter engine neimportuje).
2. Approval zůstává v adapteru.
3. Changes diff / snapshoty / screenshoty zůstávají v adapteru.
4. Git historie se do nového repa nepřenáší — začíná se čistě (krok 3 migrace bez `filter-repo`).
5. Prefix `AGENTIS_*` v env zůstává.
6. Engine zatím nedostane `init` ani vlastní starter workflow.

## Původní otevřené otázky

1. **Jen CLI, nebo i Python API?** Doporučuji CLI jako jediný kontrakt mezi projekty (izolace pádů,
   abort signálem, nezávislé verzování). Engine bude interně knihovna, ale adapter ji neimportuje.
2. **Approval** — nechat v adapteru (jen `run` + kontrola `vars.APPROVED`), nebo z něj udělat obecný
   „rozhodovací“ režim enginu? Doporučuji adapter.
3. **Changes diff / snapshoty** — zůstávají v adapteru (je to git/worktree věc). Souhlas?
4. **Historie gitu** — přenést historii souborů do nového repa, nebo začít čistě?
5. **Název env prefixu** — ponechat `AGENTIS_*` i v enginu (kompatibilita), nebo hned zavést aliasy?
6. **Bundled workflow** — `default.yaml`/`project.yaml` zůstanou v adapteru; má engine mít vlastní
   „starter“ workflow (např. `agentis-workflow init`)?

## Implementace a odchylky

Stav k 2026-10-16:

- Engine je v `/var/www/agentis-workflow` (balíček `agentis_workflow`, CLI `agentis-workflow`, kontrakt v `docs/cli.md`, protokol `1`). Adapter ho nainstaluje jako path závislost `../agentis-workflow` (editable) a volá přes `AGENTIS_WORKFLOW_COMMAND` (default `agentis-workflow` vedle `sys.executable`, jinak z `PATH`).
- Přidán příkaz `agentis-workflow check --spec spec.json` (nad rámec návrhu): synchronně načte a zvaliduje workflow, vyřeší executor, ověří images a busy stav a vypíše JSON `{"protocol", "workflow_file", "executor", "namespace", "steps"}`. Exit `0` / `2` (nevalidní) / `3` (busy). Adapter ho volá v `start` před spuštěním `run`; chyby se vracejí v odpovědi stejně jako dřív.
- `WorkflowManager` zachovává veřejné API (`start_workflow`, `approve`, `abort`, `active_count`, `wait_idle`, `run_result`, `snapshot_key_for_task`, výjimky a konstruktor s `agentis_sink`). Scheduler, retry, závislosti a artefakty běží v enginu; adapter čte JSONL události (`run.adapter_event` se stejnými payloady jako dřív) a `result.json` (komentáře, session, přílohy, artefakty z `artifact-staging/` s kontrolou cesty, followupy, changes diff).
- Abort posílá SIGTERM skupině procesů enginu; bez běžícího procesu se použije `agentis-workflow abort --executor … --label …`.
- Approval běží jako synchronní `run --spec` s `require_vars: ["APPROVED"]`; idempotence přes `approval_id` a fingerprint zůstala.
- Secrety jdou jen přes env child procesu (`pass_env: ["AGENTIS_SERVICE_TOKEN"]`); do `spec.json`, argv ani logů se nedostanou.
- `KUBECTL_COMMAND` a `DOCKER_COMMAND` přešly do enginu (adapter je už nečte).
- `pyyaml` zůstal závislostí adapteru, protože `common/prompt_context.py` čte YAML front matter rolí.
- Adapter nemá `workflow run|validate|list`; ruční běh a validace jsou v `agentis-workflow` (`run <name>`, `validate`, `list`). `scripts/mock_workflow_request.py` si payload skládá sám.
- Přesunuto do enginu (bez git historie, podle rozhodnutí 4): `skills/agentis-workflow/` (symlink v `~/.claude/skills/` nyní míří na engine), `vscode-agentis-workflow/`, `docs/workflow.md`, `docs/testovani-workflow.md`, testy schema/runtime/agent step/devrun.
- Zůstává v adapteru: `docs/koncept-local-workflow-executor.md` (koncept, není v návrhu zmíněn; k rozhodnutí, zda přesunout).
- Ověření protokolu: každé volání `check` a `result.json` nese `protocol`; neshodná verze se odmítne. Samostatný `--version` probe při startu adapteru zatím není.

