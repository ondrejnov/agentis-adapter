# Workflow režim

## K čemu workflow slouží

Workflow režim přesouvá projektově proměnlivou logiku běhu (příprava prostředí, testy, volitelný agent, commit, pull request, úklid) z Python adapteru do deklarativního YAML souboru ve worktree projektu. Adapter pak jen orchestruje: načte YAML, naplánuje kroky podle závislostí, spouští je přes zvolený executor a po doběhnutí aplikuje výstupy úspěšných kroků (komentář, přílohy, artefakty) do Agentisu.

Workflow je jediný běhový model adapteru. Hodnota `context.adapter.runtime = "docker"` nebo `"local"` nevolí žádný nástroj; pouze vynutí odpovídající executor workflow. Coding agent může být jedním z kroků (například externí `agentiscode`), ale adapter jej nespouští natvrdo a workflow jej nemusí obsahovat.

Klíčové zdrojáky:

| Soubor | Role |
| --- | --- |
| `common/workflow/schema.py` | Pydantic schema YAML, interpolace tokenů, `if` podmínky |
| `common/workflow/manager.py` | `WorkflowManager` — orchestrace runů, outputs, eventy do Agentisu |
| `common/workflow/runtime.py` | Protokol `WorkflowStepRunner` + Kubernetes executor (`KubectlJobRunner`) |
| `common/workflow/docker_runtime.py` | Nativní Docker executor (`DockerContainerRunner`) |
| `common/workflow/local_runtime.py` | Lokální executor (`LocalProcessRunner`) |
| `.agentis/workflows/*.yaml` | Konfigurace workflow v repozitáři projektu |

## Kdy se workflow spustí

JSON-RPC metody `start` a `add_message` vždy používají workflow runtime. `context.adapter.runtime` už nerozhoduje o routingu; hodnota `docker` nebo `local` pouze vynutí odpovídající executor. Pojmenované workflow vybírá `context.adapter.workflow = "<name>"` (typicky followup akce jako merge/close), jinak se podle scope použije `default.yaml` nebo `project.yaml`.

`start` / `add_message` vrací rychle — workflow běží na pozadí v daemon threadu, průběh se hlásí do Agentisu přes `run.adapter_event` (`workflow`, `workflow_step`, na konci `idle`). Produkční dispatch vystavuje jen `start`, `add_message`, `abort` a `undo`; `question` / `approve` vrátí JSON-RPC `Method not found`. `abort` nastaví run jako abortovaný a smaže nebo zastaví aktivní kroky podle labels; funguje idempotentně i bez aktivního runu známého procesu.

U paralelního workflow přichází `workflow_step` eventy v reálném pořadí běhu, ne nutně v pořadí YAML. Data eventu obsahují `step_index`, `step`, `needs` a u spuštěných kroků `job`, takže UI může řadit buď časově, nebo podle definice workflow.

Manager blokuje další start nad taskem, pro který už eviduje aktivní workflow, chybou busy. Evidence je in-memory a nesdílí se mezi procesy adapteru.

## Výběr workflow souboru

Projektové workflow YAML leží ve worktree v `.agentis/workflows/`. Pokud vybraný soubor v projektu chybí, adapter hledá soubor stejného názvu v bundled adresáři (`ADAPTER_BUNDLED_WORKFLOW_DIR`, default `workflows/` v instalaci adapteru):

| Soubor | Kdy se použije |
| --- | --- |
| `default.yaml` | Běžný task run (worktree + git větev); může definovat `followups` |
| `project.yaml` | `context.adapter.scope == "project"` — běží přímo v adresáři projektu, bez worktree a git operací |
| `<name>.yaml` (`merge.yaml`, `close.yaml`, …) | Pojmenované workflow z `context.adapter.workflow`; typicky followup akce |
| `_base.yaml` | Sdílený základ pro `extends` (viz Dědičnost níže); nemá `steps`, samostatně se spustit nedá |

Soubor se načte, vyřeší se `extends`, interpoluje a **zmrazí jednou na začátku runu** — pozdější změny ve worktree běžící workflow neovlivní. Start skončí chybou až tehdy, když vybraný soubor neexistuje ani v projektu, ani v bundled adresáři.

### Run soubory

Adapter pro každý pokus (attempt) zapíše `prompt.md` a `context.json` a kroky do něj ukládají outputs:

