import * as vscode from "vscode";
import { isMap, isScalar, isSeq, parseDocument } from "yaml";

const SELECTOR: vscode.DocumentSelector = {
  language: "yaml",
  pattern: "**/.agentis/workflows/*.yaml",
};

const TOKENS = [
  "NAMESPACE",
  "WORKDIR",
  "RUN_DIR",
  "MAIN_DIR",
  "RUN_ID",
  "TASK_ID",
  "TASK_NUMBER",
  "TASK_TITLE",
  "BRANCH",
  "BASE_BRANCH",
  "GITHUB_REPO",
] as const;

const CONDITION_VARIABLES = [
  ...TOKENS,
  "AGENTIS_RUN_ID",
  "AGENTIS_TASK_ID",
  "AGENTIS_PROJECT_ID",
  "AGENTIS_RUN_DIR",
  "AGENTIS_PROMPT_FILE",
  "AGENTIS_CONTEXT_FILE",
  "AGENTIS_ENDPOINT",
  "AGENTIS_SERVICE_TOKEN",
  "AGENTIS_SESSION_ID",
  "AGENTIS_MODEL",
  "AGENTIS_AGENT",
  "AGENTIS_EFFORT",
  "AGENTIS_AUTO_MERGE",
] as const;

const MOUNT_FIELDS = new Set([
  "name",
  "mountPath",
  "readOnly",
  "subPath",
  "subPathExpr",
  "mountPropagation",
]);

type RecordValue = Record<string, unknown>;

interface WorkflowModel {
  data: RecordValue;
  templates: Set<string>;
  variables: Set<string>;
}

const diagnosticGenerations = new Map<string, number>();

export function activate(context: vscode.ExtensionContext): void {
  const diagnostics =
    vscode.languages.createDiagnosticCollection("agentis-workflow");
  const stepNameDecoration = vscode.window.createTextEditorDecorationType({
    fontWeight: "bold",
    // VS Code exposes margin for decoration attachments, not enclosed text.
    before: {
      contentText: "\u200b",
      margin: "20px 0 0 0",
    },
  });
  const runBlockDecoration = vscode.window.createTextEditorDecorationType({
    isWholeLine: true,
    backgroundColor: "rgba(128, 128, 128, 0.12)",
  });
  const refreshOpenWorkflows = () => {
    for (const document of vscode.workspace.textDocuments) {
      if (isWorkflowUri(document.uri)) {
        void updateDiagnostics(document, diagnostics);
      }
    }
    for (const editor of vscode.window.visibleTextEditors) {
      const workflow = isWorkflowUri(editor.document.uri);
      editor.setDecorations(
        stepNameDecoration,
        workflow ? stepNameRanges(editor.document) : [],
      );
      editor.setDecorations(
        runBlockDecoration,
        workflow ? runRanges(editor.document) : [],
      );
    }
  };
  const watcher = vscode.workspace.createFileSystemWatcher(
    "**/.agentis/workflows/*.yaml",
  );

  context.subscriptions.push(
    vscode.languages.registerCompletionItemProvider(
      SELECTOR,
      new WorkflowCompletionProvider(),
      "%",
      "[",
      ",",
    ),
    vscode.languages.registerDefinitionProvider(
      SELECTOR,
      new WorkflowDefinitionProvider(),
    ),
    diagnostics,
    stepNameDecoration,
    runBlockDecoration,
    watcher,
    vscode.workspace.onDidOpenTextDocument((document) => {
      if (isWorkflowUri(document.uri)) refreshOpenWorkflows();
    }),
    vscode.workspace.onDidChangeTextDocument((event) => {
      if (isWorkflowUri(event.document.uri)) refreshOpenWorkflows();
    }),
    vscode.workspace.onDidCloseTextDocument((document) => {
      diagnostics.delete(document.uri);
      const key = document.uri.toString();
      diagnosticGenerations.set(key, (diagnosticGenerations.get(key) ?? 0) + 1);
      if (isWorkflowUri(document.uri)) refreshOpenWorkflows();
    }),
    vscode.window.onDidChangeActiveTextEditor(refreshOpenWorkflows),
    vscode.window.onDidChangeVisibleTextEditors(refreshOpenWorkflows),
    watcher.onDidCreate(refreshOpenWorkflows),
    watcher.onDidChange(refreshOpenWorkflows),
    watcher.onDidDelete(refreshOpenWorkflows),
  );
  refreshOpenWorkflows();
}

function stepNameRanges(document: vscode.TextDocument): vscode.Range[] {
  let parsed: ReturnType<typeof parseDocument>;
  try {
    parsed = parseDocument(document.getText());
  } catch {
    return [];
  }
  if (!isMap(parsed.contents)) return [];

  const workflow = parsed.contents.get("workflow", true);
  if (!isMap(workflow)) return [];
  const steps = workflow.get("steps", true);
  if (!isSeq(steps)) return [];

  return steps.items.flatMap((step) => {
    if (!isMap(step)) return [];
    const name = step.get("name", true);
    if (!isScalar(name) || typeof name.value !== "string" || !name.range) {
      return [];
    }
    return [
      new vscode.Range(
        document.positionAt(name.range[0]),
        document.positionAt(name.range[1]),
      ),
    ];
  });
}

