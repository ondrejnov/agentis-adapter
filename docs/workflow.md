# Workflow režim

## K čemu workflow slouží

Workflow režim přesouvá projektově proměnlivou logiku běhu agenta (příprava prostředí, spuštění agenta, commit, pull request, úklid) z Python adapteru do deklarativního YAML souboru ve worktree projektu. Adapter pak jen orchestruje: načte YAML, spouští kroky postupně přes zvolený executor a po úspěšném doběhnutí aplikuje výstupy (komentář, přílohy, artefakty) do Agentisu.

Je to protějšek lokálního CLI runtime (`local`), kde agent běží jako proces přímo na hostu řízený adapterem. Ve workflow režimu adapter sám žádného agenta nespouští — agent je jen jedním z kroků workflow (typicky `agentiscode` v kroku „Run agent“).

Klíčové zdrojáky:

| Soubor | Role |
| --- | --- |
| `common/workflow/schema.py` | Pydantic schema YAML, interpolace tokenů, `if` podmínky |
| `common/workflow/manager.py` | `WorkflowManager` — orchestrace runů, outputs, eventy do Agentisu |
| `common/workflow/runtime.py` | Protokol `WorkflowStepRunner` + Kubernetes executor (`KubectlJobRunner`) |
| `common/workflow/local_runtime.py` | Lokální executor (`LocalProcessRunner`) |
| `.agentis/workflows/*.yaml` | Konfigurace workflow v repozitáři projektu |

## Kdy se workflow spustí

Workflow runtime se aktivuje v JSON-RPC metodách `start` / `add_message`, když:

- `context.adapter.runtime == "workflow"`, nebo
- kontext obsahuje pojmenované workflow `context.adapter.workflow = "<name>"` (followup akce jako merge/close) — to běží přes workflow runtime vždy, bez ohledu na `runtime`.

`start` / `add_message` vrací rychle — workflow běží na pozadí v daemon threadu, průběh se hlásí do Agentisu přes `run.adapter_event` (`workflow`, `workflow_step`, na konci `idle`). Metody `question` / `approve` workflow runtime nepodporuje (není IPC do Jobu). `abort` zruší běžící workflow a smaže aktivní kroky podle labels.

Per task běží maximálně jedno workflow — pokus o start nad běžícím taskem skončí chybou (busy).

## Výběr workflow souboru

Workflow YAML leží ve worktree v `.agentis/workflows/`:

| Soubor | Kdy se použije |
| --- | --- |
| `default.yaml` | Běžný task run (worktree + git větev); jediný s `followups` |
| `project.yaml` | `context.adapter.scope == "project"` — běží přímo v adresáři projektu, bez worktree a git operací |
| `<name>.yaml` (`merge.yaml`, `close.yaml`, …) | Pojmenované workflow z `context.adapter.workflow`; typicky followup akce |

Soubor se načte, interpoluje a **zmrazí jednou na začátku runu** — pozdější změny ve worktree běžící workflow neovlivní. Chybějící soubor pro pojmenované workflow nebo project scope je chyba startu.

### Run soubory

Adapter pro každý pokus (attempt) zapíše `prompt.md` a `context.json` a kroky do něj ukládají outputs:

- běžný task run: `<worktree>/.agentis/runs/<attempt>/`, outputs kroků se čtou relativně k worktree (`.agentis/outputs/...`),
- project scope a pojmenovaná workflow: `<project_run_root>/<run_id>/<attempt>/` (default `/tmp/agentis`, env `ADAPTER_PROJECT_RUN_ROOT`) — **mimo worktree**, protože akce jako merge/close můžou worktree samy smazat; outputs se pak čtou relativně k run adresáři (`outputs/...`).

## Executory

Kde kroky fyzicky poběží, určuje `workflow.executor` v YAML; bez něj platí env `WORKFLOW_EXECUTOR` adapteru, default `kubernetes`.

### `kubernetes`

Každý krok je `batch/v1 Job` obsluhovaný přes `kubectl` (apply / wait / logs / delete) — vyžaduje platný kube context. Joby běží v namespace odvozeném z kontextu (`common/namespaces.py`: explicitní `context.namespace`, jinak `project-<slug>` pro project scope, jinak `<prefix>-<task_number>-<title>`). Každý krok musí mít `image` (na kroku nebo na workflow), jinak start selže. `volumes` / `volumeMounts` / `imagePullSecrets` / `resources` se promítají do Job manifestu.

### `local`

Kroky běží jako lokální bash subprocessy na hostu nad worktree, pod uživatelem adapter procesu, bez izolace. Kubernetes pole (`image`, `volumes`, `volumeMounts`, `imagePullSecrets`, `resources`, `deleteNamespace`) se ignorují (vypíše se varování). Logy kroků jdou do `<run_dir>/logs/<job>.log`. Proměnná `AGENTIS_TOKEN` z prostředí adapteru se do kroků nepropisuje.

