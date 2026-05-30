#!/usr/bin/env bash

if [ -f ".venv/bin/activate" ]; then
  . ".venv/bin/activate"
elif git_common_dir="$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null)"; then
  main_worktree="$(dirname "$git_common_dir")"
  if [ -f "$main_worktree/.venv/bin/activate" ]; then
    . "$main_worktree/.venv/bin/activate"
  fi
fi

echo "agentis-adapter" > /tmp/local-setup.txt