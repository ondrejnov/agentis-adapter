# Testování workflow před nasazením

Workflow (`.agentis/workflows/*.yaml`) se dá vyzkoušet bez Agentisu příkazem `agentis-adapter workflow`:

```bash
cd /var/www/muj-projekt              # kdekoli uvnitř projektu s .agentis/workflows
agentis-adapter workflow list        # jaká workflow projekt má
agentis-adapter workflow validate    # zkontroluje všechna workflow projektu
agentis-adapter workflow run ai-news "Zaměř se na nové modely"
```

`run` pošle stejný JSON-RPC `start`, jaký posílá Agentis, a vede ho stejnou cestou adapterem. Rozdíl je jen
v tom, že eventy kroků, komentáře do tasku a přílohy se místo do Agentisu **vypíší do terminálu**:

```
Workflow ai-news  (/var/www/agentis-general/.agentis/workflows/ai-news.yaml)
projekt /var/www/agentis-general · scope project · runtime local · run dev-run-3f2a…

▶ Fetch AI news
✓ Fetch AI news (8.2s)
▶ Select and summarize
✓ Select and summarize (41.0s)
▶ Render newsletter
✓ Render newsletter (0.1s)
↷ Krok přeskočen (if: SEND_EMAIL): Send newsletter email

┌─ Komentář do tasku — autor: AI newsletter, status: done
│ # AI novinky – 4. 10. 2026
│ …
│ artefakt: newsletter.html
└─

✓ Workflow doběhlo za 49.6s
  run adresář: /tmp/agentis/dev-run-3f2a…/1a105b…
  logy:        /tmp/agentis/dev-run-3f2a…/1a105b…/logs
  outputs:     /tmp/agentis/dev-run-3f2a…/1a105b…/outputs (items.json, newsletter.html, …)
```

U selhaného kroku se vypíše konec jeho logu. Exit kód je `0` při úspěchu, `1` při selhání workflow
a `2` při chybném vstupu (neexistující workflow, špatná cesta). `Ctrl+C` běžící workflow zastaví.

## `workflow run`

```
agentis-adapter workflow run WORKFLOW [PROMPT] [volby]
```

`WORKFLOW` je jméno (`ai-news`) nebo cesta k YAML (`.agentis/workflows/ai-news.yaml`). Projekt se najde
podle nejbližšího nadřazeného adresáře s `.agentis/workflows`, nebo ho určí `-C`.

| Volba | Výchozí | Význam |
| --- | --- | --- |
| `PROMPT` | prázdný | Text zadání → `$AGENTIS_PROMPT_FILE` |
| `-f, --prompt-file PATH` | — | Zadání ze souboru, `-` = stdin |
| `-C, --project DIR` | nejbližší projekt | Adresář projektu |
| `-r, --runtime` | `local` | `local` = bash na hostu, `docker` = kontejnery, `workflow` = executor z YAML / `WORKFLOW_EXECUTOR` (typicky Kubernetes) |
| `-m, --model` | nenastaveno | `AGENTIS_MODEL`; bez něj platí model nastavený ve workflow |
| `-e, --effort` | nenastaveno | `AGENTIS_EFFORT` |
| `--scope` | `project` | `task`/`worktree` vytvoří git worktree a větev jako běžný task run |
| `--title` | `Test workflow <jméno>` | Název tasku (`TASK_TITLE`) |
| `--full` | vypnuto | Vypíše komentáře celé (jinak prvních 60 řádků) |
| `--agentis` | vypnuto | Pošle eventy a outputs do skutečného Agentisu (`AGENTIS_ENDPOINT`) |

Jména `default` a `project` Agentis nevolá jménem, vybírá je scope. `run default` proto běží v task
scope (vytvoří worktree a větev), `run project` v project scope.

### Volba runtime

