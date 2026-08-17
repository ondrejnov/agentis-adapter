# Fork adapteru pro vlastní ticketovací systém

## Shrnutí

Tento repozitář není pouze obecný runner workflow. Je to obecný workflow a git
runtime, který má na vstupu i výstupu konkrétní integraci s Agentisem.

Nasazení na jiný ticketovací systém proto má dvě možné podoby:

1. **Kompatibilní integrační fasáda**: vlastní systém vystaví kontrakt podobný
   Agentisu. Změny v adapteru jsou malé, ale nový systém bude muset převzít
   Agentis JSON-RPC metody, payloady, WebSocket transport a autentizační
   hlavičky.
2. **Nativní fork**: workflow runtime zůstane zachovaný, ale Agentis se
   nahradí samostatným portem pro ticketovací systém. To je čistší dlouhodobé
   řešení a dovolí použít REST, webhooky, gRPC nebo jiný existující transport.

Doporučená varianta je druhá. Zachovat názvy `AGENTIS_*` a přímé volání
`AgentisJsonRpcClient` by znamenalo dlouhodobě přenášet cizí doménový kontrakt,
což komplikuje údržbu, testování i bezpečnostní revizi.

## Co zůstává použitelné

Následující části nejsou samy o sobě závislé na Agentisu a lze je převzít téměř
beze změny:

- `common/workflow/` — YAML workflow, DAG závislostí, podmínky, retry,
  `continueOnError`, `always` a outputs.
- `common/workflow/local_runtime.py` — spouštění lokálních bash procesů.
- `common/workflow/docker_runtime.py` — spouštění kroků přes Docker.
- `common/workflow/runtime.py` — Kubernetes executor přes `kubectl`.
- `common/git_adapter.py` — worktree, větve a snapshoty, pokud vlastní systém
  používá git task branch model.
- `common/artifacts/` — diffy, screenshoty a práce s artefakty.
- `common/status.py` a FastAPI observabilita — `/health`, `/status`, `/log` a
  `/runs/{run_id}/log`.
- workflow soubory v projektu — po nahrazení Agentis-specifických proměnných a
  příkazů v jednotlivých YAML souborech.

To platí za předpokladu, že vlastní systém umí předat stabilní identifikaci
tasku a běhu a že workflow nepotřebuje funkce, které nový systém nepodporuje.

## Co je dnes svázané s Agentisem

### Vstup do adapteru

Adapter očekává JSON-RPC 2.0 zprávy přes WebSocket. Spojení iniciuje adapter
směrem k backendu a backend do adapteru nevolá příchozí HTTP requesty.

Produkční dispatch dnes vystavuje tyto metody:

| Metoda | Účel |
| --- | --- |
| `start` | Založí běh, připraví workspace a spustí workflow. |
| `add_message` | Spustí další workflow nad existujícím během nebo session. |
| `abort` | Zastaví běžící workflow a jeho executory. |
| `undo` | Obnoví workspace z posledního in-memory source snapshotu. |

Kontrakt parametrů je definován v `common/models.py`. Nejde jen o obecný text
tasku. `context` obsahuje mimo jiné:

- `run_id`, `task_id`, `title`, `description` a `user_prompt`,
- stav, číslo a prioritu tasku,
- `project_id`, `project_title`, `project_slug` a pracovní adresář,
- `base_branch`, volitelný git repozitář a dokumentaci projektu,
- komentáře, přílohy a očekávané artefakty,
- `headers` pro vlastní metadata tasku,
- nastavení agenta, modelu, effortu, runtime, scope a workflow,
- volitelný `session_id` pro pokračování konverzace.

Vlastní systém musí buď tento kontext poskytovat, nebo adapter dostane novou
normalizační vrstvu, která jej z jeho dat sestaví.

### Výstup z adapteru

Adapter posílá výsledky zpět přes `AgentisJsonRpcClient` v
`common/agentis.py`. Je důležité odlišit přímý reporting adapteru od
telemetrie volitelného `agentiscode` kroku:

| Metoda | Co se zapisuje |
| --- | --- |
| `run.adapter_event` | **Adapter přímo** posílá start, průběh a výsledek workflow i jednotlivých kroků. |
| `run.store_session_id` | **Adapter přímo** uloží session ID z workflow outputu `session_id`; stejnou metodu používá i telemetry `agentiscode`. |
| `task.add_agent_comment` | **Adapter přímo** posílá komentář, pokud workflow output obsahuje `agent_comment`. `agentiscode` jej posílá jen s explicitním `--last-message-to-comment`. |

`task.add_agent_comment` proto zůstává důležitým integračním bodem pro workflow
runtime. Workflow outputs se do něj agregují ve
`common/workflow/manager.py`; bez ekvivalentu se ztratí finální komentář, stav
tasku, odkazy i přílohy.

Výchozí workflow obvykle nechává `agentiscode` zapsat finální text do souboru
`final-comment.md`. Komentář pak odešle až `WorkflowManager` přes
`task.add_agent_comment`. Přímé odeslání komentáře z `agentiscode` je volitelné
a aktivuje se příznakem `--last-message-to-comment`.

### Transport a autentizace

Současný transport používá:

- outbound WebSocket na `AGENTIS_WS_ENDPOINT`,
- `Authorization: Bearer <token>`,
- `X-Agentis-Adapter-Id: <adapter-id>`,
- HTTP JSON-RPC callbacky na `AGENTIS_ENDPOINT` (z adapteru a případně z
  `agentiscode`),
- `X-Auth-Token` pro běžné callbacky,
- `X-Service-Token` pro service-token metody,
- reconnect s exponenciálním backoffem a heartbeat.

Vlastní systém může tento model převzít, ale názvy hlaviček a rozdělení tokenů
není nutné zachovat. Při nativním forku musí být transportní kontrakt popsán
novou dokumentací a pokrytý integračními testy.

## Minimální kontrakt vlastního systému

Před implementací je nutné rozhodnout, které schopnosti ticketovací systém
skutečně poskytuje. Minimální produkční integrace by měla mít:

- stabilní `ticket_id`, `run_id` a `project_id`,
- možnost založit běh nad taskem,
- možnost poslat follow-up zprávu a případně ID session,
- možnost zrušit běh,
- možnost přijímat průběžné eventy s idempotentním `event_id`,
- možnost přidat komentář s výsledkem,
- možnost změnit status tasku,
- možnost připojit URL, text, obrázky a binární artefakty,
- autentizaci adapteru a oddělení uživatelských a servisních oprávnění,
- mechanismus pro doručení požadavků adapteru: WebSocket, message broker,
  webhook nebo polling.

Následující funkce jsou volitelné, ale jejich absence mění UX:

- session resume,
- followup akce typu merge, close nebo code review,
- otázky a schvalování,
- task headers předávané do workflow jako proměnné,
- task status aliasy `backlog`, `todo`, `in_progress`, `in_review`, `done`,
  `cancelled` a `blocked`,
- více komentářů z jednoho workflow,
- automatický diff jako příloha.

## Doporučená cílová architektura

Workflow runtime by měl znát interní, ticketovací systémově neutrální porty:

```text
TicketingTransport
  - přijme start / message / abort / undo

TicketingContext
  - normalizovaný task, project, run, session, komentáře a přílohy

TicketingReporter
  - event, session, comment, attachment, status, activity

WorkflowManager
  - zůstává zodpovědný za YAML, executory, snapshoty a outputs
```

Prakticky to znamená oddělit:

1. **transport** — jak se požadavek dostane do procesu adapteru,
2. **doménový mapper** — jak se payload ticketovacího systému převede na
   `AgentExecutionContextPayload`,
3. **reporter** — jak se interní event, komentář a artefakt zapíše zpět,
4. **workflow runtime** — co se nad workspace skutečně vykoná.

