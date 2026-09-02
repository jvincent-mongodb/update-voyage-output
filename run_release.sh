#!/usr/bin/env bash
# Run a full Voyage AI model-release pass: inventory -> convert -> run.
#
# Usage:
#   ./run_release.sh [extra args forwarded to `update_voyage_output.py all`]
#
# Required env: GROVE_API_KEY, VOYAGE_API_KEY.
# Recommended env for the two RAG-with-MongoDB app variants: MONGODB_URI.
# Optional env: GROVE_BASE_URL, GROVE_MODEL, DOCS_REPO
#     DOCS_REPO — absolute path to your clone of the docs monorepo
#                 (default: the path recorded in inventory.yaml).
# Optional flag forwarded: --assets-dir <dir>  (for cat.jpg/dog.jpg/banana.jpg)
set -euo pipefail

cd "$(dirname "$0")"
PY="${PYTHON:-$PWD/.venv/bin/python}"

if [[ ! -x "$PY" ]]; then
  echo "error: no interpreter at $PY" >&2
  echo "create it with:  uv venv --python 3.13 .venv && uv pip install --python .venv/bin/python -r requirements.txt" >&2
  exit 1
fi

for var in GROVE_API_KEY VOYAGE_API_KEY; do
  if [[ -z "${!var:-}" ]]; then
    echo "error: ${var} is not set (required)" >&2
    exit 1
  fi
done
if [[ -z "${MONGODB_URI:-}" ]]; then
  echo "warn: MONGODB_URI is not set — the rag_mongodb_* outputs will be skipped" >&2
fi

args=()
if [[ -n "${DOCS_REPO:-}" ]]; then
  args+=(--docs-repo "$DOCS_REPO")
fi
exec "$PY" update_voyage_output.py all --timeout "${TIMEOUT:-3600}" "${args[@]}" "$@"
