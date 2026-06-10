## Cíl

Implementuj deklarativní Kubernetes workflow režim pro Agentis adapter.

Nový režim má přesunout projektově proměnlivou logiku z Python adapteru do `.agentis/workflows/ci.yaml`. Adapter zůstane orchestrator, ale jednotlivé kroky workflow bude spouštět jako samostatné Kubernetes `Job` objekty.

## Rozhodnutí

- Aktivace režimu je explicitně přes `context.adapter.runtime == "workflow"`.
- Workflow soubor pro v1 je pevně `.agentis/workflows/ci.yaml`.
- `runner.yaml` se ve v1 nepoužije.
- Workflow je sekvenční, bez DAG/paralelismu.
- Každý `step` je jeden Kubernetes `Job`.
- `start` a `add_message` spouští workflow na pozadí a rychle vrací odpověď.
- `session_id` se nevrací synchronně ze `start`; zapíše se později přes telemetry a workflow output.
- Workspace zůstává přes `hostPath /var/www`.
- V1 cílí na single-node Kubernetes.
- Secrets se berou z readonly hostPath shell env file, např. `/root/.config/agentis/agentis.env`.
- Prompt/context se materializují do souborů ve worktree, ne do argv/env.
- Workflow YAML se načte a zmrazí jednou na začátku workflow runu.
- Restart adapteru během workflow nemusí ve v1 umět plnou recovery.
- Kubernetes API používej přes `kubectl` subprocess, ne přes Python Kubernetes client.
- `expected_artifacts` v contextu workflow režim neřeší; artifacts deklaruje `ci.yaml outputs`.

## Non-Goals

- Neimplementovat Kubernetes controller/runner v clusteru.
- Neimplementovat DAG, matrix, parallel steps ani `needs`.
- Neimplementovat PVC workspace.
- Neimplementovat full recovery po restartu adapteru.
- Neimplementovat raw Kubernetes manifest per step.
- Neřešit `question`/`approve` přes adapter IPC do Jobu.

## Workflow Schema

Přidej runtime dependency `PyYAML`.

Validaci dělej přes Pydantic modely.

Podporuj přibližně toto schema:

```yaml
version: 1
workflow:
  image: rg.nl-ams.scw.cloud/reactis/opencode:1.2
  workingDir: "[%WORKDIR%]"
  timeoutSeconds: 14400
  ttlSecondsAfterFinished: 3600
  envFiles:
    - /root/.config/agentis/agentis.env
  env:
    HOME: /root
    MAIN_DIR: "[%MAIN_DIR%]"
  volumeMounts:
    - name: www
      mountPath: /var/www
  steps:
    - name: Run agent
      run: |
        mkdir -p .agentis/outputs
        agentiscode --adapter opencode \
          --model "$AGENTIS_MODEL" \
          --agent "$AGENTIS_AGENT" \
          --run-id "$AGENTIS_RUN_ID" \
          --task-id "$AGENTIS_TASK_ID" \
          --agentis-api "$AGENTIS_ENDPOINT" \
          --final-output .agentis/outputs/final-comment.md \
          --session-output .agentis/outputs/session-id \
          < "$AGENTIS_PROMPT_FILE"
      outputs:
        - type: agent_comment
          bodyFrom: .agentis/outputs/final-comment.md
          status: 4
        - type: session_id
          valueFrom: .agentis/outputs/session-id
volumes:
  - name: www
    hostPath:
      path: /var/www
```

Step-level override povol jen pro:

- `image`
- `env`
- `workingDir`
- `timeoutSeconds`
- `ttlSecondsAfterFinished`
- `resources`
- `run`
- `outputs`

Workflow-level podporuj:

- `image`
- `workingDir`
- `timeoutSeconds`
- `ttlSecondsAfterFinished`
- `env`
- `envFiles`
- `volumes`
- `volumeMounts`
- `imagePullSecrets`
- `steps`

## Interpolace

Podporuj tokeny `[%NAME%]` ve string hodnotách YAML.

Minimální allowlist:

- `NAMESPACE`
- `WORKDIR`
- `MAIN_DIR`
- `RUN_ID`
- `TASK_ID`
- `TASK_NUMBER`
- `TASK_TITLE`
- `BRANCH`
- `BASE_BRANCH`
- `GITHUB_REPO`

Současně injektuj tyto hodnoty jako env proměnné do každého Jobu.

Přidej runtime env:

- `AGENTIS_RUN_ID`
- `AGENTIS_TASK_ID`
- `AGENTIS_PROMPT_FILE`
- `AGENTIS_CONTEXT_FILE`
- `AGENTIS_SESSION_ID`, pokud existuje
- `AGENTIS_MODEL`, pokud existuje
- `AGENTIS_AGENT`, pokud existuje
- `AGENTIS_EFFORT`, pokud existuje

## Shell Semantics

Každý `run` krok spusť přes `/bin/bash -lc`.

Wrapper musí udělat:

```bash
set -euo pipefail
set -a
. /root/.config/agentis/agentis.env
set +a
cd "$WORKDIR"
<user script>
```

