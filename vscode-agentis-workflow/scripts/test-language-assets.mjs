import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import Ajv from "ajv";
import { parse } from "yaml";

const root = new URL("../", import.meta.url);
const readJson = async (path) =>
  JSON.parse(await readFile(new URL(path, root), "utf8"));

const [manifest, schema, grammar] = await Promise.all([
  readJson("package.json"),
  readJson("schemas/agentis-workflow.schema.json"),
  readJson("syntaxes/agentis-workflow.tmLanguage.json"),
]);

assert.equal(manifest.name, "agentis-workflow");
assert.equal(
  manifest.contributes.yamlValidation[0].fileMatch,
  "**/.agentis/workflows/*.yaml",
);
assert.deepEqual(grammar.patterns, [{ include: "#interpolation" }]);

const validate = new Ajv({ allErrors: true, strict: false }).compile(schema);
const validWorkflow = parse(`
version: 1
extends: _base
workflow:
  executor: local
  maxParallel: 2
  env:
    TASK_TITLE: "[%TASK_TITLE%]"
  steps:
    - name: Prepare
      needs: []
      run: echo ready
      outputs:
        - type: var
          name: READY
          valueFrom: outputs/ready
    - name: Run agent
      needs: [Prepare]
      uses: run-agent
      if: READY && ! AGENTIS_AUTO_MERGE
  followups:
    - title: Merge
      prompt: Merge the task branch.
      workflow: merge
`);
assert.equal(validate(validWorkflow), true, JSON.stringify(validate.errors));

const inheritanceBase = parse(`
version: 1
workflow:
  stepTemplates:
    run-agent:
      env:
        RUN_AGENT_FLAGS: --json
      run: agentiscode < "$AGENTIS_PROMPT_FILE"
`);
assert.equal(validate(inheritanceBase), true, JSON.stringify(validate.errors));

const invalidCondition = parse(`
version: 1
workflow:
  steps:
    - name: Run
      if: (READY)
      run: echo ok
`);
assert.equal(validate(invalidCondition), false);

const invalidOutput = parse(`
version: 1
workflow:
  steps:
    - name: Run
      run: echo ok
      outputs:
        - type: var
          name: not-valid
`);
assert.equal(validate(invalidOutput), false);

const optionalPrompt = parse(`
version: 1
workflow:
  steps:
    - name: Run
      run: echo ok
  followups:
    - title: Merge
      workflow: merge
`);
assert.equal(validate(optionalPrompt), true, JSON.stringify(validate.errors));

const agentStep = parse(`
version: 1
workflow:
  steps:
    - name: Select
      timeoutSeconds: 1200
      agent:
        adapter: claude
        model: \${NEWS_MODEL}
        prompt: |
          Vyber zprávy.
        inputs:
          - label: "Položky:"
            path: \${AGENTIS_RUN_DIR}/outputs/items.json
            optional: true
        outputSchema: \${WORKDIR}/.agentis/schemas/selection.schema.json
        schemaRetries: 1
        output: \${AGENTIS_RUN_DIR}/outputs/selection.json
    - name: Task prompt agent
      agent:
        adapter: opencode
`);
assert.equal(validate(agentStep), true, JSON.stringify(validate.errors));

const agentInvalid = (agentYaml, extra = "") =>
  parse(`
version: 1
workflow:
  steps:
    - name: Select
${extra}      agent:
${agentYaml}
`);
assert.equal(validate(agentInvalid("        model: x")), false, "agent requires adapter");
assert.equal(
  validate(agentInvalid("        adapter: claude\n        prompt: a\n        promptFile: b")),
  false,
  "prompt and promptFile are exclusive",
);
assert.equal(
  validate(agentInvalid("        adapter: claude\n        schemaRetries: 1")),
  false,
  "schemaRetries requires outputSchema",
);
assert.equal(
  validate(agentInvalid("        adapter: claude\n        unknown: 1")),
  false,
  "agent rejects unknown keys",
);
assert.equal(
  validate(agentInvalid("        adapter: claude", "      run: echo hi\n")),
  false,
  "step cannot declare both run and agent",
);