- běžný task run: `<worktree>/.agentis/runs/<attempt>/`, outputs kroků se čtou relativně k worktree (`.agentis/outputs/...`),
- project scope a pojmenovaná workflow: `<project_run_root>/<run_id>/<attempt>/` (default `/tmp/agentis`, env `ADAPTER_PROJECT_RUN_ROOT`) — **mimo worktree**, protože akce jako merge/close můžou worktree samy smazat; outputs se pak čtou relativně k run adresáři (`outputs/...`).

## Executory

Kde kroky fyzicky poběží, určuje v první řadě `context.adapter.runtime = "docker"` nebo `"local"`, které vždy vynutí odpovídající executor. Jinak rozhoduje `workflow.executor` v YAML; bez něj platí env `WORKFLOW_EXECUTOR` adapteru, default `kubernetes`. Hodnota `workflow` v `context.adapter.runtime` samostatný executor nevybírá.

### `kubernetes`

Každý krok je `batch/v1 Job` obsluhovaný přes `kubectl` (apply / wait / logs / delete) — vyžaduje platný kube context. Joby běží v namespace odvozeném z kontextu: explicitní `context.namespace`; jinak `project-<slug>` pro project scope; sanitizované `task_id`, pokud task nemá číslo; jinak `<prefix>-<task_number>-<title>` (`common/namespaces.py`). Každý krok musí mít výslednou `image` na kroku nebo zděděnou z workflow, jinak start selže. `mounts`, `imagePullSecrets` a step-level `resources` se promítají do Job manifestu.

### `local`

Kroky běží jako lokální bash subprocessy na hostu nad worktree, pod uživatelem adapter procesu, bez izolace. Pole `context`, `image`, `steps[].image`, `mounts`, `imagePullSecrets` a `steps[].resources` se ignorují s varováním. `ttlSecondsAfterFinished` a `deleteNamespace` se ignorují bez varování. Logy kroků jdou do `<run_dir>/logs/<job>.log`. Z hostitelského prostředí se nepropíší `AGENTIS_TOKEN`, `AGENTIS_API_TOKEN` ani `AGENTIS_SERVICE_TOKEN`; runtime env nebo host-side `envFiles` ale mohou potřebné proměnné explicitně dodat.

### `docker`

Každý krok běží jako privilegovaný kontejner spuštěný přes `docker run --rm --privileged`; příkaz lze změnit přes `DOCKER_COMMAND`. Executor vyžaduje výslednou `image` stejně jako Kubernetes, ale nepoužívá kube context ani namespace. Kontejnery dostávají stejné labels jako Kubernetes Joby, takže busy-check a `abort` používají `docker ps` a `docker rm --force`. Timeout kontejner rovněž odstraní. Díky lokální image cache odpadá vytváření Kubernetes Jobu a Podu.

Existující absolutní cesty `WORKDIR`, `AGENTIS_RUN_DIR` a `MAIN_DIR` se automaticky bind-mountují na stejnou cestu v kontejneru. `workflow.mounts` se navíc převede na bind mounty, pokud položka používá `hostPath`; `readOnly`, relativní `subPath` a typy `DirectoryOrCreate`/`FileOrCreate` jsou podporované. Jiné Kubernetes volume sources, `subPathExpr` a `mountPropagation` skončí čitelnou chybou. `imagePullSecrets`, `context` a `steps[].resources` se ignorují s varováním; přihlášení k registry musí být připravené v Docker credential store uživatele adapteru. `ttlSecondsAfterFinished` a `deleteNamespace` se ignorují.

Adapter načte `envFiles` jednou na hostu při startu workflow a zmrazí jejich dotenv hodnoty do prostředí kroků. Soubory se nemountují ani nekopírují do Docker kontejneru nebo Kubernetes Podu a bash krok k jejich cestě nepotřebuje přístup. Pozdější soubor přepisuje dřívější; hodnoty z `envFiles` přepisují také `workflow.env`, runtime env a `steps[].env`. Jde o dotenv data (`KEY=value`, uvozovky a `export`), ne o shell skript; shellové příkazy a expanze se nespouštějí. Chybějící soubor ukončí start workflow chybou.

Všechny executory spouští `run` skript kroku přes stejný bash wrapper: `set -euo pipefail`, potom `cd` do `workingDir` kroku (jinak workflow `workingDir`, jinak `$WORKDIR`).

