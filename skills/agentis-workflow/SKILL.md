---
name: agentis-workflow
description: Návrh, psaní, úprava a validace Agentis workflow — deklarativních YAML souborů `.agentis/workflows/*.yaml`, které spouští Agentis Adapter (kroky v bashi, Kubernetes/Docker/local executor, outputs do Agentisu, followup akce, approval). Použij vždy, když uživatel chce vytvořit nebo upravit workflow, automatizaci, pipeline, report, mailing, periodický úkol nebo „krok“ běžící přes Agentis adapter — i když neřekne slovo „workflow“ (např. „udělej mi denní report trendů do mailu“, „přidej krok s testy před agentem“, „chci tlačítko Deploy po dokončení tasku“, „proč se mi nepropsal komentář z workflow“).
---

# Agentis workflow

Agentis Adapter (`/var/www/agentis-adapter`) přijme od Agentisu úkol, načte YAML workflow z projektu a
spustí jeho kroky. Každý krok je bash skript; s Agentisem komunikuje přes soubory deklarované v `outputs`
(komentář do tasku, přílohy, proměnné pro další kroky). Adapter sám žádnou byznys logiku nemá — co se
stane, určuje jen YAML.

Autoritativní zdroje (když si nejsi jistý detailem, čti je, ne paměť):

- `/var/www/agentis-adapter/docs/workflow.md` — kompletní reference (česky).
- `/var/www/agentis-adapter/common/workflow/schema.py` — Pydantic schema; co tu není, YAML nesmí obsahovat.
- `/var/www/agentis-adapter/.agentis/workflows/` — reálné produkční příklady (default, merge, close, slack).
- `references/patterns.md` v tomto skillu — hotové vzory kroků (fetch → LLM → e-mail, agent, paralelní
  kroky, failure report, podmínky, approval). Čti, když skládáš konkrétní kroky.

## Postup

### 1. Ujasni zadání

Než začneš psát, potřebuješ vědět:

- **Co má workflow udělat** krok po kroku (zdroj dat → zpracování → výstup).
- **Kde má výsledek skončit**: komentář v tasku Agentisu, e-mail, soubor, PR, Slack…
- **V jakém projektu** (adresář s `.agentis/workflows/`) a **jak se bude spouštět** — to určuje jméno
  souboru (viz tabulka níže).
- Potřebné tajné údaje (SMTP, API klíče) — nikdy je nepiš do YAML; patří do host-side `envFiles`.

Co jde odvodit z repozitáře (image, mounty, existující `_base.yaml`), odvoď sám a neptej se.

### 2. Zvol jméno souboru a scope

| Soubor | Kdy se spustí | Output root (viz krok 5) |
| --- | --- | --- |
| `default.yaml` | běžný task run (worktree + git větev) | **worktree** |
| `project.yaml` | task se `scope = project` (běží přímo v adresáři projektu, bez gitu) | **run adresář** |
| `<jméno>.yaml` | pojmenované workflow z `context.adapter.workflow` — followup akce / task akce s polem `workflow` | **run adresář** |
| `approval.yaml` | synchronní RPC `approve` (rozhodnutí 1/0) | **run adresář** |
| `_base.yaml` | jen rodič pro `extends`, bez `steps`, sám nespustitelný | — |

Projektový soubor `.agentis/workflows/<jméno>.yaml` má přednost před bundled souborem stejného jména
v `/var/www/agentis-adapter/workflows/`. Jméno smí obsahovat jen `[A-Za-z0-9_.-]` (validace v Agentisu).

Pojmenované workflow (např. `github-trends.yaml`) se spustí, když Agentis pošle `start` s
`context.adapter.workflow = "github-trends"`. V Agentisu se to nastaví jedním z těchto způsobů:

- **Adapter option** `workflow = <jméno>` na tasku — nebo na **opakovaném tasku** (recurring task:
  `frequency` + čas běhu; options se zkopírují do každého vygenerovaného tasku), případně na agentovi,
  prostředí či adapteru (merge v tomto pořadí, task vyhrává). Typická cesta pro „každý den / každý týden“.
- **Followup akce** jiného workflow (`workflow.followups`) nebo task akce s polem `workflow` — tlačítko
  v komentáři; má přednost před adapter option.

Task s vypnutým `worktree` běží jako project scope (bez git worktree). U pojmenovaného workflow je output
root vždy run adresář, bez ohledu na scope. Způsob spuštění vždy uveď v závěrečném shrnutí.

### 3. Vyjdi z existujícího základu

Podívej se do `<projekt>/.agentis/workflows/`. Existuje-li `_base.yaml`, použij `extends: _base` —
zdědíš image, `imagePullSecrets`, `envFiles`, mounty a šablonu `run-agent`, a workflow zůstane krátké.
Pozor: `steps` a `followups` se nedědí nikdy; `env` se merguje po klíčích; `mounts`/`envFiles`/
`imagePullSecrets` se spojují (stejné `name` se přepíše). Dědičnost má jen jednu úroveň.

