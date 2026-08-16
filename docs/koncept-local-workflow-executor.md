# Lokální workflow executor

> **Stav: implementováno.** Zdrojáky jsou v
> `common/workflow/local_runtime.py`, společný protokol a Kubernetes executor v
> `common/workflow/runtime.py`. Uživatelský popis workflow je v
> [docs/workflow.md](workflow.md).

## Účel

Workflow runtime umí stejné YAML kroky spouštět dvěma způsoby:

- `kubernetes` vytvoří pro každý krok Kubernetes Job přes `kubectl`,
- `local` spustí krok jako lokální bash proces přímo na hostu adapteru.

Orchestrace je pro oba executory společná. `WorkflowManager` řeší DAG kroků,
`needs`, `if`, paralelismus, retry, fail-fast a `always` kroky, outputs i eventy
do Agentisu. Executor řeší pouze fyzické spuštění, čekání, log a ukončení
jednoho kroku.

Workflow runtime se používá pro každý `start` a `add_message`. `local` není
samostatný CLI runtime a neurčuje, zda se workflow použije; určuje pouze, kde
jeho kroky poběží.

## Výběr executoru

Priorita je:

1. `context.adapter.runtime == "local"` vždy vynutí lokální executor.
2. Jinak se použije `workflow.executor` z vybraného YAML souboru.
3. Když YAML executor neurčí, použije se `WORKFLOW_EXECUTOR` adapteru.
4. Výchozí hodnota `WORKFLOW_EXECUTOR` je `kubernetes`.

Hodnota `context.adapter.runtime == "workflow"` samostatný executor nevybírá;
ponechá rozhodnutí na YAML a konfiguraci adapteru.

```yaml
version: 1
workflow:
  executor: local
  workingDir: "[%WORKDIR%]"
  timeoutSeconds: 600
  steps:
    - name: Run agent
      run: agentiscode < "$AGENTIS_PROMPT_FILE"
```

Globální default lze nastavit v prostředí adapteru:

```bash
WORKFLOW_EXECUTOR=local
```

Schema přijímá pouze `kubernetes` a `local`. Kubernetes executor vyžaduje
`image` na workflow nebo na každém kroku; lokální executor image nevyžaduje.
Runner se vybírá pro každý run a instance se cachují podle executoru a u
Kubernetes také podle `workflow.context`.

## Společné rozhraní

`WorkflowManager` komunikuje s executory přes protokol `WorkflowStepRunner`:

```python
class WorkflowStepRunner(Protocol):
    def prepare(
        self, workflow: WorkflowFile, *, namespace: str, run_dir: Path
    ) -> None: ...

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
        run_dir: Path,
    ) -> StepResult: ...

    def abort(self, namespace: str, labels: dict[str, str]) -> str: ...
    def delete_namespace(self, namespace: str) -> None: ...
```

`StepResult.status` je `succeeded`, `failed`, `timeout` nebo `aborted`.
`log_tail` obsahuje posledních 50 řádků výstupu jen při chybě nebo timeoutu.

`KubectlJobRunner` uvnitř `run_step()` sestaví Job manifest, provede `apply`,
čeká na stav Jobu a při neúspěchu načte log Podu. `LocalProcessRunner` používá
stejný kontrakt nad procesy hostitele. Manager proto neřeší detaily konkrétního
prostředí.

## Lokální proces

### Příkaz a pracovní adresář

Runner hledá `bash` přes `PATH`; na POSIX má fallback `/bin/bash`. Na Windows je
nutný bash dostupný přes `PATH`, například Git Bash nebo WSL. Chybějící bash či
chyba při vytvoření procesu vrátí neúspěšný `StepResult` a objeví se ve stderr i
v `workflow_step` eventu.

Krok se spouští jako:

```text
bash -lc <wrapper>
```

Wrapper je společný s Kubernetes executorem:

1. zapne `set -euo pipefail`,
2. se `set -a` načte všechny `workflow.envFiles`,
3. přejde do `step.workingDir`, jinak `workflow.workingDir`, jinak `$WORKDIR`,
4. spustí `step.run`.

Stejný pracovní adresář runner používá také jako `cwd` procesu. Bez explicitního
`workingDir` použije `WORKDIR`, případně jako poslední fallback `run_dir`.

### Prostředí

Prostředí procesu se skládá v tomto pořadí, pozdější hodnota vyhrává:

1. prostředí procesu adapteru,
2. `workflow.env`,
3. runtime env vytvořené `WorkflowManagerem`,
4. `step.env`.