Workflow runtime by neměl rozhodovat, jestli cílový systém používá REST,
GraphQL nebo JSON-RPC. Stejně tak by neměl obsahovat logiku mapování konkrétních
statusů nebo uživatelů.

## Konkrétní změny v repozitáři

### 1. Konfigurace

V `common/config.py` je potřeba nahradit nebo zobecnit Agentis nastavení:

- `AGENTIS_ENDPOINT` -> endpoint vlastního ticketovacího API,
- `AGENTIS_API_TOKEN` / `AGENTIS_TOKEN` -> token podle nového auth modelu,
- `AGENTIS_SERVICE_TOKEN` -> servisní credential, pokud jej systém rozlišuje,
- `AGENTIS_WS_ENDPOINT` -> transportní endpoint nebo broker,
- `AGENTIS_ADAPTER_ID` -> identita workeru/runneru v novém systému.

Doporučené názvy v nativním forku jsou například `TICKETING_ENDPOINT`,
`TICKETING_API_TOKEN`, `TICKETING_SERVICE_TOKEN`, `TICKETING_WS_ENDPOINT` a
`TICKETING_ADAPTER_ID`. Staré názvy lze ponechat pouze jako dočasnou migrační
vrstvu, ne jako dlouhodobý veřejný kontrakt.

### 2. Vstupní modely

V `common/models.py` je potřeba rozhodnout, zda:

- zachovat `AgentExecutionContextPayload` a přidat mapper z nového payloadu,
  nebo
- přejmenovat modely na neutrální `ExecutionContext`, `StartRequest` a
  `AddMessageRequest`.

Mapper musí explicitně řešit tyto rozdíly:

- string vs. integer identifikátory,
- číslování tasků, pokud nový systém používá pouze UUID,
- statusy a jejich převod na interní status,
- prázdné/null hodnoty,
- pořadí a autorství komentářů,
- přílohy předané při startu a při follow-up zprávě,
- bezpečné zacházení s `headers`.

Nedoporučuje se předávat payload nového systému jako volný `dict` až do
workflow. Pydantic modely mají zůstat hranicí mezi API a interní logikou.

### 3. Reporter a API klient

`common/agentis.py` by měl být nahrazen například modulem
`common/ticketing.py`, který bude mít menší explicitní rozhraní:

```text
post_run_event(...)
store_session_id(...)
add_comment(...)
store_activity(...)
```

Je lepší, aby `WorkflowManager` volal tyto operace přes port nebo service
objekt, než aby znal názvy RPC metod konkrétního systému. Tím se zároveň
oddělí:

- HTTP timeouty a retry,
- autentizační hlavičky,
- idempotence eventů a komentářů,
- serializace artefaktů,
- logování chyb callbacků.

Pokud nový systém nepodporuje JSON-RPC, nahradí se pouze implementace klienta;
workflow nemusí být kvůli tomu přepsáno.

### 4. WebSocket nebo jiný příjem požadavků

`common/rpc/passive_websocket.py` dnes řeší připojení, reconnect, JSON-RPC
validaci a graceful shutdown. Při zachování WebSocket modelu je potřeba změnit:

- endpoint a handshake hlavičky,
- ověřování identity adapteru,
- případný envelope zprávy,
- potvrzení doručení a chování při reconnectu.

Při REST webhooku nebo message brokeru je vhodné transport nahradit novým
modulem, ale zachovat stejné interní handlery `start`, `add_message`, `abort`
a `undo`. Není vhodné vkládat nový transport přímo do `AgentJsonRpcService`.

### 5. Service a názvy v kódu

Agentis-specific názvy jsou rozptýlené minimálně v těchto souborech:

| Soubor | Co bude potřeba řešit |
| --- | --- |
| `common/agentis.py` | HTTP JSON-RPC klient, tokeny a callback allowlist; klient může používat adapter i `agentiscode`. |
| `common/adapter_base.py` | Reporting eventů a log messages. |
| `common/rpc/jsonrpc.py` | Handler lifecycle metod a forwarding chyb. |
| `common/rpc/passive_websocket.py` | Outbound transport a handshake. |
| `common/workflow/manager.py` | Přímé komentáře z outputs, session output, artefakty, followups a eventy. |
| `common/models.py` | Kontext tasku, statusy, přílohy a session. |
| `common/config.py` | Env proměnné a validace credentialů. |
| `app/adapter_api.py` | Tabulka dispatch metod a názvy aplikace. |
| `app/cli.py` | CLI help, metadata a spuštění transportu. |
| `tests/test_agentis_rpc.py` | Testy klienta, auth hlaviček a chyb. |
| `README.md`, `docs/adapter.md` | Veřejný kontrakt a provozní dokumentace. |

Při refaktoru je nutné projít i workflow shell skripty. Hledat je třeba hlavně
`AGENTIS_`, `agentiscode`, `task.add_agent_comment`, `run.adapter_event` a
`run.store_session_id`.

## Workflow a git integrace

Workflow YAML může zůstat stejný pouze tehdy, pokud nový systém dodá stejné
runtime proměnné. V opačném případě je potřeba upravit zejména:

- `TASK_NUMBER`, pokud nový systém nemá číselné číslo tasku,
- `TASK_TITLE`, `TASK_ID` a `RUN_ID`,
- `BASE_BRANCH`, `BRANCH` a `GITHUB_REPO`,
- `AGENTIS_*` proměnné dostupné agentnímu kroku,
- outputs typu `agent_comment`, `session_id`, `url`, `text` a `artifact`,
- followup akce v `workflow.followups`.

GitHub příkazy v ukázkových workflow (`gh pr create`, `gh pr view`) nejsou
součástí adapteru. Pro jiný VCS nebo code-hosting systém je potřeba nahradit:

- vytvoření a push větve,
- otevření merge/pull requestu,
- rebase a kontrolu konfliktů,
- merge do base branche,
- úklid worktree a task branche.

Pokud vlastní ticketovací systém neobsahuje projektový git repozitář, musí
context místo `project_github_repo` předat jiný identifikátor nebo workflow
musí používat vlastní proměnnou. Samotný adapter git operace provádí nad
`context.working_dir`; URL repozitáře a oprávnění k pushi řeší workflow a
prostředí runneru.

## Artefakty, komentáře a session

Před implementací je třeba specifikovat cílové chování pro každý output:

| Interní output | Minimální cílová funkce |
| --- | --- |
| `agent_comment` | Text komentáře a volitelná změna statusu tasku. |
| `session_id` | Uložení session k runu pro další pokračování. |
| `url` | Odkaz s titulkem v komentáři. |
| `text` | Textová příloha nebo rozšíření komentáře. |
| `artifact` | Upload souboru, případně base64/blob reference. |
| `var` | Pouze interní workflow proměnná, do ticket systému se neposílá. |

Nativní integrace by měla mít limity velikosti, počtu a typu artefaktů. Současný
adapter soubory před odesláním base64 agreguje; nový API klient proto musí
řešit, zda:

- podporuje upload přes multipart,
- přijímá base64 přímo v komentáři,
- vyžaduje předchozí upload a následné ID přílohy,
- má maximální velikost komentáře nebo přílohy.

Bez jasné idempotence hrozí při reconnectu nebo retry duplicitní komentáře.
Eventy musí mít stabilní idempotency key, minimálně `run_id` + `event_id`.
Stejné pravidlo je vhodné zavést i pro finální komentáře.

## Bezpečnostní požadavky

Fork nesmí pouze přejmenovat tokeny. Před produkčním nasazením je potřeba
udělat samostatnou bezpečnostní revizi:

- používat TLS (`wss://` nebo HTTPS) mimo lokální vývoj,
- uložit tokeny v secret manageru, ne v repozitáři ani workflow YAML,
- oddělit credential pro příjem tasků od credentialu pro callbacky,
- omezit oprávnění service účtu na konkrétní projekty a operace,
- nikdy neposílat tokeny v `context`, běžných logách ani artefaktech,
- nebrat `headers` z ticketu jako důvěryhodné secret storage,
- sandboxovat nedůvěryhodný kód přes Docker nebo Kubernetes; `local` executor
  běží pod uživatelem adapteru bez izolace,
- omezit síť, CPU, paměť, dobu běhu a velikost artefaktů,
- zajistit izolaci worktree, namespace a branch mezi tasky,
- validovat cesty příloh a outputů proti path traversal,
- auditovat připojení adapteru, starty, aborty, callbacky a změny statusu,
- chránit status HTTP API, pokud obsahuje provozní nebo run metadata,
- definovat chování po restartu, protože registry runů a snapshotů jsou
  in-memory a nepřežijí restart procesu.

## Provozní nasazení

### Požadavky na runner

Runner potřebuje:

- Python 3.13 a instalaci přes Poetry,
- git a přístup k pracovnímu repozitáři,
- persistentní nebo alespoň dostatečně stabilní pracovní disk,
- Docker daemon, pokud se používá `WORKFLOW_EXECUTOR=docker`,
- kube context, RBAC a image registry, pokud se používá Kubernetes,
- síťové spojení k ticketovacímu systému,
- credentialy pro git push, VCS API a volitelného coding agenta,
- monitoring `/health`, `/status` a logů.

### Konfigurace prostředí

Před startem procesu musí být vyřešeno minimálně:

- identity adapteru/runneru,
- API a transportní endpoint ticketovacího systému,
- auth tokeny a jejich rotace,
- `ADAPTER_WORKTREE_ROOT` a práva k adresáři,
- `ADAPTER_PROJECT_RUN_ROOT`,
- executor a jeho image/registry konfigurace,
- `ADAPTER_SHUTDOWN_GRACE_PERIOD`,
- limity reconnectu, heartbeatů a velikosti zpráv,
- veřejná nebo interní URL status API podle monitoringu.

### Restart a škálování

Současný adapter je navržen jako jeden proces s in-memory stavem. Pro první
produkční nasazení je proto vhodné:

- provozovat jeden adapter proces na jednu adapter identity,
- před restartem poslat graceful shutdown,
- počítat s tím, že `undo` po restartu nezná předchozí snapshot,
- neprovádět aktivní run nad taskem paralelně ve více procesech bez další
  koordinace.

Horizontální škálování vyžaduje řešit výhradní vlastnictví tasku, sdílený stav
aktivních runů, deduplikaci eventů a oddělení worktree. To není pouze změna
deployment manifestu.

## Doporučený postup implementace

### Fáze 0: rozhodnutí o kompatibilitě

- Sepsat cílové API a auth model ticketovacího systému.
- Rozhodnout, zda zachovat JSON-RPC a WebSocket, nebo použít nativní transport.
- Rozhodnout, zda nový systém podporuje session, activity, followups a artefakty.
- Definovat mapování statusů a životního cyklu runu.

### Fáze 1: interní neutrální kontrakt

- Vytvořit ticketingové Pydantic modely nebo mapper do existujících modelů.
- Vytvořit `TicketingReporter` interface/protocol.
- Přesunout reporting z přímého Agentis klienta za tento interface.
- Zachovat `WorkflowManager` bez znalosti konkrétního API.

### Fáze 2: transport a callbacky

- Implementovat příjem `start`, `add_message`, `abort` a `undo`.
- Implementovat autentizaci, reconnect nebo retry podle nového transportu.
- Implementovat event, completion comment, status, session a artifact callbacky.
- Přidat idempotenci a korelaci přes `run_id`/`event_id`.

### Fáze 3: workflow projektu