Když `_base.yaml` neexistuje, začni z `assets/template-report.yaml` (vzor: fetch → LLM → e-mail) nebo
zkopíruj infrastrukturní hlavičku z `/var/www/agentis-adapter/workflows/_base.yaml`.

### 4. Napiš kroky

Každý `run` běží v bash wrapperu `set -euo pipefail` a `cd` do `workingDir`. Z toho plyne:

- Nedefinovaná proměnná shodí krok → volitelné hodnoty piš jako `${VAR:-default}` / `${VAR:+--flag "$VAR"}`.
- Selhání kdekoli v pipe shodí krok → u příkazů, kde je chyba přijatelná, použij `|| true` vědomě.
- Velké inline skripty (Python přes heredoc `<<'PY'`) jsou v pořádku; uvozovkovaný delimiter zabrání
  shellové expanzi uvnitř. YAML block scalar (`run: |`) odstraní jen společné odsazení, takže ukončovací
  `PY` musí být na stejné úrovni jako první řádek skriptu — ne odsazený uvnitř `if`/`{ … }` bloku,
  jinak heredoc neskončí. Validátor to nepozná; složitější logiku raději dej do jednoho heredocu
  na nejvyšší úrovni skriptu.
- Když LLM vybírá nebo hodnotí data, chtěj po něm strukturovaný JSON (ID + zdůvodnění) a komentář/e-mail
  z něj sestav deterministicky v dalším kroku — LLM si pak nevymyslí odkazy a formát je stabilní
  (vzor 2b v `references/patterns.md`). Tvar JSON vynucuj JSON Schematem v `.agentis/schemas/<jméno>.schema.json`
  (`agent.outputSchema`, viz níže), ne popisem „odpověz POUZE JSON v tomto tvaru“ v promptu; povolená ID
  dej do schématu jako `enum` (vygeneruj kopii schématu do run adresáře v předchozím kroku).
  Volný text/HTML přímo od LLM je v pořádku u čistě textových reportů.

Prostředí, které krok dostane automaticky: `AGENTIS_RUN_DIR`, `AGENTIS_PROMPT_FILE` (text zadání z tasku),
`AGENTIS_CONTEXT_FILE` (JSON kontext), `AGENTIS_RUN_ID`, `AGENTIS_TASK_ID`, `AGENTIS_PROJECT_ID`,
všechny interpolační tokeny jako env (`WORKDIR`, `BRANCH`, `TASK_TITLE`…), volitelně `AGENTIS_MODEL`,
`AGENTIS_SESSION_ID`, `AGENTIS_EFFORT`, `AGENTIS_ENDPOINT`, `AGENTIS_SERVICE_TOKEN`, a `var` outputs
kroků, na kterých krok (transitivně) závisí.

Interpolace `[%TOKEN%]` v YAML se provede při načtení souboru. Povolené tokeny jsou jen `NAMESPACE`,
`WORKDIR`, `RUN_DIR`, `MAIN_DIR`, `RUN_ID`, `TASK_ID`, `TASK_NUMBER`, `TASK_TITLE`, `BRANCH`,
`BASE_BRANCH`, `GITHUB_REPO` — cokoli jiného je chyba. Uvnitř `run` skriptů preferuj env proměnné
(`"$WORKDIR"`) před tokeny: hodnota (např. název tasku) pak nejde přes shell parsing.

Agent/LLM krok voláš přes `agentiscode` (wrapper nad OpenCode a Claude Code). Preferuj deklarativní krok
`agent:` (adapter, model, `prompt`/`promptFile`, `inputs`, `outputSchema`, `output`) — adapter z něj vygeneruje
bash sám, s telemetrií do tasku a bezpečným quotováním; v hodnotách se expandují jen `$NAME`/`${NAME}`/
`${NAME:-text}`. Se `outputSchema` je `output` zvalidovaný JSON (při chybě agent dostane opravný turn přes
`--resume`, když nepomůže, krok skončí exit 3). Kompletní pole: sekce „Agentní kroky (`agent`)“ v
`docs/workflow.md`. Alternativně šablona `uses: run-agent` z `_base.yaml`, nebo ruční `run` — prompt na stdin,
`--final-output` uloží finální odpověď do souboru, který pak publikuješ jako `agent_comment`. Detaily
a varianty v `references/patterns.md`.

### 5. Outputs — nejčastější zdroj „nic se nestalo“

Outputs jsou cesty **relativní k output rootu** a nesmí z něj utéct:

- `default.yaml` (task scope): root = **worktree** → zapisuj do `.agentis/outputs/…`, deklaruj
  `bodyFrom: .agentis/outputs/…`.
- `project.yaml`, pojmenovaná workflow, approval: root = **run adresář** (`$AGENTIS_RUN_DIR`) →
  zapisuj do `"$AGENTIS_RUN_DIR/outputs/…"`, deklaruj `bodyFrom: outputs/…`.

Když krok zapíše soubor jinam, než ukazuje deklarace, output se potichu nepropíše.

