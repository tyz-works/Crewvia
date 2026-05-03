#!/usr/bin/env bash
# Initialize bench/fixture/ as a standalone git repo with the bugged state at HEAD.
# Run once before benchmark execution. Safe to re-run (resets to bugged state).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
FIXTURE_DIR="$SCRIPT_DIR/fixture"

cd "$FIXTURE_DIR"

if [ -d .git ]; then
  echo "[init-fixture] resetting existing repo to bugged state..."
  # Find the initial "bugged state" commit (oldest) and reset to it
  INITIAL_COMMIT=$(git log --oneline | tail -1 | awk '{print $1}')
  git reset --hard "$INITIAL_COMMIT"
  git clean -fd
  echo "[init-fixture] HEAD reset to initial: $(git rev-parse --short HEAD)"
else
  echo "[init-fixture] initializing git repo in $FIXTURE_DIR..."
  git init
  git add .
  git commit -m "initial: bugged state (5 bugs in src/utils.ts)"
  echo "[init-fixture] done. HEAD = $(git rev-parse --short HEAD)"
fi