function runRanges(document: vscode.TextDocument): vscode.Range[] {
  let parsed: ReturnType<typeof parseDocument>;
  try {
    parsed = parseDocument(document.getText());
  } catch {
    return [];
  }
  if (!isMap(parsed.contents)) return [];

  const workflow = parsed.contents.get("workflow", true);
  if (!isMap(workflow)) return [];

  return [
    ...runRangesInSequence(document, workflow.get("steps", true)),
    ...runRangesInMap(document, workflow.get("stepTemplates", true)),
  ];
}

function runRangesInSequence(
  document: vscode.TextDocument,
  value: unknown,
): vscode.Range[] {
  if (!isSeq(value)) return [];
  return value.items.flatMap((item) =>
    isMap(item) ? runRange(document, item) : [],
  );
}

function runRangesInMap(
  document: vscode.TextDocument,
  value: unknown,
): vscode.Range[] {
  if (!isMap(value)) return [];
  return value.items.flatMap((item) =>
    isMap(item.value) ? runRange(document, item.value) : [],
  );
}

function runRange(
  document: vscode.TextDocument,
  step: unknown,
): vscode.Range[] {
  if (!isMap(step)) return [];
  const pair = step.items.find(
    (item) => isScalar(item.key) && item.key.value === "run",
  );
  if (!pair || !isScalar(pair.key) || !isScalar(pair.value)) return [];
  if (!pair.key.range || !pair.value.range) return [];

  const startLine = document.positionAt(pair.key.range[0]).line;
  const valueEnd = pair.value.range[2];
  const endLine = document.positionAt(
    Math.max(pair.value.range[0], valueEnd - 1),
  ).line;
  return [
    new vscode.Range(
      startLine,
      0,
      endLine,
      document.lineAt(endLine).text.length,
    ),
  ];
}

export function deactivate(): void {}

