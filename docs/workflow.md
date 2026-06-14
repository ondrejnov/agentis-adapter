# Workflow režim

## K čemu workflow slouží

Workflow režim přesouvá projektově proměnlivou logiku běhu agenta (příprava prostředí, spuštění agenta, commit, pull request, úklid) z Python adapteru do deklarativního YAML souboru ve worktree projektu. Adapter pak jen orchestruje: načte YAML, spouští kroky postupně přes zvolený executor a po doběhnutí aplikuje výstupy úspěšných kroků (komentář, přílohy, artefakty) do Agentisu.

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
| `_base.yaml` | Sdílený základ pro `extends` (viz Dědičnost níže); nemá `steps`, samostatně se spustit nedá |

Soubor se načte, vyřeší se `extends`, interpoluje a **zmrazí jednou na začátku runu** — pozdější změny ve worktree běžící workflow neovlivní. Chybějící soubor pro pojmenované workflow nebo project scope je chyba startu.

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
extends: _base                  # volitelné: dědičnost z jiného souboru (viz níže)
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
  stepTemplates:                # sdílené definice kroků pro `uses` (viz níže)
    run-agent:
      run: ...
  steps:                        # povinné, aspoň jeden krok
    - name: Run agent
      uses: run-agent           # volitelně: defaulty kroku ze šablony (viz níže)
      run: |                    # bash skript kroku (povinný, pokud ho nedodá `uses`)
        ...
      if: ENV_READY != 'true'   # volitelná podmínka (viz níže)
      continueOnError: false    # selhání kroku nezastaví workflow (viz Error handling)
      retries: 0                # počet opakování selhaného kroku
      always: false             # krok běží i po selhání dřívějšího kroku
      image: ...                # přepis workflow image (jen kubernetes)
      env: {}                   # env navíc pro tento krok
      workingDir: ...           # přepis pracovního adresáře
      timeoutSeconds: 600       # přepis timeoutu
      resources: {}             # K8s resources (jen kubernetes)
      outputs: [...]            # viz níže
volumes: [...]                  # K8s volumes (jen kubernetes)
```

Schema je striktní (`extra="forbid"`) — neznámé klíče jsou chyba.

### Dědičnost (`extends`)

Top-level pole `extends: <name>` načte před validací soubor `.agentis/workflows/<name>.yaml` jako rodiče a smerguje ho s potomkem — typicky `extends: _base`, aby image, env a volumes nebyly zkopírované v každém workflow. Rodičovský soubor nemusí mít `steps`, takže se samostatně spustit nedá (start na něm selže na validaci). Podporovaná je **jediná úroveň** dědičnosti: rodič s vlastním `extends` (řetězení i cyklus) je chyba `WorkflowExtendsError`; chybějící cílový soubor je `FileNotFoundError` s cestou.

Merge probíhá nad surovým YAML (defaulty schématu nepřebijí hodnoty rodiče) a **interpolace `[%TOKEN%]` běží až po merge** — tokeny v base se vyhodnotí v kontextu runu potomka. Sémantika po polích:

| Pole | Sémantika |
| --- | --- |
| skaláry (`image`, `workingDir`, `timeoutSeconds`, `deleteNamespace`, …) | potomek přepisuje rodiče; bez hodnoty v potomkovi platí rodič |
| `env` | merge po klíčích, potomek vyhrává |
| `stepTemplates` | merge po jménech šablon; potomek přepisuje **celou** šablonu (žádný deep-merge polí) |
| `envFiles`, `volumeMounts`, `imagePullSecrets`, `volumes` | konkatenace rodič + potomek; položka-mapa se stejným `name` se přepíše na místě, přesný duplikát se vynechá |
| `steps`, `followups` | **nedědí se nikdy** — potomek je musí definovat sám |

Konkatenace seznamů (místo přepisu) je zvolená záměrně: potomci typicky jen *přidávají* mounty navíc a přepis by je nutil kopírovat celý base blok, čímž by dědičnost ztratila smysl. Přepis podle `name` zároveň brání duplicitním jménům volumes v Job manifestu a umožňuje cílené přepsání jedné položky (např. zrušit `readOnly`). `steps` se nedědí, protože kroky jsou podstata workflow — „zdědit a upravit“ seznam kroků se nedá vyjádřit srozumitelně; sdílení *jednoho* kroku mezi workflow řeší `stepTemplates` + `uses` (viz níže).

### Sdílené kroky (`stepTemplates` + `uses`)

Krok, který se opakuje ve více workflow, se definuje jednou ve `workflow.stepTemplates` (typicky v `_base.yaml`, odkud se dědí přes `extends`) a kroky se na něj odkazují přes `uses: <jméno šablony>`:

```yaml
workflow:
  stepTemplates:
    run-agent:
      env:
        RUN_AGENT_FLAGS: --json
      run: |
        agentiscode ${RUN_AGENT_FLAGS:-} ...
      outputs:
        - type: session_id
          valueFrom: outputs/session-id
  steps:
    - name: Run agent
      uses: run-agent
      env:
        RUN_AGENT_FLAGS: ""    # odchylka jen tam, kde je potřeba