## Struktura YAML

```yaml
version: 1                      # povinné, vždy 1
extends: _base                  # volitelné: dědičnost z jiného souboru (viz níže)
workflow:
  executor: docker              # volitelné: kubernetes | docker | local; default dle adapteru
  context: my-kube-context      # volitelné: kubectl context; local jej ignoruje
  image: registry/image:tag     # default image; v K8s musí mít výslednou image každý krok
  imagePullSecrets:
    - name: registry
  workingDir: "[%WORKDIR%]"
  timeoutSeconds: 14400         # default timeout kroku (sekundy)
  maxParallel: 4                # maximum současně běžících kroků v jednom runu
  ttlSecondsAfterFinished: 3600 # schema default: TTL dokončených K8s Jobů
  deleteNamespace: false        # po úspěchu smazat celý namespace (jen kubernetes)
  envFiles:                     # host-side dotenv soubory injektované do env kroků
    - /root/.config/agentis/agentis.env
  env:                          # env společné všem krokům
    TASK_NUMBER: "[%TASK_NUMBER%]"
  mounts:                       # jen kubernetes; generuje volumeMounts i volumes
    - name: www
      mountPath: /var/www
      readOnly: true            # volitelné mount pole
      hostPath:                 # K8s volume source; lze použít i secret/configMap/emptyDir/...
        path: /var/www
  followups: [...]              # akce nabídnuté v completion komentáři (viz níže)
  stepTemplates:                # sdílené definice kroků pro `uses` (viz níže)
    run-agent:
      run: ...
  steps:                        # povinné, aspoň jeden krok
    - name: Run agent
      needs: ["Check environment"] # volitelné: explicitní závislosti podle jmen kroků
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
      ttlSecondsAfterFinished: 300 # přepis TTL konkrétního K8s Jobu
      resources: {}             # K8s resources (jen kubernetes)
      outputs: [...]            # viz níže
```

Každá položka `workflow.mounts` musí mít `name`, `mountPath` a aspoň jeden Kubernetes volume source klíč. Do `volumeMounts` se propisují jen mount pole `name`, `mountPath`, `readOnly`, `subPath`, `subPathExpr`, `mountPropagation`; ostatní klíče položky se propíšou do odpovídající položky `pod.spec.volumes`.

Schema je s výjimkou položek `mounts` striktní (`extra="forbid"`) — neznámé klíče jsou chyba. Mount povoluje dodatečné klíče jako Kubernetes volume source a předává je do manifestu; jejich platnost proto ověří až Kubernetes API.

### Dědičnost (`extends`)

Top-level pole `extends: <name>` načte před validací soubor `<name>.yaml` ze stejného adresáře jako vybrané workflow a smerguje ho s potomkem — v projektu tedy typicky `.agentis/workflows/_base.yaml`, u bundled fallbacku `workflows/_base.yaml`. Rodičovský soubor nemusí mít `steps`, takže se samostatně spustit nedá (start na něm selže na validaci). Podporovaná je **jediná úroveň** dědičnosti: rodič s vlastním `extends` (řetězení i cyklus) je chyba `WorkflowExtendsError`; chybějící cílový soubor je `FileNotFoundError` s cestou.

Merge probíhá nad surovým YAML (defaulty schématu nepřebijí hodnoty rodiče) a **interpolace `[%TOKEN%]` běží až po merge** — tokeny v base se vyhodnotí v kontextu runu potomka. Sémantika po polích:

| Pole | Sémantika |
| --- | --- |
| skaláry (`image`, `workingDir`, `timeoutSeconds`, `deleteNamespace`, …) | potomek přepisuje rodiče; bez hodnoty v potomkovi platí rodič |
| `env` | merge po klíčích, potomek vyhrává |
| `stepTemplates` | merge po jménech šablon; potomek přepisuje **celou** šablonu (žádný deep-merge polí) |
| `envFiles`, `mounts`, `imagePullSecrets` | konkatenace rodič + potomek; položka-mapa se stejným `name` se přepíše na místě, přesný duplikát se vynechá |
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

Šablona má stejná pole jako krok kromě `name`, `uses` a `needs`. Závislosti jsou vlastnost konkrétního workflow grafu, ne reusable šablony. Sémantika merge:

- pole, které krok sám deklaruje, vyhrává nad šablonou — **celé** (deklarované `outputs` nahradí outputs šablony, nedoplňují se),
- `env` se merguje po klíčích, krok vyhrává,
- `run` je u kroku s `uses` volitelný (dodá ho šablona); krok bez `run` i `uses` je chyba validace, stejně jako `uses` na neexistující šablonu.

Resoluce proběhne při načtení souboru (po `extends` merge), runtime už vidí jen plně rozbalené kroky. Parametrizace skriptu se dělá přes env proměnné s defaulty v bashi (`${VAR:-default}`), ne přes interpolační tokeny — `[%TOKEN%]` v šabloně se nahradí built-in hodnotami runu jako kdekoli jinde.

Dodávaná volitelná šablona `run-agent` v `_base.yaml` spouští externí balíček `agentiscode` (adapter CLI podle modelu, resume session) a parametrizuje se přes `RUN_AGENT_FLAGS` (default `--json`), `RUN_AGENT_OUTPUT_DIR` (default `$AGENTIS_RUN_DIR/outputs`; při přepisu je nutné přepsat i `outputs`, jejich cesty se čtou relativně k output rootu runu) a `RUN_AGENT_STREAM_FILTER` (příkaz, kterým proteče stdout agenta, default `cat` — Slack workflow tudy posílá stream do `scripts/slack_stream.py`). Balíček musí být samostatně nainstalovaný na hostu nebo ve zvolené image; není závislostí adapteru.

### Paralelní kroky (`needs` + `maxParallel`)

Workflow je DAG nad `workflow.steps`. Bez pole `needs` zůstává chování zpětně kompatibilní a sekvenční: krok implicitně čeká na bezprostředně předchozí krok. Paralelismus se zapíná explicitně:

- `needs` neuvedeno — implicitní závislost na předchozím kroku (`[]` jen u prvního kroku), tedy dnešní sekvenční pořadí,
- `needs: []` — root krok bez závislostí; může startovat hned, jakmile je volný slot,
- `needs: ["A", "B"]` — krok čeká na dokončení dříve definovaných kroků `A` a `B`.

`needs` odkazuje na `name` kroku a smí ukazovat jen na kroky definované výše v YAML. Pokud workflow používá `needs`, názvy kroků musí být unikátní. Forward reference, duplicitní položky v `needs` a neznámé názvy jsou validační chyba při načtení workflow.

Současně běžící kroky omezuje `workflow.maxParallel` (default `4`, minimum `1`). Limit platí stejně pro Kubernetes Joby i lokální procesy; retry kroku po celou dobu zabírá jeden slot.

```yaml
workflow:
  maxParallel: 2
  steps:
    - name: Build backend
      run: poetry run pytest backend

    - name: Build frontend
      needs: []
      run: npm test

    - name: Publish report
      needs: ["Build backend", "Build frontend"]
      run: ./scripts/report.sh
```

`needs` je ordering dependency, ne automatická success gate:

- dependency přeskočená přes `if` dependent odblokuje,
- dependency selhaná s `continueOnError: true` dependent odblokuje,
- fatální selhání bez `continueOnError` přepne workflow do fail-fast režimu: nové běžné kroky se už nestartují, pending běžné kroky se označí jako skipped, už běžící kroky se nechají doběhnout a pak můžou běžet relevantní `always` kroky.

Autor workflow ručí za bezpečnost paralelních zápisů do sdíleného worktree a output adresářů. Runtime nepozná, jestli bash skript jen čte, nebo zapisuje, proto žádný automatický lock nedává. Dva paralelní kroky by neměly zapisovat stejný soubor v `.agentis/outputs` / `outputs`; ne-`var` outputs se sice aplikují deterministicky v pořadí YAML, ale obsah kolidujícího souboru je race condition.

### Interpolace tokenů

Ve string hodnotách YAML lze použít tokeny `[%NAME%]`; nahradí se při načtení souboru. Povolené tokeny (jiné jméno je chyba, neznámá hodnota se nahradí prázdným stringem):

| Token | Hodnota |
| --- | --- |
| `NAMESPACE` | Logický namespace runu; local executor jej používá jen v metadatech |
| `WORKDIR` | absolutní cesta k worktree |
| `RUN_DIR` | adresář run souborů (prompt, context, outputs) |
| `MAIN_DIR` | hlavní adresář projektu (`context.working_dir`) |
| `RUN_ID` / `TASK_ID` / `TASK_NUMBER` / `TASK_TITLE` | identifikace runu a tasku |
| `BRANCH` / `BASE_BRANCH` | task větev a cílová větev |
| `GITHUB_REPO` | GitHub repozitář projektu |