`envFiles` source-uj před spuštěním user scriptu.

## Kubernetes Job Runtime

Generuj `batch/v1 Job`.

Každý Job musí mít labels:

- `agentis.workflow=true`
- `agentis.task_id=<task_id>`
- `agentis.run_id=<run_id>`
- `agentis.attempt=<attempt_id>`
- `agentis.step_index=<index>`
- `agentis.step=<safe_step_name>`

Job naming:

- `wf-<short-run-id>-<attempt>-<step-index>-<safe-step-name>`

Použij:

- `activeDeadlineSeconds` z timeoutu
- `ttlSecondsAfterFinished` z workflow/step configu
- `restartPolicy: Never`
- `backoffLimit: 0`

Sleduj Job přes `kubectl`.

Po dokončení kroku:

- success pokud Job completed
- failed pokud Job failed, timeout nebo non-zero exit
- při failed přilož tail logu do `run.adapter_event`

## Background Workflow Manager

Implementuj background orchestration podobně jako současný `BaseSessionManager`.

`start` flow:

- vytvoř worktree přes existující git lifecycle
- materializuj prompt do `.agentis/runs/<attempt>/prompt.md`
- materializuj context do `.agentis/runs/<attempt>/context.json`
- načti a validuj `.agentis/workflows/ci.yaml`
- odmítni start, pokud už běží Job pro stejný task namespace
- spusť background thread
- vrať rychlou JSON-RPC odpověď s workflow metadata

`add_message` flow:

- použij stejný workflow
- prompt je `params.message`
- pokud context obsahuje session id, předej ho jako `AGENTIS_SESSION_ID`
- další chování stejné jako `start`

`abort` flow:

- ve workflow režimu nevyžaduj `session_id`
- najdi aktivní Joby podle labels `agentis.task_id` a `agentis.run_id`
- smaž je přes `kubectl delete job`
- reportuj abort event

`question` flow:

- workflow režim nepodporuje adapter IPC
- vrať unsupported chybu nebo zachovej no-op podle současného kontraktu

## Outputs

Přejmenuj staré step `attachments` na `outputs`.

Outputs aplikuj až po úspěšném dokončení celého workflow.

Podporuj minimálně:

```yaml
- type: agent_comment
  bodyFrom: .agentis/outputs/final-comment.md
  status: 4
```

```yaml
- type: session_id
  valueFrom: .agentis/outputs/session-id
```

```yaml
- type: url
  label: Pull Request
  valueFrom: .agentis/outputs/pull-request-url
```

```yaml
- type: artifact
  path: dist/report.json
  name: report
```

Finalizace:

- adapter zavolá `task.add_agent_comment`
- `body` vezme z `agent_comment.bodyFrom`
- `status` vezme z `agent_comment.status`
- `actions` použij stejné completion actions jako současný CLI flow
- `artifacts` pošli přes `task.add_agent_comment(..., artifacts=[...])`
- `url/text` outputs přidej jako comment attachments nebo adapter event data podle existujícího kontraktu

## agentiscode CLI

Přidej flagy:

- `--final-output PATH`
- `--session-output PATH`

`--final-output` uloží finální assistant text.

`--session-output` uloží agent session id, jakmile je známé.

Zachovej stávající telemetry chování.

## Úpravy Souborů

Očekávané oblasti změn:

- `pyproject.toml`
- `common/models.py`
- `common/rpc/jsonrpc.py`
- `common/kubernetes/runtime.py` nebo nový workflow runtime modul
- `common/session_manager.py` jen pokud bude potřeba sdílet helpery
- `app/agentiscode.py`
- `.agentis/workflows/ci.yaml`
- testy v `tests/`

Preferuj nové malé moduly pod `common/workflow/` nebo `common/kubernetes/workflow.py`.

## Testy

Přidej testy pro:

- PyYAML parsing a Pydantic validaci workflow schema
- token interpolation
- Job manifest generation
- bash wrapper s `set -euo pipefail` a envFiles
- `runtime=workflow` start spouští background workflow a neblokuje
- busy task odmítne druhý workflow
- abort maže Joby podle labels bez `session_id`
- failed step reportuje log tail
- outputs se aplikují až po success celého workflow
- `agentiscode --final-output`
- `agentiscode --session-output`

Spusť:

```bash
poetry run pytest -q
poetry run ruff check .
```

## Akceptační Kritéria

- `context.adapter.runtime="workflow"` spustí `.agentis/workflows/ci.yaml` jako sekvenci Kubernetes Jobů.
- Jeden step odpovídá jednomu Jobu.
- `start` a `add_message` vrací rychle a workflow běží na pozadí.
- Běžící workflow pro stejný task blokuje další workflow.
- Selhání kroku zastaví workflow a pošle failed adapter event s log tail.
- Úspěšný workflow aplikuje `outputs` do Agentisu.
- `abort` funguje i bez `session_id`.
- Prompt ani Agentis token nejsou v argv ani v Kubernetes manifestu.
- `runner.yaml` není vyžadovaný pro v1.