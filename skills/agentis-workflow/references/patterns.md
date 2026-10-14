# Vzory kroků Agentis workflow

Všechny příklady předpokládají **run adresář jako output root** (`project.yaml`, pojmenovaná workflow).
V task scope (`default.yaml`) zapisuj do `.agentis/outputs/…` a deklaruj stejné relativní cesty.

## Obsah

1. Agent nad promptem tasku (šablona `run-agent`)
2. Přímé volání `agentiscode` s vlastním vstupem (2b: LLM vybírá, renderuje kód)
3. Proměnné mezi kroky + podmínky
4. Paralelní kroky
5. Failure komentář přes `always`
6. Přílohy: odkaz, text, artefakty
7. Followup akce (tlačítka po dokončení)
8. Approval workflow
9. Kroky v jiném adresáři / nad hlavním repem

---

## 1. Agent nad promptem tasku (šablona `run-agent`)

S `extends: _base` stačí:

```yaml
- name: Run agent
  uses: run-agent
```

Šablona čte prompt z `$AGENTIS_PROMPT_FILE`, model z `AGENTIS_MODEL` (Claude modely → adapter `claude`,
jinak `opencode`), navazuje session přes `AGENTIS_SESSION_ID` a publikuje `agent_comment` +
`session_id`. Parametry přes env: `RUN_AGENT_FLAGS`, `RUN_AGENT_OUTPUT_DIR`, `RUN_AGENT_STREAM_FILTER`.
V task scope je nutné přepsat výstupní adresář **i** outputs (deklarované outputs nahradí šablonu celé):

```yaml
- name: Run agent
  uses: run-agent
  env:
    RUN_AGENT_OUTPUT_DIR: .agentis/outputs
  outputs:
    - type: agent_comment
      bodyFrom: .agentis/outputs/final-comment.md
      status: in_review
    - type: session_id
      valueFrom: .agentis/outputs/session-id
```

## 2. Přímé volání `agentiscode` s vlastním vstupem (2b: LLM vybírá, renderuje kód)

Když LLM zpracovává data z předchozího kroku (report, shrnutí, klasifikace):

```yaml
- name: Summarize
  run: |
    { cat "$AGENTIS_PROMPT_FILE"; printf '\n---\n'; cat "$AGENTIS_RUN_DIR/outputs/data.json"; } \
      | agentiscode --adapter opencode --json \
          --model "${AGENTIS_MODEL:-openai/gpt-5.4-mini}" \
          ${AGENTIS_EFFORT:+--effort "$AGENTIS_EFFORT"} \
          --run-id "$AGENTIS_RUN_ID" --task-id "$AGENTIS_TASK_ID" \
          --final-output "$AGENTIS_RUN_DIR/outputs/summary.md"
  outputs:
    - type: agent_comment
      bodyFrom: outputs/summary.md
      status: done
```

Užitečné volby: `--adapter opencode|claude`, `--model`, `--effort`, `--cwd`, `--timeout`, `--resume`,
`--session-output PATH`, `--json` (stream eventů na stdout = vidět v logu kroku). Prompt jde na stdin
nebo jako poziční argument. Formát výstupu (Markdown, HTML pro e-mail…) si řekni v promptu.

### 2b. LLM vybírá, výstup se renderuje deterministicky

LLM vrátí jen JSON s ID položek; titulky a odkazy se doplní ze stažených dat, takže nic nevymyslí.

```yaml
- name: Select items
  run: |
    {
      printf 'Vyber nejvýše 5 položek. Odpověz POUZE JSON polem [{"id": ..., "why": "..."}].\n'
      printf 'Doplňující preference uživatele:\n'; cat "$AGENTIS_PROMPT_FILE"
      printf '\nData:\n'; cat "$AGENTIS_RUN_DIR/outputs/items.json"
    } | agentiscode --adapter opencode --model "$REPORT_MODEL" \
          --run-id "$AGENTIS_RUN_ID" --task-id "$AGENTIS_TASK_ID" \
          --final-output "$AGENTIS_RUN_DIR/outputs/selection.json"

- name: Render digest
  run: |
    python3 - <<'PY'
    import html, json, os, re
    from pathlib import Path
    out = Path(os.environ["AGENTIS_RUN_DIR"]) / "outputs"
    raw = (out / "selection.json").read_text(encoding="utf-8")
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())   # LLM občas obalí do ```json
    items = {str(i["id"]): i for i in json.loads((out / "items.json").read_text())}
    picked = [(items[str(s["id"])], s.get("why", "")) for s in json.loads(raw) if str(s.get("id")) in items]
    (out / "digest.md").write_text("\n".join(f"- [{i['title']}]({i['url']}) — {w}" for i, w in picked))
    (out / "digest.html").write_text("<ul>" + "".join(
        f'<li><a href="{html.escape(i["url"])}">{html.escape(i["title"])}</a> — {html.escape(w)}</li>'
        for i, w in picked) + "</ul>")
    PY
  outputs:
    - type: agent_comment
      bodyFrom: outputs/digest.md
      status: done