- **`local`** — kroky běží jako bash procesy přímo na hostu, pod aktuálním uživatelem, bez sandboxu.
  Nejrychlejší pro ladění skriptů. `image`, `mounts` a `imagePullSecrets` se ignorují, takže **neověří**,
  že image obsahuje potřebné nástroje.
- **`docker`** — každý krok v kontejneru přes `docker run`. Ověří image bez Kubernetes.
- **`workflow`** — executor podle YAML (Kubernetes Joby). Nejvěrnější produkci.

Než workflow nasadíš, pusť ho aspoň jednou ve stejném executoru, v jakém poběží v produkci.

## `workflow validate`

```bash
agentis-adapter workflow validate                 # všechna workflow projektu (kromě _base apod.)
agentis-adapter workflow validate ai-news merge   # jen vybraná
```

Načte YAML stejným loaderem jako adapter: syntaxe, `extends`, `uses`, povolené `[%TOKEN%]`, striktní
schema (neznámé klíče), `needs` a `if`. Navíc ověří, že soubory v `envFiles` existují. Nekontroluje
obsah image ani to, zda cesty v `outputs` odpovídají souborům, které kroky zapisují — to ukáže `run`
(když workflow deklaruje komentář a žádný nevznikne, `run` na to upozorní).

Ve VS Code totéž průběžně hlásí rozšíření z `vscode-agentis-workflow/` (`dist/agentis-workflow.vsix`).

## Kroky s vedlejšími účinky

`run` vykoná **všechny** kroky, včetně odeslání e-mailu, pushe nebo deploye. Takové kroky dej pod
podmínku a v testu proměnnou nenastavuj:

```yaml
workflow:
  env:
    SEND_EMAIL: "1"      # v produkci; pro test zakomentuj nebo nastav na ""
  steps:
    - name: Send newsletter email
      if: SEND_EMAIL
```

Holá podmínka bere chybějící, prázdnou, `0`, `false` i `no` hodnotu jako nepravdu, takže se krok přeskočí
(ve výpisu `↷`). Alternativně přesměruj příjemce přes `workflow.env` na vlastní adresu.

## Kde hledat výsledky

Pojmenovaná workflow a project scope zapisují do run adresáře (`ADAPTER_PROJECT_RUN_ROOT`,
default `/tmp/agentis`):

```
/tmp/agentis/<run_id>/<attempt_id>/
├── logs/<job>.log      # stdout/stderr každého kroku (lokální executor)
└── outputs/            # soubory, které kroky zapsaly do $AGENTIS_RUN_DIR/outputs
```

Cestu vypíše `run` na konci. V task scope (`run default`) leží outputs ve worktree v `.agentis/outputs/`.

## Instalace příkazu

`agentis-adapter` je entrypoint z `pyproject.toml`. V repu adapteru funguje přes
`/var/www/agentis-adapter/.venv/bin/agentis-adapter` (nebo `poetry run agentis-adapter`, ale pozor:
`poetry -C …` mění pracovní adresář, takže pak použij `-C <projekt>`). Pro globální příkaz nainstaluj
adapter přes pipx jako editable, aby se změny v repu projevily bez reinstalace:

```bash
pipx install --force -e /var/www/agentis-adapter
```

## Nízkoúrovňová varianta

`scripts/mock_workflow_request.py` sestaví a odešle surový `start` payload se všemi poli kontextu
(Slack hlavičky, `--task-id`, `--session-id`, `--print-only` pro výpis JSON). Hodí se pro ladění
protokolu; pro běžné testování workflow používej `agentis-adapter workflow run`.

## Kontrolní seznam

- [ ] `agentis-adapter workflow validate` prošel
- [ ] `agentis-adapter workflow run <jméno>` skončil `✓` a komentář vypadá podle očekávání
- [ ] Soubory v run adresáři (`outputs/`) mají očekávaný obsah
- [ ] Běh aspoň jednou ve stejném executoru jako v produkci (`-r workflow` / `-r docker`)
- [ ] Kroky s vedlejšími účinky byly při testu vypnuté nebo přesměrované
