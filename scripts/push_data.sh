#!/usr/bin/env bash
#
# push_data.sh — Push classified + latest data to an external data repository.
#
# This script clones the target data repo, copies the classified and latest
# data into it, commits, and pushes. It expects a GitHub token with write
# access to the target repo.
#
# Environment variables:
#   DATA_REPO_URL   — Full clone URL of the data repo
#                     (e.g. https://x-access-token:$TOKEN@github.com/YouthOpp/data.git)
#   DATA_BRANCH     — Branch to push to (default: main)
#
# Usage:
#   DATA_REPO_URL="https://x-access-token:${GITHUB_TOKEN}@github.com/YouthOpp/data.git" \
#     bash scripts/push_data.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

DATA_REPO_URL="${DATA_REPO_URL:?DATA_REPO_URL environment variable is required}"
DATA_BRANCH="${DATA_BRANCH:-main}"

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

echo "[push_data] Cloning data repo → $WORK_DIR"
git clone --depth 1 --branch "$DATA_BRANCH" "$DATA_REPO_URL" "$WORK_DIR" 2>/dev/null || \
  git clone --depth 1 "$DATA_REPO_URL" "$WORK_DIR"

# Copy classified data
if [ -d "$REPO_ROOT/data/classified" ]; then
  echo "[push_data] Copying classified data …"
  rm -rf "$WORK_DIR/classified"
  cp -r "$REPO_ROOT/data/classified" "$WORK_DIR/classified"
fi

# Copy latest dataset
if [ -d "$REPO_ROOT/data/latest" ]; then
  echo "[push_data] Copying latest dataset …"
  rm -rf "$WORK_DIR/latest"
  mkdir -p "$WORK_DIR/latest"
  cp "$REPO_ROOT/data/latest/opportunities.json"  "$WORK_DIR/latest/" 2>/dev/null || true
  cp "$REPO_ROOT/data/latest/opportunities.jsonl" "$WORK_DIR/latest/" 2>/dev/null || true
fi

# Commit and push
cd "$WORK_DIR"
git config user.name  "github-actions[bot]"
git config user.email "github-actions[bot]@users.noreply.github.com"
git add -A

if git diff --cached --quiet; then
  echo "[push_data] No changes to push."
else
  TIMESTAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  git commit -m "chore: update data — $TIMESTAMP"
  git push origin "$DATA_BRANCH"
  echo "[push_data] Pushed to $DATA_BRANCH."
fi

echo "[push_data] Done."