```

Šablona má stejná pole jako krok kromě `name` a `uses`. Sémantika merge:

- pole, které krok sám deklaruje, vyhrává nad šablonou — **celé** (deklarované `outputs` nahradí outputs šablony, nedoplňují se),
- `env` se merguje po klíčích, krok vyhrává,
- `run` je u kroku s `uses` volitelný (dodá ho šablona); krok bez `run` i `uses` je chyba validace, stejně jako `uses` na neexistující šablonu.

Resoluce proběhne při načtení souboru (po `extends` merge), runtime už vidí jen plně rozbalené kroky. Parametrizace skriptu se dělá přes env proměnné s defaulty v bashi (`${VAR:-default}`), ne přes interpolační tokeny — `[%TOKEN%]` v šabloně se nahradí built-in hodnotami runu jako kdekoli jinde.

Dodávaná šablona `run-agent` v `_base.yaml` spouští agenta (`agentiscode`, adapter CLI podle modelu, resume session) a parametrizuje se přes `RUN_AGENT_FLAGS` (default `--json`), `RUN_AGENT_OUTPUT_DIR` (default `$AGENTIS_RUN_DIR/outputs`; při přepisu je nutné přepsat i `outputs`, jejich cesty se čtou relativně k output rootu runu) a `RUN_AGENT_STREAM_FILTER` (příkaz, kterým proteče stdout agenta, default `cat` — Slack workflow tudy posílá stream do `scripts/slack_stream.py`).

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

Krok s `if` se spustí jen při splnění podmínky. Proměnnými podmínky jsou:

- `var` outputs předchozích kroků,
- built-in hodnoty runu — všechny interpolační tokeny z tabulky výše (`GITHUB_REPO`, `BRANCH`, `BASE_BRANCH`, `TASK_NUMBER`, …); stejná jména dostávají kroky i jako env proměnné,
- env proměnné kroku — přesně to env, které krok dostane (`workflow.env`, runtime env od adapteru jako `AGENTIS_MODEL`/`AGENTIS_AGENT`/task header env, a `step.env`). Lze tak podmínit krok hodnotou env: `if: AGENTIS_MODEL == 'opus'` nebo `if: DEPLOY_ENV != 'prod'`.

Při kolizi jmen vyhrává `var` output kroku nad built-in hodnotou i nad env (krok tak může env/built-in hodnotu pro zbytek workflow přepsat); v env samotném platí pořadí `workflow.env` < runtime env < `step.env`.

Gramatika: termy `VAR`, `!VAR`, `VAR == hodnota`, `VAR != 'hodnota'` spojené `&&` a `||`. `&&` má přednost před `||` — `A && B || C` se vyhodnotí jako `(A && B) || C`; závorky nejsou. Negace `!` platí jen na jednotlivý holý term, ne na porovnání ani skupinu. Hodnota porovnání s mezerami nebo se spojkou `&&` / `||` musí být v uvozovkách (`MODE == 'a && b'`).

Neznámá proměnná se chová jako prázdný string; holý test `VAR` bere `""`/`0`/`false`/`no` (case-insensitive) jako nepravdu. Syntaxe podmínek se validuje už při načtení workflow souboru. Přeskočení kroku se hlásí jako event `workflow_step` se statusem `skipped` a podmínkou v datech; outputs přeskočeného kroku se na konci neaplikují.

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
- name: Create pull request
  if: GITHUB_REPO && ENV_READY != 'true'
  run: gh pr create ...
```

### Outputs

Kroky komunikují s adapterem přes soubory; cesty jsou relativní k output rootu (worktree, resp. run adresáři — viz výše) a nesmí z něj utéct. Typ `var` se čte hned po doběhnutí kroku, ostatní outputs se aplikují **až po úspěšném dokončení celého workflow** jediným voláním do Agentisu:

| Typ | Pole | Význam |
| --- | --- | --- |
| `agent_comment` | `bodyFrom`, `status`, `name`/`nameFrom` | Tělo completion komentáře tasku + cílový status — číslo, nebo alias `backlog`/`todo`/`in_progress`/`in_review`/`done`/`cancelled`/`blocked` (číselník `Task.STATUS_*` v Agentisu). Volitelné `name` (statické) nebo `nameFrom` (soubor počítaný za běhu kroku, např. „Agent - $MODEL“) přetíží jméno autora komentáře; `nameFrom` má přednost. Jinak se použije jméno odvozené z modelu/agenta. |
| `session_id` | `valueFrom` | Uloží session id do runu (`run.store_session_id`) pro pozdější resume |
| `url` / `text` | `label`, `valueFrom` | Příloha komentáře (odkaz / text) |
| `artifact` | `name`, `path` | Soubor přiložený ke komentáři (base64) |
| `var` | `name`, `valueFrom` | Workflow proměnná pro `if` podmínky a env dalších kroků |

Outputs se aplikují po dokončení workflow **za úspěšně doběhlé kroky** — i když workflow jako celek selhalo (viz Error handling níže). Outputs přeskočených a selhaných kroků se neaplikují. U běžných task runů adapter navíc automaticky přikládá „Changes diff“ (snapshot zdrojáků při startu vs. konci).

### Error handling kroků

Selhaný krok bez příznaků níže workflow ukončí: do Agentisu jde událost `workflow_step` failed s posledními ~50 řádky logu, přeskočí se všechny zbývající ne-`always` kroky (hlásí se jako skipped) a na závěr jde `idle` failed se jménem selhaného kroku.

- **`continueOnError: true`** — selhání kroku workflow nezastaví. Krok se nahlásí jako failed (s `continueOnError: true` v datech eventu), ale jeho `var` outputs se nečtou a ostatní outputs se na konci neaplikují.
- **`retries: N`** — selhaný krok se zopakuje až N× (bez backoffu), tj. maximálně `N + 1` spuštění. Do Agentisu se hlásí **jen finální výsledek** s počtem pokusů (`attempts` v datech eventu) — mezivýsledky pokusů by jen zaplevelily timeline. Abort mezi pokusy workflow ukončí. Opakovaný pokus dostane unikátní jméno Jobu (`<job>-r<n>`), protože selhaný K8s Job s původním jménem stále existuje; u lokálního executoru má tím pádem každý pokus vlastní log soubor.
- **`always: true`** — krok běží i poté, co dřívější krok fatálně selhal (typicky úklid a failure komentář na konci). `always` kroky běží v původním pořadí a `if` podmínky pro ně platí stejně. Adapter jim navíc exportuje env proměnné `AGENTIS_WORKFLOW_STATUS` (`failed`/`success`) a `AGENTIS_FAILED_STEP` (jméno prvního fatálně selhaného kroku, jinak prázdné) — krok z nich pozná, jestli má složit failure komentář.

Protože se outputs úspěšných kroků aplikují i u selhaného workflow, může `always` krok doručit `agent_comment` s důvodem selhání do ticketu (viz krok „Report merge failure“ v `merge.yaml`). **Followup akce se u failure komentáře nenabízí** — sekce `workflow.followups` platí jen pro úspěšný run; nabízet merge/close nad rozdělanou prací po selhaném runu nedává smysl.

### Followup akce

Sekce `workflow.followups` definuje akce nabídnuté v completion komentáři po doběhnutí workflow — konfigurují se jen tady, nikde v Pythonu. Akce nejsou samostatné RPC metody: kliknutí dispatchne `start` s `context.adapter.workflow = "<workflow>"`, který spustí `.agentis/workflows/<workflow>.yaml`.

```yaml
followups:
  - title: Git merge
    if: PR_CREATED                 # volitelné — podmínka nad `var` outputs runu
    prompt: Sloučit změny z task větve do hlavní větve.
    workflow: merge
    continue_previous_run: false   # volitelné
```

Volitelné `if` podmíní nabídku akce výsledkem konkrétního runu: vyhodnocuje se nad `var` outputs runu stejnou gramatikou jako `if` kroků (viz výše), ale **bez built-in hodnot** — k dispozici jsou jen proměnné z `var` outputs úspěšně doběhlých kroků. Followup bez podmínky se nabízí vždy. Syntaxe se validuje při načtení workflow souboru. V `default.yaml` tak „Git merge" závisí na `PR_CREATED` (krok „Create pull request" čte `.agentis/outputs/pull-request-url` i jako var) — run bez commitů a PR akci nenabídne, „Zavřít prostředí" se nabízí vždy.