Z hostitelského prostředí se před merge odstraní `AGENTIS_TOKEN`,
`AGENTIS_API_TOKEN` a `AGENTIS_SERVICE_TOKEN`. Tím tokeny adapteru neprosáknou
do lokálních kroků automaticky. YAML nebo soubor uvedený v `envFiles` však může
proměnné explicitně znovu definovat, proto musí být workflow důvěryhodné.

### Logy

Stdout a stderr kroku se zapisují společně do:

```text
<run_dir>/logs/<job_name>.log
```

`job_name` generuje manager z runu, attemptu, indexu a bezpečného názvu kroku.
Retry používá odlišné jméno, takže každý pokus má vlastní log. Při `failed` nebo
`timeout` runner pošle posledních 50 řádků do eventu a vypíše chybu také na
stderr adapteru. Úspěšný ani abortovaný krok `log_tail` neposílá.

### Timeout a abort

Každý proces běží ve vlastní process group. Poll smyčka sleduje dokončení,
`timeoutSeconds` a sdílený `abort_event`.

- Na POSIX runner pošle celé process group `SIGTERM`, počká 5 sekund a případně
  použije `SIGKILL`.
- Na Windows spustí `taskkill /F /T` a jako fallback ukončí hlavní proces.
- `abort()` ukončí všechny procesy registrované pod task labelem.

Registr procesů je pouze in-memory. Používá se pro busy-check a abort v rámci
jednoho běžícího adapter procesu; není to globální lock mezi více instancemi.

## Chování YAML polí

| Pole | Kubernetes | Local |
| --- | --- | --- |
| `run`, `if`, `needs`, `always`, `continueOnError`, `retries` | společná orchestrace | společná orchestrace |
| `env`, `envFiles`, `workingDir`, `timeoutSeconds`, `outputs` | použito | použito |
| `maxParallel` | použito managerem | použito managerem |
| `image`, `steps[].image` | použito a vyžadováno | ignorováno, varování |
| `context` | kubectl context | ignorováno, varování |
| `imagePullSecrets`, `mounts`, `steps[].resources` | promítnuto do Jobu | ignorováno, varování |
| `ttlSecondsAfterFinished`, `steps[].ttlSecondsAfterFinished` | TTL Jobu | ignorováno bez varování |
| `deleteNamespace` | po úspěchu může smazat namespace | ignorováno bez varování |

`LocalProcessRunner.prepare()` vytvoří adresář `logs` a jednou za run vypíše
varování pro nastavená Kubernetes pole, která kontroluje. Namespace se i u
lokálního runu počítá kvůli jednotným eventům a labelům, ale žádný Kubernetes
namespace se nevytváří; `delete_namespace()` je no-op.

## Souběh a bezpečnost

- Lokální kroky běží bez kontejnerové izolace a se stejnými oprávněními jako
  proces adapteru. Mohou měnit hostitele i soubory mimo worktree.
- Per task může běžet nejvýše jedno workflow. Různé tasky ale mohou běžet
  souběžně a kolidovat přes porty, globální cache nebo sdílené adresáře.
- `maxParallel` omezuje počet současných kroků jednoho runu, ne počet procesů
  napříč runy.
- Po restartu adapteru se in-memory registry neobnoví. Náhle ukončené potomky
  nelze po startu znovu dohledat ani spravovat a podle způsobu ukončení a
  platformy mohou zůstat běžet. Kubernetes Joby naproti tomu existují nezávisle
  na procesu adapteru.
- `envFiles` a workflow skripty jsou spouštěný kód. Lokální executor je vhodný
  pouze pro důvěryhodné workflow.

## Ověření

Testy v `tests/test_workflow.py` pokrývají:

- výběr local executoru z YAML, `WORKFLOW_EXECUTOR` i vynucení přes runtime,
- běh skutečného bash procesu a aplikaci outputs,
- odstranění Agentis tokenů z hostitelského prostředí,
- chybějící bash a chybu spuštění,
- selhání kroku a přenos konce logu,
- timeout a abort celého stromu procesů,
- požadavek na image pouze pro Kubernetes executor,
- odmítnutí neznámého executoru schématem.

Relevantní ověření lze spustit příkazem:

```bash
poetry run pytest -q tests/test_workflow.py -k "local_executor or runtime_local or kubernetes_executor_requires_image or unknown_executor"
```

## Možná rozšíření

- Izolovaný executor přes Docker nebo Podman se stejným protokolem.
- Per-executor limity souběhu napříč workflow runy.
- Perzistentní evidence lokálních procesů a recovery nebo úklid po restartu.