- Přepsat `.agentis/workflows/*.yaml` na příkazy a env nového prostředí.
- Nahradit GitHub-specific kroky nebo je odstranit.
- Zajistit image, registry, git credentialy a agentní nástroje.
- Otestovat failure, abort, retry, followup a restart scénáře.

### Fáze 4: provoz a rollout

- Nasadit nejprve jeden canary runner a testovací projekt.
- Ověřit obousměrnou komunikaci přes skutečný ticket.
- Změřit dobu startu, callback latenci, velikost artefaktů a chování při výpadku API.
- Teprve poté povolit produkční projekty a případně více runnerů.

## Akceptační testy

Před ostrým provozem musí projít minimálně tyto scénáře:

1. `start` založí workspace a ticket dostane start a step eventy.
2. Úspěšný workflow přidá komentář, změní status a přiloží odkaz.
3. Selhaný krok vytvoří failure event a failure komentář bez falešného `done`.
4. `abort` zastaví lokální proces, Docker kontejner i Kubernetes Job podle executorů.
5. `add_message` naváže na správný `run_id` a session, pokud je podporována.
6. Opakovaný event nebo callback nevytvoří duplicitu.
7. Výpadek ticketovacího API aktivuje retry/reconnect a nezpůsobí únik tokenu do logu.
8. Příloha s nepovolenou cestou se odmítne a běžný artefakt se bezpečně nahraje.
9. Dva paralelní starty stejného tasku skončí deterministicky jako busy nebo
   jako jeden přijatý běh.
10. Graceful shutdown nechá doběhnout povolenou práci a nepřijímá nové tasky.
11. Restart runneru nezpůsobí neomezené duplikování komentářů ani eventů.
12. Run s neexistujícím nebo neplatným workflow vrátí srozumitelnou chybu tasku.

Stávající testovací základ je vhodné zachovat a rozšířit o testy nového
ticketing klienta. Minimálně je potřeba pokrýt `tests/test_agentis_rpc.py`,
`tests/test_passive_websocket.py`, `tests/test_workflow.py`, `tests/test_api.py`
a end-to-end testy přes reálný nebo contract-test mock ticketovacího API.

## Orientační rozsah

Rozsah závisí hlavně na tom, zda vlastní systém již má push transport,
idempotentní eventy a upload příloh. Relativní odhad:

| Varianta | Rozsah |
| --- | --- |
| Kompatibilní fasáda s Agentis kontraktem | Malý až střední; více práce je na serverové fasádě než v adapteru. |
| Nativní REST/GraphQL integrace bez session a artefaktů | Střední; mapper, klient, transport, statusy a testy. |
| Plná integrace včetně session, activity, followups, artefaktů a VCS | Velký; jde o nový produktový integrační modul, ne jen fork konfigurace. |
| Produkční Kubernetes provoz a horizontální škálování | Samostatná infrastruktura, security review a provozní práce nad rámec kódu. |

Největší riziko není samotné spuštění bash kroku. Je jím přesná a idempotentní
replikace lifecycle tasku: přijetí požadavku, průběžný stav, výsledek,
komentář, přílohy, session, abort a obnova po reconnectu nebo restartu.

## Checklist pro převzetí

- [ ] Je zdokumentován cílový transport a autentizace.
- [ ] Existuje stabilní mapování task/run/project/session ID.
- [ ] Je definováno mapování statusů a chování při failure/abort.
- [ ] Je implementován reporter pro eventy, komentáře a artefakty.
- [ ] Je vyřešena idempotence eventů a callbacků.
- [ ] Jsou odděleny uživatelské a servisní credentialy.
- [ ] Jsou upraveny Agentis-specific env proměnné a workflow skripty.
- [ ] Je rozhodnuto o session, activity, followups, questions a approvals.
- [ ] Je zajištěna izolace worktree a executorů.
- [ ] Je připraven monitoring, alerting a graceful shutdown.
- [ ] Prošly unit, contract, integrační a end-to-end testy.
- [ ] Proběhl canary rollout na testovacím projektu.