class WorkflowCompletionProvider implements vscode.CompletionItemProvider<vscode.CompletionItem> {
  async provideCompletionItems(
    document: vscode.TextDocument,
    position: vscode.Position,
  ): Promise<vscode.CompletionItem[]> {
    if (!isWorkflowUri(document.uri)) return [];
    const linePrefix = document
      .lineAt(position.line)
      .text.slice(0, position.character);
    const property = linePrefix.match(
      /^\s*(?:-\s*)?([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$/,
    );

    const needsMode = needsCompletionMode(document, position.line, property);
    if (needsMode) {
      return needsCompletionItems(
        previousStepNames(document, position.line),
        needsReplacementRange(document, position, linePrefix),
        needsMode,
      );
    }

    const tokenFragment = linePrefix.match(/(?:\[%[A-Z_]*|\[)$/)?.[0];
    if (tokenFragment) {
      const range = new vscode.Range(
        position.line,
        position.character - tokenFragment.length,
        position.line,
        position.character,
      );
      return TOKENS.map((token) => {
        const item = new vscode.CompletionItem(
          `[%${token}%]`,
          vscode.CompletionItemKind.Variable,
        );
        item.detail = "Agentis interpolation token";
        item.insertText = `[%${token}%]`;
        item.range = range;
        return item;
      });
    }

    if (!property) return [];
    const key = property[1];
    const range = scalarRange(position, linePrefix);
    if (key === "extends") {
      return workflowFileItems(document.uri, range, true);
    }
    if (
      key === "workflow" &&
      insideSection(document, position.line, "followups")
    ) {
      return workflowFileItems(document.uri, range, false);
    }

    const model = await loadModel(document);
    if (key === "uses") {
      return completionItems(
        model.templates,
        range,
        vscode.CompletionItemKind.Reference,
        "Workflow step template",
      );
    }
    if (key === "if") {
      return completionItems(
        visibleConditionVariables(model, document, position.line),
        range,
        vscode.CompletionItemKind.Variable,
        "Workflow condition variable",
      );
    }
    return [];
  }
}

class WorkflowDefinitionProvider implements vscode.DefinitionProvider {
  async provideDefinition(
    document: vscode.TextDocument,
    position: vscode.Position,
  ): Promise<vscode.Definition | undefined> {
    if (!isWorkflowUri(document.uri)) return undefined;

    const parsed = parseDocument(document.getText());
    if (parsed.errors.length > 0 || !isMap(parsed.contents)) return undefined;

    const offset = document.offsetAt(position);
    const root = parsed.contents;
    const extendsPair = mapPair(root, "extends");
    const extendsName = stringValueAt(document, extendsPair?.value, offset);
    if (extendsName) return workflowFileDefinition(document, extendsName);

    const workflow = mapPair(root, "workflow")?.value;
    if (!isMap(workflow)) return undefined;

    const followups = mapPair(workflow, "followups")?.value;
    if (isSeq(followups)) {
      for (const followup of followups.items) {
        const workflowPair = mapPair(followup, "workflow");
        const workflowName = stringValueAt(
          document,
          workflowPair?.value,
          offset,
        );
        if (workflowName) return workflowFileDefinition(document, workflowName);
      }
    }

    const parent = await loadParentSource(document, root);
    const sources = parent
      ? [{ document, root }, parent]
      : [{ document, root }];

    for (const step of workflowSteps(workflow)) {
      const usesPair = mapPair(step, "uses");
      const templateName = stringValueAt(document, usesPair?.value, offset);
      if (templateName) {
        for (const source of sources) {
          const target = templateDefinition(source, templateName);
          if (target) return target;
        }
      }

      const needsPair = mapPair(step, "needs");
      if (!isSeq(needsPair?.value)) continue;
      for (const dependency of needsPair.value.items) {
        const dependencyName = stringValueAt(document, dependency, offset);
        if (!dependencyName) continue;
        for (const source of sources) {
          const target = stepDefinition(source, dependencyName);
          if (target) return target;
        }
      }
    }

    const condition = conditionAtOffset(document, workflow, offset);
    if (condition) {
      const variable = identifierAtOffset(document.getText(), offset);
      if (variable) {
        for (const source of sources) {
          const target = variableDefinition(source, condition, variable);
          if (target) return target;
        }
      }
    }

    return undefined;
  }
}

interface WorkflowSource {
  document: vscode.TextDocument;
  root: unknown;
}

function mapPair(
  map: unknown,
  name: string,
): { key: unknown; value: unknown } | undefined {
  if (!isMap(map)) return undefined;
  return map.items.find((item) => scalarString(item.key) === name);
}

function scalarString(value: unknown): string | undefined {
  return isScalar(value) && typeof value.value === "string"
    ? value.value
    : undefined;
}

function nodeRange(
  document: vscode.TextDocument,
  node: unknown,
): vscode.Range | undefined {
  if (!node || typeof node !== "object") return undefined;
  const range = (node as { range?: readonly number[] }).range;
  if (
    !range ||
    range.length < 2 ||
    typeof range[0] !== "number" ||
    typeof range[1] !== "number"
  ) {
    return undefined;
  }
  return new vscode.Range(
    document.positionAt(range[0]),
    document.positionAt(range[1]),
  );
}

function rangeContains(
  document: vscode.TextDocument,
  node: unknown,
  offset: number,
): boolean {
  const range = nodeRange(document, node);
  if (!range) return false;
  const start = document.offsetAt(range.start);
  const end = document.offsetAt(range.end);
  return offset >= start && offset <= end;
}

function stringValueAt(
  document: vscode.TextDocument,
  node: unknown,
  offset: number,
): string | undefined {
  const value = scalarString(node);
  return value !== undefined && rangeContains(document, node, offset)
    ? value
    : undefined;
}

function workflowSteps(workflow: unknown): unknown[] {
  const steps = mapPair(workflow, "steps")?.value;
  return isSeq(steps) ? steps.items : [];
}

function locationAtNode(
  document: vscode.TextDocument,
  node: unknown,
): vscode.Location | undefined {
  const range = nodeRange(document, node);
  return range ? new vscode.Location(document.uri, range) : undefined;
}

function templateDefinition(
  source: WorkflowSource,
  templateName: string,
): vscode.Location | undefined {
  const workflow = mapPair(source.root, "workflow")?.value;
  const templates = mapPair(workflow, "stepTemplates")?.value;
  if (!isMap(templates)) return undefined;
  const pair = mapPair(templates, templateName);
  return pair ? locationAtNode(source.document, pair.key) : undefined;
}

function stepDefinition(
  source: WorkflowSource,
  stepName: string,
): vscode.Location | undefined {
  const workflow = mapPair(source.root, "workflow")?.value;
  for (const step of workflowSteps(workflow)) {
    const namePair = mapPair(step, "name");
    if (scalarString(namePair?.value) === stepName) {
      return namePair
        ? locationAtNode(source.document, namePair.value)
        : undefined;
    }
  }
  return undefined;
}

async function workflowFileDefinition(
  document: vscode.TextDocument,
  workflowName: string,
): Promise<vscode.Location | undefined> {
  if (!/^[A-Za-z0-9_.-]+$/.test(workflowName)) return undefined;
  const uri = vscode.Uri.joinPath(document.uri, "..", `${workflowName}.yaml`);
  try {
    const stat = await vscode.workspace.fs.stat(uri);
    if (stat.type !== vscode.FileType.File) return undefined;
  } catch {
    return undefined;
  }
  return new vscode.Location(uri, new vscode.Range(0, 0, 0, 0));
}

async function loadParentSource(
  document: vscode.TextDocument,
  root: unknown,
): Promise<WorkflowSource | undefined> {
  const extendsName = scalarString(mapPair(root, "extends")?.value);
  if (!extendsName || !/^[A-Za-z0-9_.-]+$/.test(extendsName)) return undefined;
  try {
    const parentUri = vscode.Uri.joinPath(
      document.uri,
      "..",
      `${extendsName}.yaml`,
    );
    const parentDocument = await vscode.workspace.openTextDocument(parentUri);
    const parsed = parseDocument(parentDocument.getText());
    if (parsed.errors.length > 0 || !isMap(parsed.contents)) return undefined;
    return { document: parentDocument, root: parsed.contents };
  } catch {
    return undefined;
  }
}

function conditionAtOffset(
  document: vscode.TextDocument,
  workflow: unknown,
  offset: number,
): unknown | undefined {
  const containers = [
    ...workflowSteps(workflow),
    ...sequenceItems(mapPair(workflow, "followups")?.value),
  ];
  return containers.find((container) => {
    const condition = mapPair(container, "if")?.value;
    return stringValueAt(document, condition, offset) !== undefined;
  });
}

function identifierAtOffset(text: string, offset: number): string | undefined {
  const isIdentifierCharacter = (character: string | undefined): boolean =>
    character !== undefined && /[A-Za-z0-9_]/.test(character);
  let cursor = offset;
  if (!isIdentifierCharacter(text[cursor]) && cursor > 0) cursor -= 1;
  if (!isIdentifierCharacter(text[cursor])) return undefined;

  let start = cursor;
  while (start > 0 && isIdentifierCharacter(text[start - 1])) start -= 1;
  let end = cursor + 1;
  while (end < text.length && isIdentifierCharacter(text[end])) end += 1;
  const value = text.slice(start, end);
  return /^[A-Za-z_][A-Za-z0-9_]*$/.test(value) ? value : undefined;
}

function variableDefinition(
  source: WorkflowSource,
  container: unknown,
  variable: string,
): vscode.Location | undefined {
  const workflow = mapPair(source.root, "workflow")?.value;
  const candidates = [workflow, container];
  const uses = scalarString(mapPair(container, "uses")?.value);
  if (uses) {
    const templates = mapPair(workflow, "stepTemplates")?.value;
    candidates.push(mapPair(templates, uses)?.value);
  }

  for (const candidate of candidates) {
    const env = mapPair(candidate, "env")?.value;
    const envPair = mapPair(env, variable);
    if (envPair) return locationAtNode(source.document, envPair.key);
  }

  const outputContainers = [
    ...workflowSteps(workflow),
    ...mapValues(mapPair(workflow, "stepTemplates")?.value),
  ];
  for (const outputContainer of outputContainers) {
    const outputs = mapPair(outputContainer, "outputs")?.value;
    if (!isSeq(outputs)) continue;
    for (const output of outputs.items) {
      if (scalarString(mapPair(output, "type")?.value) !== "var") continue;
      const namePair = mapPair(output, "name");
      if (scalarString(namePair?.value) === variable) {
        return namePair
          ? locationAtNode(source.document, namePair.value)
          : undefined;
      }
    }
  }
  return undefined;
}

function sequenceItems(value: unknown): unknown[] {
  return isSeq(value) ? value.items : [];
}

function mapValues(value: unknown): unknown[] {
  return isMap(value) ? value.items.map((item) => item.value) : [];
}

type NeedsCompletionMode = "block" | "empty" | "flow" | "flow-open";

function needsCompletionMode(
  document: vscode.TextDocument,
  currentLine: number,
  property: RegExpMatchArray | null,
): NeedsCompletionMode | undefined {
  if (property?.[1] === "needs") {
    const value = property[2];
    if (!value.includes("[")) return "empty";
    if (
      !value.includes(",") &&
      !document.lineAt(currentLine).text.includes("]")
    ) {
      return "flow-open";
    }
    return "flow";
  }

  const text = document.lineAt(currentLine).text;
  if (!/^\s*-\s*/.test(text)) return undefined;
  const indent = text.length - text.trimStart().length;
  for (let line = currentLine - 1; line >= 0; line -= 1) {
    const candidate = document.lineAt(line).text;
    if (!candidate.trim() || candidate.trimStart().startsWith("#")) continue;
    const candidateIndent = candidate.length - candidate.trimStart().length;
    if (candidateIndent >= indent) continue;
    return /^\s*needs:\s*(?:#.*)?$/.test(candidate) ? "block" : undefined;
  }
  return undefined;
}

function needsCompletionItems(
  values: Iterable<string>,
  range: vscode.Range,
  mode: NeedsCompletionMode,
): vscode.CompletionItem[] {
  return [...values]
    .sort((left, right) => left.localeCompare(right))
    .map((value) => {
      const item = new vscode.CompletionItem(
        value,
        vscode.CompletionItemKind.Reference,
      );
      item.detail = "Earlier workflow step";
      const quoted = JSON.stringify(value);
      item.insertText =
        mode === "empty"
          ? `[${quoted}]`
          : mode === "flow-open"
            ? `${quoted}]`
            : quoted;
      item.range = range;
      return item;
    });
}

async function workflowFileItems(
  uri: vscode.Uri,
  range: vscode.Range,
  includeBaseFiles: boolean,
): Promise<vscode.CompletionItem[]> {
  const directory = vscode.Uri.joinPath(uri, "..");
  const names = new Set<string>();
  try {
    for (const [name, type] of await vscode.workspace.fs.readDirectory(
      directory,
    )) {
      if (type === vscode.FileType.File && name.endsWith(".yaml"))
        names.add(name);
    }
  } catch {
    return [];
  }
  for (const document of vscode.workspace.textDocuments) {
    if (
      isWorkflowUri(document.uri) &&
      vscode.Uri.joinPath(document.uri, "..").toString() ===
        directory.toString()
    ) {
      const name = document.uri.path.split("/").at(-1);
      if (name) names.add(name);
    }
  }

  const currentName = uri.path.split("/").at(-1);
  return [...names]
    .filter(
      (name) =>
        name !== currentName && (includeBaseFiles || !name.startsWith("_")),
    )
    .sort((left, right) => left.localeCompare(right))
    .map((name) => {
      const workflowName = name.slice(0, -".yaml".length);
      const item = new vscode.CompletionItem(
        workflowName,
        vscode.CompletionItemKind.File,
      );
      item.detail = includeBaseFiles
        ? "Workflow to extend"
        : "Followup workflow";
      item.insertText = workflowName;
      item.range = range;
      return item;
    });
}

function isWorkflowUri(uri: vscode.Uri): boolean {
  return /(?:^|\/)\.agentis\/workflows\/[^/]+\.yaml$/.test(uri.path);
}

function scalarRange(
  position: vscode.Position,
  linePrefix: string,
): vscode.Range {
  const token = linePrefix.match(/[A-Za-z0-9_.-]*$/)?.[0] ?? "";
  return new vscode.Range(
    position.line,
    position.character - token.length,
    position.line,
    position.character,
  );
}

function needsReplacementRange(
  document: vscode.TextDocument,
  position: vscode.Position,
  linePrefix: string,
): vscode.Range {
  const quoted = linePrefix.match(/(["'])[^"']*\1?$/)?.[0];
  if (!quoted) return scalarRange(position, linePrefix);
  const quote = quoted[0];
  let end = position.character;
  if (
    !quoted.endsWith(quote) &&
    document.lineAt(position.line).text[end] === quote
  ) {
    end += 1;
  }
  return new vscode.Range(
    position.line,
    position.character - quoted.length,
    position.line,
    end,
  );
}

function insideSection(
  document: vscode.TextDocument,
  currentLine: number,
  sectionName: string,
): boolean {
  let sectionIndent: number | undefined;
  for (let line = 0; line <= currentLine; line += 1) {
    const text = document.lineAt(line).text;
    if (!text.trim() || text.trimStart().startsWith("#")) continue;
    const indent = text.length - text.trimStart().length;
    if (
      sectionIndent !== undefined &&
      indent <= sectionIndent &&
      line !== currentLine
    ) {
      sectionIndent = undefined;
    }
    if (new RegExp(`^\\s*${sectionName}:\\s*(?:#.*)?$`).test(text)) {
      sectionIndent = indent;
    }
  }
  return sectionIndent !== undefined;
}

function previousStepNames(
  document: vscode.TextDocument,
  currentLine: number,
): Set<string> {
  const workflow = workflowObject(parseWorkflow(document.getText()));
  const steps = records(workflow.steps);
  const currentIndex = currentStepIndex(document, currentLine) ?? steps.length;
  const existing = new Set(
    currentIndex < steps.length && Array.isArray(steps[currentIndex].needs)
      ? steps[currentIndex].needs.filter(
          (name): name is string => typeof name === "string",
        )
      : [],
  );
  return new Set(
    steps
      .slice(0, currentIndex)
      .map((step) => step.name)
      .filter(
        (name): name is string =>
          typeof name === "string" && !existing.has(name),
      ),
  );
}

async function readWorkflowText(uri: vscode.Uri): Promise<string> {
  const open = vscode.workspace.textDocuments.find(
    (document) => document.uri.toString() === uri.toString(),
  );
  if (open) return open.getText();
  return new TextDecoder().decode(await vscode.workspace.fs.readFile(uri));
}

async function loadModel(
  document: vscode.TextDocument,
): Promise<WorkflowModel> {
  let data = parseWorkflow(document.getText());
  if (typeof data.extends === "string") {
    try {
      const parentUri = vscode.Uri.joinPath(
        document.uri,
        "..",
        `${data.extends}.yaml`,
      );
      data = mergeWorkflowData(
        parseWorkflow(await readWorkflowText(parentUri)),
        data,
      );
    } catch {
      // Diagnostics report missing or invalid parents.
    }
  }
  return createModel(data);
}

function parseWorkflow(text: string): RecordValue {
  try {
    const value: unknown = parseDocument(text).toJS();
    return isRecord(value) ? value : {};
  } catch {
    return {};
  }
}

function mergeWorkflowData(
  parent: RecordValue,
  child: RecordValue,
): RecordValue {
  const parentWorkflow = workflowObject(parent);
  const childWorkflow = workflowObject(child);
  return {
    ...parent,
    ...child,
    workflow: {
      ...parentWorkflow,
      ...childWorkflow,
      env: {
        ...(isRecord(parentWorkflow.env) ? parentWorkflow.env : {}),
        ...(isRecord(childWorkflow.env) ? childWorkflow.env : {}),
      },
      stepTemplates: {
        ...(isRecord(parentWorkflow.stepTemplates)
          ? parentWorkflow.stepTemplates
          : {}),
        ...(isRecord(childWorkflow.stepTemplates)
          ? childWorkflow.stepTemplates
          : {}),
      },
      envFiles: mergeWorkflowList(
        parentWorkflow.envFiles,
        childWorkflow.envFiles,
      ),
      mounts: mergeWorkflowList(parentWorkflow.mounts, childWorkflow.mounts),
      imagePullSecrets: mergeWorkflowList(
        parentWorkflow.imagePullSecrets,
        childWorkflow.imagePullSecrets,
      ),
      steps: childWorkflow.steps,
      followups: childWorkflow.followups,
    },
  };
}

function mergeWorkflowList(parent: unknown, child: unknown): unknown[] {
  const merged = Array.isArray(parent) ? [...parent] : [];
  const indexes = new Map<string, number>();
  merged.forEach((item, index) => {
    if (isRecord(item) && typeof item.name === "string") {
      indexes.set(item.name, index);
    }
  });
  for (const item of Array.isArray(child) ? child : []) {
    if (
      isRecord(item) &&
      typeof item.name === "string" &&
      indexes.has(item.name)
    ) {
      merged[indexes.get(item.name)!] = item;
    } else if (
      !merged.some(
        (existing) => JSON.stringify(existing) === JSON.stringify(item),
      )
    ) {
      merged.push(item);
    }
  }
  return merged;
}

function createModel(data: RecordValue): WorkflowModel {
  const workflow = workflowObject(data);
  const templates = new Set<string>();
  const variables = new Set<string>(CONDITION_VARIABLES);
  if (isRecord(workflow.stepTemplates)) {
    for (const name of Object.keys(workflow.stepTemplates)) templates.add(name);
  }
  collectEnv(workflow, variables);
  return { data, templates, variables };
}

function visibleConditionVariables(
  model: WorkflowModel,
  document: vscode.TextDocument,
  currentLine: number,
): Set<string> {
  const variables = new Set(model.variables);
  const workflow = workflowObject(model.data);
  const templates = isRecord(workflow.stepTemplates)
    ? workflow.stepTemplates
    : {};
  const steps = records(workflow.steps);

  if (insideSection(document, currentLine, "stepTemplates")) {
    const name = currentTemplateName(document, currentLine);
    if (name) collectEnv(templates[name], variables);
    return variables;
  }
  if (insideSection(document, currentLine, "followups")) {
    for (const step of steps)
      collectOutputVariables(step, templates, variables);
    return variables;
  }

  const index = currentStepIndex(document, currentLine);
  if (index !== undefined && index < steps.length) {
    const step = steps[index];
    collectEnv(step, variables);
    if (typeof step.uses === "string")
      collectEnv(templates[step.uses], variables);
    const dependencies = new Set<number>();
    collectDependencyIndexes(steps, index, dependencies);
    for (const dependency of dependencies) {
      collectOutputVariables(steps[dependency], templates, variables);
    }
  }
  return variables;
}

function currentTemplateName(
  document: vscode.TextDocument,
  currentLine: number,
): string | undefined {
  let sectionIndent: number | undefined;
  let name: string | undefined;
  for (let line = 0; line <= currentLine; line += 1) {
    const text = document.lineAt(line).text;
    if (!text.trim() || text.trimStart().startsWith("#")) continue;
    const indent = text.length - text.trimStart().length;
    if (sectionIndent !== undefined && indent <= sectionIndent) {
      sectionIndent = undefined;
      name = undefined;
    }
    if (/^\s*stepTemplates:\s*(?:#.*)?$/.test(text)) {
      sectionIndent = indent;
      continue;
    }
    if (sectionIndent !== undefined && indent === sectionIndent + 2) {
      const match = text.match(/^\s*([^:#]+):\s*(?:#.*)?$/);
      if (match) name = match[1].trim();
    }
  }
  return name;
}

function currentStepIndex(
  document: vscode.TextDocument,
  currentLine: number,
): number | undefined {
  let sectionIndent: number | undefined;
  let index = -1;
  for (let line = 0; line <= currentLine; line += 1) {
    const text = document.lineAt(line).text;
    if (!text.trim() || text.trimStart().startsWith("#")) continue;
    const indent = text.length - text.trimStart().length;
    if (sectionIndent !== undefined && indent <= sectionIndent)
      sectionIndent = undefined;
    if (/^\s*steps:\s*(?:#.*)?$/.test(text)) {
      sectionIndent = indent;
      continue;
    }
    if (
      sectionIndent !== undefined &&
      indent === sectionIndent + 2 &&
      /^\s*-\s+/.test(text)
    ) {
      index += 1;
    }
  }
  return index >= 0 ? index : undefined;
}

function collectDependencyIndexes(
  steps: RecordValue[],
  index: number,
  result: Set<number>,
): void {
  const step = steps[index];
  const names = new Map(
    steps.map((item, itemIndex) => [item.name, itemIndex] as const),
  );
  const dependencies = Array.isArray(step?.needs)
    ? step.needs
        .map((name) => names.get(name))
        .filter(
          (dependency): dependency is number =>
            dependency !== undefined && dependency < index,
        )
    : index > 0
      ? [index - 1]
      : [];
  for (const dependency of dependencies) {
    if (result.has(dependency)) continue;
    result.add(dependency);
    collectDependencyIndexes(steps, dependency, result);
  }
}

function collectEnv(value: unknown, variables: Set<string>): void {
  if (!isRecord(value) || !isRecord(value.env)) return;
  for (const name of Object.keys(value.env)) variables.add(name);
}

function collectOutputVariables(
  step: RecordValue,
  templates: RecordValue,
  variables: Set<string>,
): void {
  const candidate =
    typeof step.uses === "string" ? templates[step.uses] : undefined;
  const template = isRecord(candidate) ? candidate : {};
  const outputs = Array.isArray(step.outputs)
    ? step.outputs
    : Array.isArray(template.outputs)
      ? template.outputs
      : [];
  for (const output of outputs) {
    if (
      isRecord(output) &&
      output.type === "var" &&
      typeof output.name === "string"
    ) {
      variables.add(output.name);
    }
  }
}

async function updateDiagnostics(
  document: vscode.TextDocument,
  collection: vscode.DiagnosticCollection,
): Promise<void> {
  if (!isWorkflowUri(document.uri)) {
    collection.delete(document.uri);
    return;
  }
  const key = document.uri.toString();
  const generation = (diagnosticGenerations.get(key) ?? 0) + 1;
  diagnosticGenerations.set(key, generation);
  const version = document.version;
  const diagnostics: vscode.Diagnostic[] = [];
  const data = parseWorkflow(document.getText());
  const workflow = isRecord(data.workflow) ? data.workflow : undefined;
  if (!workflow) {
    setCurrentDiagnostics(
      document,
      collection,
      diagnostics,
      generation,
      version,
    );
    return;
  }

  const model = await loadModel(document);
  const effectiveWorkflow = workflowObject(model.data);
  const unknownTokens = new Set<string>();
  collectUnknownTokens(model.data, unknownTokens);
  for (const token of unknownTokens) {
    diagnostics.push(
      diagnostic(
        propertyRange(document, "workflow"),
        `Unknown Agentis interpolation token [%${token}%].`,
      ),
    );
  }

  await addExtendsDiagnostics(document, data, diagnostics);
  const steps = records(workflow.steps);
  const stepRanges = listItemRanges(document, "steps");
  if (
    !Array.isArray(workflow.steps) &&
    !(await isReferencedAsParent(document.uri))
  ) {
    diagnostics.push(
      diagnostic(
        propertyRange(document, "workflow"),
        "Workflow has no steps and cannot be executed; omit steps only in a file used as an extends parent.",
        vscode.DiagnosticSeverity.Warning,
      ),
    );
  }

  const usesNeeds = steps.some(
    (step) => step.needs !== undefined && step.needs !== null,
  );
  const counts = new Map<string, number>();
  for (const step of steps) {
    if (typeof step.name === "string") {
      counts.set(step.name, (counts.get(step.name) ?? 0) + 1);
    }
  }
  const previous = new Set<string>();
  for (const [index, step] of steps.entries()) {
    const range = stepRanges[index] ?? propertyRange(document, "steps");
    if (typeof step.uses === "string" && !model.templates.has(step.uses)) {
      diagnostics.push(
        diagnostic(range, `Step uses unknown template '${step.uses}'.`),
      );
    }
    if (
      usesNeeds &&
      typeof step.name === "string" &&
      counts.get(step.name)! > 1
    ) {
      diagnostics.push(
        diagnostic(
          range,
          `Step name '${step.name}' must be unique when needs is used.`,
        ),
      );
    }
    if (Array.isArray(step.needs)) {
      const invalid = step.needs.filter(
        (name) => typeof name === "string" && !previous.has(name),
      );
      if (invalid.length > 0) {
        diagnostics.push(
          diagnostic(
            range,
            `needs references unknown or future steps: ${invalid.join(", ")}.`,
          ),
        );
      }
    }
    if (typeof step.name === "string") previous.add(step.name);
  }

  const mountRanges = listItemRanges(document, "mounts");
  records(effectiveWorkflow.mounts).forEach((mount, index) => {
    const hasSource = Object.entries(mount).some(
      ([name, value]) => !MOUNT_FIELDS.has(name) && value !== null,
    );
    if (!hasSource) {
      diagnostics.push(
        diagnostic(
          mountRanges[index] ?? propertyRange(document, "mounts"),
          "Mount requires a Kubernetes volume source such as hostPath, secret, configMap, emptyDir, or persistentVolumeClaim.",
        ),
      );
    }
  });
  setCurrentDiagnostics(document, collection, diagnostics, generation, version);
}

function setCurrentDiagnostics(
  document: vscode.TextDocument,
  collection: vscode.DiagnosticCollection,
  diagnostics: vscode.Diagnostic[],
  generation: number,
  version: number,
): void {
  if (
    document.version === version &&
    diagnosticGenerations.get(document.uri.toString()) === generation
  ) {
    collection.set(document.uri, diagnostics);
  }
}

function collectUnknownTokens(value: unknown, result: Set<string>): void {
  if (typeof value === "string") {
    for (const match of value.matchAll(/\[%([A-Z_]+)%\]/g)) {
      if (!TOKENS.includes(match[1] as (typeof TOKENS)[number]))
        result.add(match[1]);
    }
  } else if (Array.isArray(value)) {
    for (const item of value) collectUnknownTokens(item, result);
  } else if (isRecord(value)) {
    for (const item of Object.values(value)) collectUnknownTokens(item, result);
  }
}

async function addExtendsDiagnostics(
  document: vscode.TextDocument,
  data: RecordValue,
  diagnostics: vscode.Diagnostic[],
): Promise<void> {
  if (typeof data.extends !== "string") return;
  const range = propertyRange(document, "extends");
  const current = document.uri.path
    .split("/")
    .at(-1)
    ?.replace(/\.yaml$/, "");
  if (data.extends === current) {
    diagnostics.push(diagnostic(range, "A workflow cannot extend itself."));
    return;
  }
  try {
    const parent = parseWorkflow(
      await readWorkflowText(
        vscode.Uri.joinPath(document.uri, "..", `${data.extends}.yaml`),
      ),
    );
    if (parent.extends !== undefined && parent.extends !== null) {
      diagnostics.push(
        diagnostic(
          range,
          `Chained extends is not supported; '${data.extends}' also declares extends.`,
        ),
      );
    }
  } catch {
    diagnostics.push(
      diagnostic(
        range,
        `Workflow extends target '${data.extends}.yaml' was not found.`,
      ),
    );
  }
}

async function isReferencedAsParent(uri: vscode.Uri): Promise<boolean> {
  const directory = vscode.Uri.joinPath(uri, "..");
  const current = uri.path
    .split("/")
    .at(-1)
    ?.replace(/\.yaml$/, "");
  if (!current) return false;
  const checked = new Set<string>();
  for (const document of vscode.workspace.textDocuments) {
    if (
      !isWorkflowUri(document.uri) ||
      vscode.Uri.joinPath(document.uri, "..").toString() !==
        directory.toString() ||
      document.uri.toString() === uri.toString()
    ) {
      continue;
    }
    checked.add(document.uri.toString());
    if (parseWorkflow(document.getText()).extends === current) return true;
  }
  try {
    for (const [name, type] of await vscode.workspace.fs.readDirectory(
      directory,
    )) {
      if (
        type !== vscode.FileType.File ||
        !name.endsWith(".yaml") ||
        name === `${current}.yaml`
      ) {
        continue;
      }
      const child = vscode.Uri.joinPath(directory, name);
      if (checked.has(child.toString())) continue;
      if (parseWorkflow(await readWorkflowText(child)).extends === current)
        return true;
    }
  } catch {
    return false;
  }
  return false;
}

function listItemRanges(
  document: vscode.TextDocument,
  sectionName: string,
): vscode.Range[] {
  const ranges: vscode.Range[] = [];
  let sectionIndent: number | undefined;
  for (let line = 0; line < document.lineCount; line += 1) {
    const text = document.lineAt(line).text;
    if (!text.trim() || text.trimStart().startsWith("#")) continue;
    const indent = text.length - text.trimStart().length;
    if (sectionIndent !== undefined && indent <= sectionIndent)
      sectionIndent = undefined;
    if (new RegExp(`^\\s*${sectionName}:\\s*(?:#.*)?$`).test(text)) {
      sectionIndent = indent;
      continue;
    }
    if (
      sectionIndent !== undefined &&
      indent === sectionIndent + 2 &&
      /^\s*-\s+/.test(text)
    ) {
      ranges.push(lineRange(document, line));
    }
  }
  return ranges;
}

function propertyRange(
  document: vscode.TextDocument,
  name: string,
): vscode.Range {
  const pattern = new RegExp(`^(\\s*)${name}:`);
  for (let line = 0; line < document.lineCount; line += 1) {
    const match = document.lineAt(line).text.match(pattern);
    if (match) {
      return new vscode.Range(
        line,
        match[1].length,
        line,
        match[1].length + name.length,
      );
    }
  }
  return new vscode.Range(0, 0, 0, Math.min(document.lineAt(0).text.length, 1));
}

function lineRange(document: vscode.TextDocument, line: number): vscode.Range {
  const text = document.lineAt(line).text;
  const start = text.length - text.trimStart().length;
  return new vscode.Range(line, start, line, text.length);
}

function diagnostic(
  range: vscode.Range,
  message: string,
  severity = vscode.DiagnosticSeverity.Error,
): vscode.Diagnostic {
  const value = new vscode.Diagnostic(range, message, severity);
  value.source = "Agentis workflow";
  return value;
}

function completionItems(
  values: Iterable<string>,
  range: vscode.Range,
  kind: vscode.CompletionItemKind,
  detail: string,
): vscode.CompletionItem[] {
  return [...values]
    .sort((left, right) => left.localeCompare(right))
    .map((value) => {
      const item = new vscode.CompletionItem(value, kind);
      item.detail = detail;
      item.insertText = value;
      item.range = range;
      return item;
    });
}

function workflowObject(data: RecordValue): RecordValue {
  return isRecord(data.workflow) ? data.workflow : {};
}

function records(value: unknown): RecordValue[] {
  return Array.isArray(value) ? value.filter(isRecord) : [];
}

function isRecord(value: unknown): value is RecordValue {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