### Prostředí kroků

Kromě `workflow.env` / `step.env` dostane každý krok od adapteru:

- všechny interpolační tokeny jako env proměnné (`WORKDIR`, `BRANCH`, …),
- `AGENTIS_RUN_ID`, `AGENTIS_TASK_ID`, `AGENTIS_PROJECT_ID`, `AGENTIS_RUN_DIR`, `AGENTIS_PROMPT_FILE` (soubor s promptem), `AGENTIS_CONTEXT_FILE` (context JSON),
- volitelně `AGENTIS_ENDPOINT` a `AGENTIS_SERVICE_TOKEN` z konfigurace adapteru; service token je záměrně předán agentím krokům pro callbacky do Agentisu,
- volitelně `AGENTIS_SESSION_ID` (resume předchozí session), `AGENTIS_MODEL`, `AGENTIS_AGENT`, `AGENTIS_EFFORT` z `context.adapter`,
- `AGENTIS_AUTO_MERGE` (`"true"` / `"false"`) a hlavičky tasku jako sanitizované `TASK_HEADER_*`,
- proměnné z `var` outputs transitivních `needs` kroku.

### Podmínky `if`

Krok nebo followup akce s `if` se spustí/nabídne jen při splnění podmínky. Proměnnými podmínky jsou:

- u kroků `var` outputs transitivních `needs` kroku, u followup akcí všechny `var` outputs dokončeného runu,
- built-in hodnoty runu — všechny interpolační tokeny z tabulky výše (`GITHUB_REPO`, `BRANCH`, `BASE_BRANCH`, `TASK_NUMBER`, …); stejná jména dostávají kroky i jako env proměnné,
- env proměnné kroku/followupu (`workflow.env`, runtime env od adapteru jako `AGENTIS_MODEL`/`AGENTIS_AGENT`/`AGENTIS_AUTO_MERGE`/task header env, u kroků navíc `step.env`). Lze tak podmínit krok nebo followup hodnotou env: `if: AGENTIS_MODEL == 'opus'`, `if: DEPLOY_ENV != 'prod'` nebo `if: PR_CREATED && !AGENTIS_AUTO_MERGE`.

Při kolizi jmen vyhrává viditelný `var` output nad built-in hodnotou i nad env (krok tak může env/built-in hodnotu pro své dependenty přepsat); v env samotném platí pořadí `workflow.env` < runtime env < `step.env` (u followup akcí žádné `step.env` není). Paralelní větev, na které krok explicitně ani transitivně nezávisí přes `needs`, jeho `if` ani env neovlivní, i kdyby doběhla dřív.

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

Kroky komunikují s adapterem přes soubory; cesty jsou relativní k output rootu (worktree, resp. run adresáři — viz výše) a nesmí z něj utéct. Typ `var` se čte hned po úspěšném doběhnutí kroku, aby mohl řídit dependenty. Ostatní outputs se po konci workflow agregují v pořadí kroků a následně mohou vyvolat samostatné `run.store_session_id` a jedno či více `task.add_agent_comment` volání:

| Typ | Pole | Význam |
| --- | --- | --- |
| `agent_comment` | `bodyFrom`, `status`, `name`/`nameFrom` | Tělo completion komentáře tasku + cílový status — číslo, nebo alias `backlog`/`todo`/`in_progress`/`in_review`/`done`/`cancelled`/`blocked` (číselník `Task.STATUS_*` v Agentisu). Volitelné `name` nebo `nameFrom` přetíží autora; `nameFrom` má přednost. Bez nich adapter posílá `author_name: null` a fallback řeší Agentis. |
| `session_id` | `valueFrom` | Uloží session id do runu (`run.store_session_id`) pro pozdější resume |
| `url` / `text` | `label`, `valueFrom` | Příloha komentáře (odkaz / text) |
| `artifact` | `name`, `path` | Jeden soubor nebo glob souborů přiložených ke komentáři (base64); výsledky globu se řadí podle cesty, adresáře a cesty mimo output root se ignorují |
| `var` | `name`, `valueFrom` | Workflow proměnná pro `if` podmínky a env dalších kroků |