Workflow bez sekce (`project.yaml`, `merge.yaml`, `close.yaml`) žádné akce nenabízí. Lokální CLI sessions čtou sekci best-effort přes `load_workflow_followups()` — nevalidní soubor znamená jen žádné akce, dokončení runu na něm nespadne. Lokální sessions navíc nemají žádné `var` outputs runu, takže podmíněné followups (`if`) se v nich konzervativně přeskakují — akce s nevyhodnotitelnou podmínkou se nenabízí.

### Prostředí lokálních sessions (`local-env.yaml`)

Mini workflow `.agentis/workflows/local-env.yaml` deklaruje prostředí pro lokální CLI sessions (environment `local`) — nahradilo dřívější `.agentis/local-setup.sh`. Nespouští ho `WorkflowManager`: při každém spawnu agent CLI ho `build_local_env_shell_command()` (`common/workflow/local_env.py`) best-effort přečte z cwd agenta a složí z něj bash skript `env + kroky + exec agent`. Chybějící nebo nevalidní soubor znamená spuštění agenta bez setupu (varování na stderr), neúspěšný krok agenta nespustí.

Použijí se jen `workflow.env`, `workflow.envFiles` a `steps[].run`; Kubernetes pole a kroková `if`/`outputs`/`env` se ignorují s varováním. Dvě odlišnosti proti executorům:

- hodnoty `workflow.env` expanduje bash — `PATH: "[%WORKDIR%]/.venv/bin:$PATH"` tedy zachová PATH hosta a jen předřadí venv,
- každý krok běží v subshellu, takže `exit 0` ukončí jen krok (guard „už je hotovo“), ne spuštění agenta.

Z tokenů jsou k dispozici `[%WORKDIR%]` (cwd agenta) a `[%MAIN_DIR%]` (hlavní worktree); obě hodnoty jsou krokům k dispozici i jako env proměnné.

## Dodávaná workflow

Workflow `default.yaml`, `project.yaml`, `slack.yaml`, `merge.yaml` a `close.yaml` dědí přes `extends: _base` sdílenou infrastrukturu (image, `imagePullSecrets`, `envFiles`, společné env a volumes) a šablonu kroku `run-agent` z `_base.yaml` a definují jen vlastní kroky a odchylky.

| Soubor | Účel |
| --- | --- |
| `_base.yaml` | Sdílený základ pro dědičnost (infrastruktura + šablona `run-agent`); samostatně nespustitelný (nemá `steps`) |
| `default.yaml` | Plný task run: příprava `.env` a virtualenvu (podmíněně přes `ENV_READY`), spuštění agenta (`uses: run-agent` s outputs ve worktree a bez `--json`), commit, push + pull request (jen s nastaveným repozitářem — `if: GITHUB_REPO`); nabízí followups „Git merge“ a „Zavřít prostředí“ |
| `project.yaml` | Run nad celým projektem bez gitu — jen `uses: run-agent` s defaultními outputs `agent_comment` + `session_id` |
| `slack.yaml` | Dotaz ze Slack threadu (project scope): `uses: run-agent` se streamem přes `scripts/slack_stream.py`, odpověď/failure report do Slacku |
| `merge.yaml` | Rebase task větve na base (konflikty řeší AI resolver), fast-forward base větve, push, úklid worktree a větve; při selhání pošle failure komentář (`always` krok) |
| `close.yaml` | Úklid worktree a task větve bez merge; `deleteNamespace: true` |
| `local-env.yaml` | Prostředí lokálních CLI sessions: PATH s venv (worktree, pak hlavní worktree) a vytvoření venv při studeném startu; viz výše |

## Časté chyby

- **`Workflow executor 'kubernetes' vyžaduje 'image'`** — krok nemá `image` ani workflow default; doplnit, nebo přepnout `executor: local`.
- **`Workflow file not found`** — ve worktree chybí `.agentis/workflows/<soubor>.yaml` (u project scope `project.yaml`, u followup akce soubor pojmenovaného workflow).
- **`Workflow extends target not found`** — `extends` ukazuje na neexistující soubor v `.agentis/workflows/`.
- **`chained 'extends' is not supported`** — rodičovský soubor má vlastní `extends`; dědičnost má jen jednu úroveň.
- **`uses unknown step template`** — krok odkazuje šablonu, která po `extends` merge není ve `workflow.stepTemplates`.
- **`Unknown workflow token [%X%]`** — token mimo allowlist; viz tabulka výše.
- **Workflow „busy“** — per task běží jen jeden run; počkat na doběhnutí nebo zavolat `abort`.
- **Output se nepropsal** — soubor neexistuje, je prázdný, krok byl přeskočen (přes `if` nebo po selhání workflow), krok sám selhal (včetně `continueOnError`), nebo cesta vede mimo output root.
