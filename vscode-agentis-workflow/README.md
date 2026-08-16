# Agentis Workflow

VS Code language support for declarative workflow files stored in `.agentis/workflows/*.yaml`.

## Features

- YAML validation, property completion, enum completion, snippets, and hover documentation.
- Highlighting and completion for supported `[%TOKEN%]` interpolation values.
- Context-aware completion for `extends`, followup `workflow`, `uses`, `needs`, and `if`.
- Go to Definition for workflow files, step templates, steps, and locally defined condition variables.
- Cross-file completion from the one-level `extends` parent, including unsaved editor content.
- Live diagnostics for interpolation tokens, inheritance, template references, DAG dependencies, duplicate step names, and mount volume sources.
- Bold formatting for workflow step names.
- Light gray backgrounds for workflow `run` blocks.

The extension installs `redhat.vscode-yaml` as a dependency. Runtime validation in `common/workflow/schema.py` remains authoritative.

## Development

```bash
npm install
npm test
npm run typecheck
npm run build:extension
npm run package
```

The packaged extension is written to `dist/agentis-workflow.vsix`.
