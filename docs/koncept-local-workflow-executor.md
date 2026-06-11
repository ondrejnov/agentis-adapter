# Koncept: lokální executor pro workflow režim

> **Stav: implementováno.** Viz `common/workflow/local_runtime.py`,
> `WORKFLOW_EXECUTOR` v `common/config.py` a sekci „Workflow runtime"
> v `docs/development.md`.

## Motivace

Workflow režim (`context.adapter.runtime = "workflow"`) dnes spouští kroky z
`.agentis/workflows/default.yaml` výhradně jako Kubernetes Joby přes `kubectl`.
Deklarativní část (sekvenční kroky, `if` podmínky, `var`/`agent_comment`/`artifact`
outputs, interpolace `[%TOKEN%]`, prompt/context soubory) ale na Kubernetes nijak
nezávisí — dává smysl umět tytéž kroky spustit jako lokální bash procesy přímo
nad worktree/projektovým adresářem, stejně jako běží `local` runtime.

Cíl: jedna konfigurační volba rozhodne, jestli workflow poběží v Kubernetes,
nebo lokálně v bashi. Workflow YAML i chování outputs zůstávají stejné.

## Proč to jde snadno

`WorkflowManager` (`common/workflow/manager.py`) drží celou orchestraci
(pořadí kroků, `if` podmínky, vars, outputs, adapter eventy) a s exekucí
komunikuje jen přes `KubectlJobRunner`:

- `ensure_namespace(namespace)`
- `has_active_jobs(namespace, task_label)`
- `apply_job(manifest)` + `wait_for_job(...)` → `succeeded|failed|timeout|aborted`
- `job_log_tail(namespace, name)`
- `delete_jobs_by_labels(namespace, labels)`

Jediné místo, kde manager „ví" o Kubernetes, je stavba manifestu
(`build_job_manifest`). Stačí tedy rozhraní posunout o úroveň výš — na
granularitu *kroku* — a Kubernetes detaily schovat do runneru.

## Návrh

### 1. Protokol `WorkflowStepRunner`

V `common/workflow/runtime.py` definovat protokol, který manager používá místo
přímé práce s manifesty:

```python
@dataclass
class StepResult:
    status: str          # "succeeded" | "failed" | "timeout" | "aborted"
    log_tail: str        # posledních ~50 řádek výstupu (pro failed event)


class WorkflowStepRunner(Protocol):
    def prepare(self, *, namespace: str, run_dir: Path) -> None: ...
    def has_active_run(self, namespace: str, task_label: str) -> bool: ...
    def run_step(
        self,
        workflow: WorkflowFile,
        step: WorkflowStep,
        *,
        namespace: str,
        name: str,
        labels: dict[str, str],
        env: dict[str, str],
        timeout: float,
        abort_event: threading.Event,
    ) -> StepResult: ...
    def abort(self, namespace: str, labels: dict[str, str]) -> str: ...
```

`KubectlJobRunner` protokol implementuje tím, co dělá dnes (postaví manifest,
`apply`, `wait_for_job`, při failu `job_log_tail`, abort = `delete_jobs_by_labels`).
Manager pak neimportuje `build_job_manifest` vůbec.

### 2. `LocalProcessRunner` (nový `common/workflow/local_runtime.py`)

Lokální implementace spouští krok jako subprocess:

- Příkaz: `/bin/bash -lc build_bash_wrapper(spec.envFiles, step.run)` —
  stávající wrapper (`set -euo pipefail`, sourcing envFiles, `cd "$WORKDIR"`)
  funguje lokálně beze změny; envFiles jako
  `/root/.config/agentis/agentis.env` jsou stejně hostPath soubory na hostu.
- Env: `{**spec.env, **runtime_env, **step.env}` přes `os.environ` jako základ
  (lokální proces potřebuje PATH atd. hosta).
- Cwd: `step.workingDir or spec.workingDir or WORKDIR`.
- Log: stdout+stderr do `run_dir/logs/<index>-<safe_step_name>.log`;
  `log_tail` čte posledních 50 řádek z tohoto souboru.
- Timeout a abort: `Popen` ve vlastní process group (`start_new_session=True`),
  poll smyčka hlídá `abort_event` a deadline; při timeoutu/abortu
  `os.killpg(..., SIGTERM)` a po grace period `SIGKILL`.
- `has_active_run`: in-memory registr běžících procesů per task label
  (lokální adapter je single-process, víc netřeba).
- `prepare`: jen `mkdir -p run_dir/logs` — žádný namespace.

### 3. Konfigurace

Dvě úrovně, YAML má přednost:

**a) Default adapteru** — env proměnná / `.env`:

```bash
# .env
WORKFLOW_EXECUTOR=local   # nebo "kubernetes" (default, zpětně kompatibilní)
```

→ `Settings.workflow_executor: str = "kubernetes"` v `common/config.py`.

**b) Per-workflow override** — volitelné pole ve workflow YAML:

```yaml
version: 1
workflow:
  executor: local        # volitelné; bez něj platí WORKFLOW_EXECUTOR
  workingDir: "[%WORKDIR%]"
  steps:
    - name: Run agent
      run: |
        agentiscode --adapter claude ... < "$AGENTIS_PROMPT_FILE"
```

→ `WorkflowSpec.executor: Literal["kubernetes", "local"] | None = None`.

`WorkflowManager.start_workflow()` po načtení YAML vybere runner:
`workflow.workflow.executor or settings.workflow_executor`. Runner se volí per
run (cache obou instancí v manageru), takže jeden adapter může souběžně
obsluhovat K8s i lokální workflow.

Aktivace workflow režimu jako takového zůstává beze změny přes
`context.adapter.runtime == "workflow"` — executor jen říká *kde* kroky poběží.

### 4. Mapování polí default.yaml na lokální běh

| Pole | kubernetes | local |
|---|---|---|
| `run`, `if`, `env`, `envFiles`, `outputs` | beze změny | beze změny |
| `workingDir` | container workingDir | cwd subprocesů |
| `timeoutSeconds` | `activeDeadlineSeconds` | deadline poll smyčky |
| `image`, `imagePullSecrets` | povinné / použité | ignorováno |
| `volumes`, `volumeMounts` | mounty | ignorováno (běží přímo na hostu) |
| `resources`, `ttlSecondsAfterFinished` | Job spec | ignorováno |

- `image` ve schématu povolit jako `str | None`; validátor vyžaduje image jen
  pro `executor == "kubernetes"` (resp. když se workflow reálně spouští v K8s,
  vyhodí `LocalProcessRunner`/manager srozumitelnou chybu naopak nikdy).
- Ignorovaná pole lokální runner jednou za run zaloguje na stderr
  (`[workflow] local executor ignoruje: volumes, volumeMounts, ...`), aby
  nepřekvapilo, že mounty „nefungují".
- `namespace` zůstává jen jako label/hodnota v eventech (`data.namespace`),
  reálně se nic nevytváří.

### 5. Co se nemění

- Celý `WorkflowManager`: prompt/context soubory v `run_dir`, attempt id,
  `var` outputs + `if` podmínky, aplikace outputs do Agentisu
  (`task.add_agent_comment`, `run.store_session_id`), adapter eventy
  (`workflow`, `workflow_step`, `idle`), project scope (`project.yaml`,
  `project_run_root`).
- JSON-RPC kontrakt (`start`/`add_message`/`abort`) a chování busy-checku
  (jen jeho implementace je per executor).

## Dotčené soubory

- `common/config.py` — `workflow_executor` setting (+ čtení `WORKFLOW_EXECUTOR`).
- `common/workflow/schema.py` — `executor` pole, `image` volitelné s validací.
- `common/workflow/runtime.py` — `StepResult`, protokol, `KubectlJobRunner.run_step`.
- `common/workflow/local_runtime.py` — nový `LocalProcessRunner`.
- `common/workflow/manager.py` — výběr runneru, volání `run_step` místo
  manifest/apply/wait/log_tail.
- `tests/test_workflow.py` — stávající fake runner přejde na nový protokol;
  nové testy pro `LocalProcessRunner` s reálnými `echo`/`exit 1` kroky
  (timeout, abort, log tail, ignorovaná pole).

## Rizika a vědomé kompromisy

- **Žádná izolace**: lokální kroky běží pod uživatelem adapteru přímo nad
  worktree — stejný trade-off jako `local` runtime, jen to explicitně
  zdokumentovat.
- **Souběh**: víc workflow runů nad stejným projektem si lokálně může šlapat
  po prostředí (porty, globální cache). Busy-check per task to řeší stejně
  jako dnes; mezi tasky to neřešíme (stejné jako u K8s s hostPath workspace).
- **Recovery po restartu adapteru**: lokální procesy umřou s adapterem
  (daemon thready). U K8s Joby přežijí. V1 plně v duchu stávajícího
  rozhodnutí „bez full recovery".

## Možná rozšíření (mimo v1)

- Per-run volba přes `context.adapter.executor` z Agentisu (přednost před
  YAML), kdyby měl jeden projekt jezdit oběma způsoby podle typu tasku.
- Třetí executor `docker`/`podman` — stejný protokol, `image` by se znovu
  využilo, mounty přes `-v`.