```

## 3. Proměnné mezi kroky + podmínky

```yaml
- name: Detect changes
  run: |
    mkdir -p "$AGENTIS_RUN_DIR/outputs"
    if git diff --quiet "origin/$BASE_BRANCH"...HEAD; then printf '' ; else printf 'true'; fi \
      > "$AGENTIS_RUN_DIR/outputs/has-changes"
  outputs:
    - type: var
      name: HAS_CHANGES
      valueFrom: outputs/has-changes

- name: Run tests
  if: HAS_CHANGES
  run: poetry run pytest -q
```

`var` se čte hned po úspěchu kroku; závislé kroky ho dostanou v `if` i jako env (`$HAS_CHANGES`).
Holý test `VAR` bere `""`, `0`, `false`, `no` jako nepravdu. `if` může použít i env (`AGENTIS_MODEL`,
`workflow.env`) a built-in tokeny (`GITHUB_REPO`, `BRANCH`…).

## 4. Paralelní kroky

```yaml
workflow:
  maxParallel: 3
  steps:
    - name: Fetch A
      needs: []
      run: ...
    - name: Fetch B
      needs: []
      run: ...
    - name: Merge results
      needs: ["Fetch A", "Fetch B"]
      run: ...
```

Paralelní kroky nesmí zapisovat stejný soubor. `var` z paralelní větve vidí jen kroky, které na ní přes
`needs` závisí.

## 5. Failure komentář přes `always`

```yaml
- name: Report failure
  always: true
  run: |
    [ "${AGENTIS_WORKFLOW_STATUS:-success}" = "failed" ] || exit 0
    mkdir -p "$AGENTIS_RUN_DIR/outputs"
    printf '❌ Workflow selhalo v kroku „%s“.\n' "$AGENTIS_FAILED_STEP" > "$AGENTIS_RUN_DIR/outputs/failure.md"
  outputs:
    - type: agent_comment
      bodyFrom: outputs/failure.md
      status: blocked
```

Po úspěšném runu soubor nevznikne → prázdný komentář se vynechá. `AGENTIS_WORKFLOW_STATUS` nelze použít
v `if` (přidává se až po jeho vyhodnocení), proto test v `run`.

## 6. Přílohy

```yaml
outputs:
  - type: url
    label: Pull request
    valueFrom: outputs/pr-url
  - type: text
    label: Test log
    valueFrom: outputs/pytest.txt
  - type: artifact
    name: Screenshots
    path: outputs/screens/*.png
```

## 7. Followup akce

```yaml
workflow:
  followups:
    - title: Odeslat znovu
      prompt: Pošli report ještě jednou.
      workflow: github-trends          # spustí .agentis/workflows/github-trends.yaml
      continue_previous_run: false
    - title: Git merge
      if: PR_CREATED && !AGENTIS_AUTO_MERGE
      prompt: Sloučit změny do hlavní větve.
      workflow: merge
```

Followups se nabízí jen po úspěšném runu a nedědí se přes `extends`.

## 8. Approval workflow

Synchronní rozhodnutí pro RPC `approve`. Vstup: `AGENTIS_APPROVE_METADATA` (JSON string, typicky
`{"command": "..."}`). Musí existovat právě jeden `var` output `APPROVED` s hodnotou `1`/`0`, volitelně
jeden `APPROVED_METADATA` (JSON objekt). Příkaz nikdy nevykonávej ani neinterpoluj do shellu — zpracuj
ho jako data přes `jq`. Referenční implementace: `/var/www/agentis-adapter/workflows/approval_ai.yaml`.

```yaml
- name: Review command
  run: |
    command="$(jq -r '.command // ""' <<< "$AGENTIS_APPROVE_METADATA")"
    if [[ "$command" == "git status --short" ]]; then echo 1; else echo 0; fi > "$AGENTIS_RUN_DIR/approved.data"
  outputs:
    - type: var
      name: APPROVED
      valueFrom: approved.data
```

## 9. Kroky v jiném adresáři

`workingDir` na kroku přepíše výchozí adresář, např. `workingDir: "[%MAIN_DIR%]"` pro operace nad hlavním
checkoutem projektu (merge, úklid worktree). V kontejnerových executorech musí být adresář dostupný přes
mount (`/var/www` z `_base.yaml`); `WORKDIR`, `AGENTIS_RUN_DIR` a `MAIN_DIR` Docker executor mountuje sám.