Pole v tabulce jsou funkčně potřebná pro uvedené chování, ale současné schema je s výjimkou `var.name` / `var.valueFrom` nevynucuje podle typu; neúplný output proto runtime typicky ignoruje. Schema navíc přijímá pole `filename`, které manager aktuálně nepoužívá.

Artifact `path` může obsahovat `*`, `?`, `[]` a rekurzivní `**`. Jeden output tak může přiložit více souborů; runtime pro jejich počet ani celkovou velikost aktuálně nevynucuje limit, takže glob musí být dostatečně úzký.

Outputs se aplikují po dokončení workflow **za úspěšně doběhlé kroky** v pořadí kroků v YAML — i když workflow jako celek selhalo (viz Error handling níže). Outputs přeskočených a selhaných kroků se neaplikují. Pokud vznikne více komentářů, sdílené attachments, screenshoty, artifacts a actions dostane pouze poslední. Adapter automaticky přikládá „Changes diff“ pro nepojmenované workflow včetně project scope; pojmenovaná workflow snapshot ani diff nemají.

### Error handling kroků

Selhaný krok bez příznaků níže přepne workflow do fail-fast režimu: do Agentisu jde událost `workflow_step` failed s posledními ~50 řádky logu, nové běžné kroky se už nestartují, pending ne-`always` kroky se označí jako skipped a už běžící kroky se nechají doběhnout. Po doběhnutí aktivních běžných kroků se spustí relevantní `always` kroky a na závěr jde `idle` failed se jménem prvního fatálně selhaného kroku.

- **`continueOnError: true`** — selhání kroku workflow nezastaví a dependenty odblokuje. Krok se nahlásí jako failed (s `continueOnError: true` v datech eventu), ale jeho `var` outputs se nečtou a ostatní outputs se na konci neaplikují.
- **`retries: N`** — selhaný krok se zopakuje až N× (bez backoffu), tj. maximálně `N + 1` spuštění. Do Agentisu se hlásí jen finální výsledek; failed event obsahuje počet pokusů v `attempts`, success event jej aktuálně neobsahuje. Abort mezi pokusy workflow ukončí. Opakovaný pokus dostane unikátní jméno Jobu (`<job>-r<n>`), takže u lokálního executoru má každý pokus vlastní log soubor. Během retry krok pořád zabírá jeden slot `maxParallel`.
- **`always: true`** — krok běží i poté, co workflow fatálně selhalo (typicky úklid a failure komentář na konci). V DAG pořád respektuje `needs` jako pořadí, ale nevyžaduje jejich úspěch: čeká, až dependencies skončí jako success/failed/skipped. Bez explicitního `needs` platí sekvenční default na předchozí krok. `if` podmínky pro něj platí stejně. Skript kroku dostane `AGENTIS_WORKFLOW_STATUS` (`failed`/`success`) a `AGENTIS_FAILED_STEP`; tyto dvě proměnné se přidávají až po vyhodnocení `if`, takže je lze použít v `run`, ne v podmínce kroku.

`abort` není failure workflow: zastaví aktivní kroky, scheduler už nespustí pending ani `always` kroky a outputs se neaplikují.

Protože se outputs úspěšných kroků aplikují i u selhaného workflow, může `always` krok doručit `agent_comment` s důvodem selhání do ticketu (viz krok „Report merge failure“ v `merge.yaml`). **Followup akce se u failure komentáře nenabízí** — sekce `workflow.followups` platí jen pro úspěšný run; nabízet merge/close nad rozdělanou prací po selhaném runu nedává smysl.

### Followup akce

Sekce `workflow.followups` definuje akce nabídnuté v completion komentáři po doběhnutí workflow — konfigurují se jen tady, nikde v Pythonu. Akce nejsou samostatné RPC metody: kliknutí dispatchne `start` s `context.adapter.workflow = "<workflow>"`, který spustí `.agentis/workflows/<workflow>.yaml`.

```yaml
followups:
  - title: Git merge
    if: PR_CREATED && !AGENTIS_AUTO_MERGE  # volitelné — stejná gramatika jako `if` kroků
    prompt: Sloučit změny z task větve do hlavní větve.
    workflow: merge
    continue_previous_run: false   # volitelné
```