Oba executory spouští `run` skript kroku přes stejný bash wrapper: `set -euo pipefail`, sourcing `envFiles`, `cd` do `workingDir` kroku (jinak workflow `workingDir`, jinak `$WORKDIR`).

## Struktura YAML

```yaml
version: 1                      # povinné, vždy 1
workflow:
  executor: local               # volitelné: kubernetes | local; default dle adapteru
  image: registry/image:tag     # povinné pro executor kubernetes
  imagePullSecrets:
    - name: registry
  workingDir: "[%WORKDIR%]"
  timeoutSeconds: 14400         # default timeout kroku (sekundy)
  ttlSecondsAfterFinished: 300  # TTL dokončených K8s Jobů
  deleteNamespace: false        # po úspěchu smazat celý namespace (jen kubernetes)
  envFiles:                     # soubory sourcované na začátku každého kroku
    - /root/.config/agentis/agentis.env
  env:                          # env společné všem krokům
    TASK_NUMBER: "[%TASK_NUMBER%]"
  volumeMounts: [...]           # jen kubernetes
  followups: [...]              # akce nabídnuté v completion komentáři (viz níže)
  steps:                        # povinné, aspoň jeden krok
    - name: Run agent
      run: |                    # bash skript kroku
        ...
      if: ENV_READY != 'true'   # volitelná podmínka (viz níže)
      image: ...                # přepis workflow image (jen kubernetes)
      env: {}                   # env navíc pro tento krok
      workingDir: ...           # přepis pracovního adresáře
      timeoutSeconds: 600       # přepis timeoutu
      resources: {}             # K8s resources (jen kubernetes)
      outputs: [...]            # viz níže
volumes: [...]                  # K8s volumes (jen kubernetes)
```

Schema je striktní (`extra="forbid"`) — neznámé klíče jsou chyba.

### Interpolace tokenů

Ve string hodnotách YAML lze použít tokeny `[%NAME%]`; nahradí se při načtení souboru. Povolené tokeny (jiné jméno je chyba, neznámá hodnota se nahradí prázdným stringem):

| Token | Hodnota |
| --- | --- |
| `NAMESPACE` | Kubernetes namespace runu |
| `WORKDIR` | absolutní cesta k worktree |
| `RUN_DIR` | adresář run souborů (prompt, context, outputs) |
| `MAIN_DIR` | hlavní adresář projektu (`context.working_dir`) |
| `RUN_ID` / `TASK_ID` / `TASK_NUMBER` / `TASK_TITLE` | identifikace runu a tasku |
| `BRANCH` / `BASE_BRANCH` | task větev a cílová větev |
| `GITHUB_REPO` | GitHub repozitář projektu |

### Prostředí kroků

Kromě `workflow.env` / `step.env` dostane každý krok od adapteru:

- všechny interpolační tokeny jako env proměnné (`WORKDIR`, `BRANCH`, …),
- `AGENTIS_RUN_ID`, `AGENTIS_TASK_ID`, `AGENTIS_RUN_DIR`, `AGENTIS_PROMPT_FILE` (soubor s promptem), `AGENTIS_CONTEXT_FILE` (context JSON),
- volitelně `AGENTIS_SESSION_ID` (resume předchozí session), `AGENTIS_MODEL`, `AGENTIS_AGENT`, `AGENTIS_EFFORT` z `context.adapter`,
- proměnné z `var` outputs už dokončených kroků.

### Podmínky `if`

Krok s `if` se spustí jen při splnění podmínky nad proměnnými z `var` outputs předchozích kroků. Syntaxe: `VAR`, `!VAR`, `VAR == hodnota`, `VAR != 'hodnota'`. Neznámá proměnná se chová jako prázdný string; holý test bere `""`/`0`/`false`/`no` (case-insensitive) jako nepravdu. Outputs přeskočeného kroku se na konci neaplikují.

```yaml
- name: Check environment
  run: |
    mkdir -p .agentis/outputs
    printf 'true' > .agentis/outputs/env-ready
  outputs:
    - type: var
      name: ENV_READY
      valueFrom: .agentis/outputs/env-ready
- name: Create virtualenv
  if: ENV_READY != 'true'
  run: python3.13 -m venv .venv
```

### Outputs

Kroky komunikují s adapterem přes soubory; cesty jsou relativní k output rootu (worktree, resp. run adresáři — viz výše) a nesmí z něj utéct. Typ `var` se čte hned po doběhnutí kroku, ostatní outputs se aplikují **až po úspěšném dokončení celého workflow** jediným voláním do Agentisu:

| Typ | Pole | Význam |
| --- | --- | --- |
| `agent_comment` | `bodyFrom`, `status` | Tělo completion komentáře tasku + cílový status |
| `session_id` | `valueFrom` | Uloží session id do runu (`run.store_session_id`) pro pozdější resume |
| `url` / `text` | `label`, `valueFrom` | Příloha komentáře (odkaz / text) |
| `artifact` | `name`, `path` | Soubor přiložený ke komentáři (base64) |
| `var` | `name`, `valueFrom` | Workflow proměnná pro `if` podmínky a env dalších kroků |

Při selhání kroku se workflow zastaví, do Agentisu jde událost s posledními ~50 řádky logu a žádné outputs se neaplikují. U běžných task runů adapter navíc automaticky přikládá „Changes diff“ (snapshot zdrojáků při startu vs. konci).

### Followup akce

Sekce `workflow.followups` definuje akce nabídnuté v completion komentáři po doběhnutí workflow — konfigurují se jen tady, nikde v Pythonu. Akce nejsou samostatné RPC metody: kliknutí dispatchne `start` s `context.adapter.workflow = "<workflow>"`, který spustí `.agentis/workflows/<workflow>.yaml`.

```yaml
followups:
  - title: Git merge
    prompt: Sloučit změny z task větve do hlavní větve.
    workflow: merge
    continue_previous_run: false   # volitelné
```

Workflow bez sekce (`project.yaml`, `merge.yaml`, `close.yaml`) žádné akce nenabízí. Lokální CLI sessions čtou sekci best-effort přes `load_workflow_followups()` — nevalidní soubor znamená jen žádné akce, dokončení runu na něm nespadne.

### Prostředí lokálních sessions (`local-env.yaml`)

Mini workflow `.agentis/workflows/local-env.yaml` deklaruje prostředí pro lokální CLI sessions (environment `local`) — nahradilo dřívější `.agentis/local-setup.sh`. Nespouští ho `WorkflowManager`: při každém spawnu agent CLI ho `build_local_env_shell_command()` (`common/workflow/local_env.py`) best-effort přečte z cwd agenta a složí z něj bash skript `env + kroky + exec agent`. Chybějící nebo nevalidní soubor znamená spuštění agenta bez setupu (varování na stderr), neúspěšný krok agenta nespustí.

Použijí se jen `workflow.env`, `workflow.envFiles` a `steps[].run`; Kubernetes pole a kroková `if`/`outputs`/`env` se ignorují s varováním. Dvě odlišnosti proti executorům:

- hodnoty `workflow.env` expanduje bash — `PATH: "[%WORKDIR%]/.venv/bin:$PATH"` tedy zachová PATH hosta a jen předřadí venv,
- každý krok běží v subshellu, takže `exit 0` ukončí jen krok (guard „už je hotovo“), ne spuštění agenta.

Z tokenů jsou k dispozici `[%WORKDIR%]` (cwd agenta) a `[%MAIN_DIR%]` (hlavní worktree); obě hodnoty jsou krokům k dispozici i jako env proměnné.

## Dodávaná workflow

| Soubor | Účel |
| --- | --- |
| `default.yaml` | Plný task run: příprava `.env` a virtualenvu (podmíněně přes `ENV_READY`), spuštění agenta (`agentiscode`, adapter podle modelu), commit, push + pull request; nabízí followups „Git merge“ a „Zavřít prostředí“ |
| `project.yaml` | Run nad celým projektem bez gitu — jen spuštění agenta s outputs `agent_comment` + `session_id` |
| `merge.yaml` | Rebase task větve na base (konflikty řeší AI resolver), fast-forward base větve, push, úklid worktree a větve |
| `close.yaml` | Úklid worktree a task větve bez merge; `deleteNamespace: true` |
| `local-env.yaml` | Prostředí lokálních CLI sessions: PATH s venv (worktree, pak hlavní worktree) a vytvoření venv při studeném startu; viz výše |

## Časté chyby

- **`Workflow executor 'kubernetes' vyžaduje 'image'`** — krok nemá `image` ani workflow default; doplnit, nebo přepnout `executor: local`.
- **`Workflow file not found`** — ve worktree chybí `.agentis/workflows/<soubor>.yaml` (u project scope `project.yaml`, u followup akce soubor pojmenovaného workflow).
- **`Unknown workflow token [%X%]`** — token mimo allowlist; viz tabulka výše.
- **Workflow „busy“** — per task běží jen jeden run; počkat na doběhnutí nebo zavolat `abort`.
- **Output se nepropsal** — soubor neexistuje, je prázdný, krok byl přeskočen přes `if`, workflow neskončilo úspěchem, nebo cesta vede mimo output root.
