# CLAUDE.md

Orientation for Claude working in this repo.

## What this repo is

Tooling to regenerate the documented example outputs of the **Voyage AI docs**
(`content/voyageai` inside the sibling docs repo `docs-mongodb-internal`) after
every major Voyage AI model release, routing LLM API calls through MongoDB's
**Grove** gateway. Scope is strictly `content/voyageai`; the docs-notebooks and
llms.txt are out of scope.

Two sibling repos sit under the parent dir `..`:

- `docs-mongodb-internal/` — the docs monorepo (read-only input; this tool never edits it)
- `update-voyage-output/` — **this repo**

## The pipeline (one script, opt-in flags)

- `update_voyage_output.py inventory` — scan `content/voyageai` for documented
  outputs (`.. output::` directive file-backed or inline, plus unreferenced
  `*-output.md` files) → write `inventory.yaml`.
- `update_voyage_output.py convert` — copy each example into `converted/<mirror>`,
  rewriting LLM-calling files to point at Grove (`api-key` header, Anthropic
  route `<base>/anthropic`, OpenAI route `<base>/openai/v1`, `GROVE_MODEL`
  override). Non-LLM examples copied verbatim.
- `update_voyage_output.py run` — execute each converted example with the venv
  python and write stdout to `outputs/<mirror>` (+ `outputs/manifest.yaml`).
- `update_voyage_output.py all` — inventory → convert → run.

Env: `GROVE_API_KEY` + `VOYAGE_API_KEY` required for convert/run/all;
`MONGODB_URI` needed for the two `rag_mongodb_*` app variants; `GROVE_MODEL`
pins the routed LLM model; `GROVE_BASE_URL` overrides the gateway.

When `GROVE_MODEL` is unset, `run` auto-falls back to a comparable model if
Grove 404s on the doc's literal model id (see the `models` subcommand to list
what the deployment exposes, and `grove.model_fallbacks` in `inventory.yaml`
to tune). The model that produced each output is recorded in
`outputs/manifest.yaml`.

Docs repo location: `--docs-repo <absolute path>` (or `DOCS_REPO` env) points
at the user's clone. Resolution order: flag → env → `inventory.yaml`'s
`docs_repo` (so convert/run work after the clone moves) → sibling
`../docs-mongodb-internal`. A supplied-but-invalid path fails fast
(`resolve_docs_repo`).

## Key files

- `update_voyage_output.py` — the whole tool.
- `inventory.yaml` — durable, curatable inventory. The `CURATED` dict inside the
  script (and user edits to this YAML) own the `derived_from` mapping
  output → example(s); the scan owns `location`/`rendered_in`/models.
- `DESIGN.md` — full design rationale, Grove semantics, release checklist,
  known quirks. **Read it before changing conversion or run behavior.**
- `.venv/` — uv-managed venv (Python 3.13) with all example deps installed.

## Working conventions

- Use `.venv/bin/python` (uv venv, per project convention).
- Never edit anything under `docs-mongodb-internal/`; treat it as read-only input.
- Generated trees (`converted/`, `outputs/`) are gitignored; commit
  `update_voyage_output.py`, `inventory.yaml`, and the docs only.
- LLM outputs are non-deterministic by nature; the caller diffs/merges rather
  than expecting byte-exact matches (the docs themselves say "output will vary").