Volitelné `if` podmíní nabídku akce výsledkem konkrétního runu: vyhodnocuje se stejnou gramatikou jako `if` kroků (viz výše) nad `workflow.env`, runtime env, built-in hodnotami a `var` outputs úspěšně doběhlých kroků. Followup bez podmínky se nabízí vždy. Syntaxe se validuje při načtení workflow souboru. V projektovém `.agentis/workflows/default.yaml` tohoto repozitáře tak „Git merge" závisí na `PR_CREATED` a `!AGENTIS_AUTO_MERGE` — run bez commitů/PR nebo run s auto-merge akci nenabídne, „Zavřít prostředí" se nabízí vždy.

Workflow bez sekce (`project.yaml`, `merge.yaml`, `close.yaml`) žádné akce nenabízí.

## Workflow v tomto repozitáři

Repozitář obsahuje dvě odlišné sady. Bundled fallback v `workflows/` se distribuuje s adapterem a obsahuje jen `_base.yaml`, `default.yaml` a `project.yaml`; obě spustitelná workflow mají pouze standardní agentí krok a žádné followups. Projektová sada v `.agentis/workflows/` konfiguruje samotný vývoj tohoto repozitáře a navíc obsahuje `slack.yaml`, `merge.yaml` a `close.yaml`.

Projektová workflow dědí přes `.agentis/workflows/_base.yaml` sdílenou infrastrukturu (image, `imagePullSecrets`, `envFiles`, společné env a mounty) a šablonu `run-agent`:

| Soubor | Účel |
| --- | --- |
| `.agentis/workflows/_base.yaml` | Sdílený základ projektové sady; samostatně nespustitelný |
| `.agentis/workflows/default.yaml` | Plný task run: příprava prostředí, agent, commit, push a PR; nabízí „Git merge“ a „Zavřít prostředí“ |
| `.agentis/workflows/project.yaml` | Run nad celým projektem bez git kroků |
| `.agentis/workflows/slack.yaml` | Dotaz ze Slack threadu se streamem přes `scripts/slack_stream.py` |
| `.agentis/workflows/merge.yaml` | Rebase, fast-forward base větve, push a úklid; při selhání pošle failure komentář |
| `.agentis/workflows/close.yaml` | Úklid worktree a task větve bez merge; `deleteNamespace: true` |
| `workflows/_base.yaml` | Minimální bundled šablona `run-agent` |
| `workflows/default.yaml` | Bundled fallback běžného tasku: jediný krok `Run agent` |
| `workflows/project.yaml` | Bundled fallback project scope: jediný krok `Run agent` |

## Časté chyby

- **Workflow executor `kubernetes` nebo `docker` vyžaduje `image`** — krok nemá `image` ani workflow default; doplnit, nebo přepnout `executor: local`.
- **`Workflow file not found`** — vybraný soubor (u project scope `project.yaml`, u followup akce pojmenované workflow) chybí v projektovém `.agentis/workflows/` i v bundled adresáři adapteru.
- **`Workflow extends target not found`** — `extends` ukazuje na neexistující soubor v `.agentis/workflows/`.
- **`chained 'extends' is not supported`** — rodičovský soubor má vlastní `extends`; dědičnost má jen jednu úroveň.
- **`uses unknown step template`** — krok odkazuje šablonu, která po `extends` merge není ve `workflow.stepTemplates`.
- **`unknown or future 'needs'`** — `needs` odkazuje na neexistující krok nebo na krok definovaný až níže; producent musí být v YAML před konzumentem.
- **`workflow steps using 'needs' require unique step names`** — workflow používá `needs`, ale více kroků má stejné `name`; názvy musí být unikátní, protože `needs` na ně odkazuje.
- **`duplicate 'needs' entries`** — jeden krok má v `needs` stejné jméno vícekrát.
- **`Unknown workflow token [%X%]`** — token mimo allowlist; viz tabulka výše.
- **Workflow „busy“** — per task běží jen jeden run; počkat na doběhnutí nebo zavolat `abort`.
- **Output se nepropsal** — krok byl přeskočen nebo selhal (včetně `continueOnError`), cesta vede mimo output root nebo chybí funkčně potřebné pole. Prázdné `agent_comment`, `session_id`, `url` a `text` se vynechají; `var` se naopak propaguje jako prázdný string a prázdný artifact se přiloží.