| Typ | Pole | Co udělá |
| --- | --- | --- |
| `agent_comment` | `bodyFrom`, `status`, volitelně `name` | komentář do tasku + status (`done`, `in_review`, `blocked`, …); `name` = zobrazené jméno autora |
| `url` / `text` | `label`, `valueFrom` | příloha komentáře |
| `artifact` | `name`, `path` (glob) | soubor(y) přiložené ke komentáři; glob drž úzký |
| `session_id` | `valueFrom` | uloží session pro pozdější resume agenta |
| `var` | `name`, `valueFrom` | proměnná pro `if` a env závislých kroků |

Outputs se aplikují až po konci workflow a jen za **úspěšné, nepřeskočené** kroky (i když workflow jako
celek selže). Když vznikne více `agent_comment`, přílohy dostane poslední.

### 6. Řízení toku (jen když je potřeba)

- `needs: [...]` → DAG a paralelismus (`needs: []` = startuje hned); bez `needs` běží kroky sekvenčně.
  Při použití `needs` musí být jména kroků unikátní a odkazovat jen nahoru.
- `if: VAR == 'x' && !OTHER` — gramatika: `VAR`, `!VAR`, `==`, `!=`, `&&` (váže silněji), `||`, bez závorek.
- `continueOnError: true`, `retries: N`, `always: true` (úklid / failure komentář; má `AGENTIS_WORKFLOW_STATUS`
  a `AGENTIS_FAILED_STEP`), `timeoutSeconds` na kroku.
- `workflow.followups` — tlačítka v completion komentáři spouštějící jiné pojmenované workflow; nabízí se
  jen po úspěšném runu.

### 7. Validuj

Vždy spusť validátor — používá přesně loader adapteru (extends, uses, tokeny, striktní schema, needs, if):

```bash
cd /var/www/agentis-adapter && poetry run python \
  skills/agentis-workflow/scripts/validate_workflow.py <projekt>/.agentis/workflows/<jméno>.yaml
```

Validátor nekontroluje, zda image obsahuje použité nástroje, zda existují `envFiles` a obsahují potřebné
klíče, a zda jsou cesty v outputs konzistentní se zápisem v `run`. Tyto věci zkontroluj ručně; u envFiles
jen jména klíčů, nikdy hodnoty: `grep -o '^[A-Za-z_][A-Za-z0-9_]*=' /root/.config/agentis/agentis.env`.
Chybí-li klíč (např. `SMTP_SERVER`), řekni to uživateli — nedoplňuj tajné hodnoty sám. Pokud jde krok bezpečně
vyzkoušet lokálně (čisté čtení dat, bez odesílání e-mailů/PR), můžeš jeho bash spustit ručně s nastaveným
`AGENTIS_RUN_DIR=$(mktemp -d)`; kroky s vedlejšími účinky (e-mail, push, deploy) bez souhlasu uživatele
nespouštěj.

### 8. Shrnutí pro uživatele

Uveď: cestu k souboru, co jednotlivé kroky dělají, výsledek validace, jaké tajné hodnoty/env musí být
v `envFiles` (a které tam chybí), a jak workflow spustit (task/opakovaný task s adapter option
`workflow = <jméno>`, followup akce, nebo `default.yaml`/`project.yaml` podle scope).

## Pravidla, která drží workflow bezpečné a funkční

- **Tajné údaje nikdy do YAML ani do logu** (žádné `echo $TOKEN`, žádné `set -x` kolem secretů). Patří do
  host-side `envFiles` (např. `/root/.config/agentis/agentis.env`); adapter je načte na hostu a předá krokům.
- **Nedůvěryhodný text (prompt, název tasku, metadata approvalu) nevkládej do shellu jako kód.** Čti ho
  z `$AGENTIS_PROMPT_FILE`/env a předávej jako data (stdin, argumenty v uvozovkách, `jq --arg`).
- **Konfigurovatelné hodnoty** (příjemce e-mailu, URL, model) dej do `workflow.env`, ne natvrdo do skriptu
  — workflow se pak dá znovu použít a hodnoty jsou vidět na jednom místě.
- `kubernetes` a `docker` executor vyžadují `image` u každého kroku (zděděnou nebo vlastní). `local`
  executor není sandbox — běží přímo jako uživatel adapteru.
- Workflow se načte a zmrazí na začátku runu; úprava YAML během běhu nic nezmění.
- Neznámé klíče v YAML jsou chyba (`extra="forbid"`); výjimkou jsou jen položky `mounts`
  (Kubernetes volume source).

## Časté chyby

| Příznak | Příčina |
| --- | --- |
| `Unknown workflow token [%X%]` | token mimo allowlist — použij env proměnnou |
| `Extra inputs are not permitted` | překlep v klíči nebo pole, které schema nezná |
| `unknown or future 'needs'` | `needs` ukazuje na neexistující nebo níže definovaný krok |
| `Workflow executor 'kubernetes' vyžaduje 'image'` | chybí `image` na kroku i ve workflow |
| `… ani zabalený fallback …` | soubor chybí v projektu i v bundled `workflows/`; zkontroluj jméno a scope |
| komentář/příloha se neobjevila | krok selhal/přeskočen, nebo nesedí output root (`.agentis/outputs` vs `outputs/`) |
| krok padá na `unbound variable` | `set -u` — použij `${VAR:-}` |
