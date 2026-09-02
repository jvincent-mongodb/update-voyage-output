# Design notes — `update_voyage_output.py`

This file is the record of why the tool is shaped the way it is, what the
"documented outputs" of the Voyage AI docs are, and the exact semantics the
Grove conversion + run stages implement. Read it before changing behavior, and
extend it when the model-release workflow changes.

---

## 1. Problem & scope

The MongoDB docs monorepo (`docs-mongodb-internal`) ships a Voyage AI docset
under `content/voyageai/`. Many of its pages display **documented output** —
the stdout a reader should see when they run a code example. Those outputs hard
on the assumption that the current Voyage models rank/tokenize in a particular
way, so on every **major Voyage AI model release** the outputs shown in the
docs go stale and must be regenerated.

Two wrinkles make this a pipeline instead of a one-liner:

1. Several of the examples call an **LLM** (Anthropic `claude-*`, OpenAI
   `gpt-*`) to write the answer. A doc-verification run cannot use a personal
   provider key; MongoDB's internal gateway, **Grove**, fronts LLM calls and is
   what a release run should go through.
2. The output of each example must be captured and laid out so a writer can
   diff it against (and merge it back into) the docs PR.

**Scope boundary (per the requester):** only outputs inside `content/voyageai`
in the docs repo. The `ext-source/docs-notebooks/voyageai` notebooks and the
`platform/tools/generate-llms/llms-output/voyageai/llms.txt` are **out of scope**.

## 2. The three+ stages (one script, opt-in flags)

`update_voyage_output.py` implements everything as subcommands:

| Subcommand  | Does                                                   | Writes |
|-------------|--------------------------------------------------------|--------|
| `inventory` | Scans `content/voyageai` and inventories every documented output + the examples that produce it | `inventory.yaml` |
| `convert`   | Copies each example into `converted/<monorepo-mirror>`, rewriting LLM-calling files to use Grove | `converted/` |
| `run`       | Executes each converted example and captures stdout  | `outputs/<monorepo-mirror>` + `outputs/manifest.yaml` |
| `all`       | `inventory` → `convert` → `run` in one pass           | everything above |

Rationale for one script with flags rather than separate tools: the three
stages share one input (the inventory) and one mental model, and a release is
always the same ordering. The inventory is the durable artifact a writer edits
between `inventory` and `convert`.

### Required environment

`GROVE_API_KEY` **and** `VOYAGE_API_KEY` are required for `convert` / `run` /
`all`. `--no-key-check` bypasses the check for local dry-runs of the static
stages. Optional: `GROVE_BASE_URL`, `GROVE_MODEL`.

### Docs monorepo location

The docs clone can live anywhere. `--docs-repo <absolute path>` (or the
`DOCS_REPO` env var) points at it. Resolution order, first match wins:

1. `--docs-repo` (command line) — authoritative; a supplied-but-bad path is a
   hard error, not a silent fallthrough;
2. `$DOCS_REPO` (env);
3. the `docs_repo` recorded in `inventory.yaml` (so a later `convert`/`run`
   works even when the clone moved since `inventory` ran);
4. the sibling `../docs-mongodb-internal`.

The chosen path must be a directory containing `content/voyageai`. `inventory`
stores the resolved absolute path in `inventory.yaml` (`docs_repo:`), which is
what makes the fallback in step 3 work.

The interpreter used to *run* examples defaults to `<repo>/.venv/bin/python`
when present (it must have `voyageai`, `anthropic`, `openai`, `numpy`,
`pymongo`, `langchain-community`, `langchain-text-splitters`, `pypdf`,
`python-dotenv`, `Pillow`, `datasets`). Pass `--python` to override.

## 3. What counts as a "documented output"

Provided via the docs team's custom `.. output::` directive, two shapes:

- **File-backed**: `.. output:: /includes/rag/shared/rag-output.md` — the output
  lives in a markdown file under `source/includes/` that the page pulls in with
  the directive. `rendered_in` records the page that references it.
- **Inline**: `.. output::` with no path and body content underneath, inside a
  `.. example::`/`.. io-code-block::`. The output text is embedded in the page.

One more implicit case: **standalone output `.md` files under
`source/includes/`** that are *not* referenced by any directive yet (the real
example today: `includes/rag/shared/in-memory-rag-output.md`). These are
inventoried too, flagged `rendered_in: (not referenced by an output:: directive yet)`.

The 14 inventory entries today (verified against the repo):

- 6 `semantic-search/*-output.md` ← `semantic_search_*.py` (pure Voyage, no Grove)
- `rag/shared/rag-output.md` ← the MongoDB RAG app (`main.py` pipeline; both
  Anthropic and OpenAI variants, needs `MONGODB_URI`, interactive stdin)
- `rag/shared/in-memory-rag-output.md` ← `in-memory-rag.py` (both variants)
- `quickstart.txt` inline, "Generate Your First Embeddings" ← `generate-embeddings.py`
- `quickstart/step-run-app.rst` inline ← `rag-application-{anthropic,openai}.py`
- `rag/shared/run-pipeline-instructions-in-memory.rst` inline ← `in-memory-rag.py`
- `tutorials/tokenization.txt` inline ×3 (`tokenize`, `count_tokens`,
  `count_usage`) ← inline `.. input::` python blocks, extracted verbatim

## 4. Inventory schema (`inventory.yaml`)

```yaml
docs_repo: /abs/path/to/docs-mongodb-internal
scope: content/voyageai
grove: {base_url: ..., model_env: GROVE_MODEL}
voyage_models_observed: [rerank-2.5, rerank-3, voyage-4-large, ...]  # pulled from example sources
outputs:
  - id: file-shared-rag-output            # deterministic: file-<dir>-<stem> | inline-<page>-<slug>
    title: …
    location: {kind: file|inline, path: <repo-relative>, line: <line of ..output::>, block: <slug>?}
    rendered_in: [<repo-relative page paths that pull this output in>]
    derived_from:
      - name: rag_mongodb_anthropic        # example slug
        kind: script|app|block
        source: <repo-relative file or base dir>
        files: […]                        # app: files assembled into a flat runnable dir
        entrypoint: main.py               # app
        block: tokenize-method            # block: inline-block slug on `source`
        needs_grove: true
        providers: [anthropic|openai]
        llm_models: [claude-sonnet-4-5-20250929]   # literals that get GROVE_MODEL-overridden
        model: claude-sonnet-4-5-20250929 # informational (primary llm_models[0])
        env_vars: [MONGODB_URI]           # forwarded from the shell; warned about when missing
        assets: [cat.jpg, dog.jpg, banana.jpg]  # copied from --assets-dir into run cwd
        stdin: [Y, What are the latest…]  # bytes piped to the process (interactive apps)
        mirror_target: content/voyageai/… # where stdout lands under outputs/
stale_output_ids: []                      # ids present before but no longer detected
```

`id` is **position-independent** so a release doesn't shuffle/invalidate ids
when outputs are added or removed: file outputs get `file-<parent-dir>-<stem>`,
inline outputs get `inline-<page-stem>-<block-slug>`.

### Who curates what

- The **scan** owns `location`, `rendered_in`, and `voyage_models_observed`.
- The **script's `CURATED` map** owns `derived_from` (which examples produce
  each output). It is keyed by detection key. On a release, when the docs add a
  brand-new output, the scan finds it and the entry shows up with an empty
  `derived_from` and a "add to CURATED" hint.
- **You**, between `inventory` and `convert`, own edits to `inventory.yaml`.
  On regeneration the tool keeps any hand-written `derived_from` that already
  exists (so your mapping fixes survive the next `inventory` run).

## 5. Grove conversion semantics

Grove: **base URL** `https://grove-gateway-prod.azure-api.net/grove-foundry-prod`,
authenticated via an **`api-key` header** (not the SDKs' default
`x-api-key`/`Authorization`). Route paths:

- OpenAI-compatible: `<base>/openai/v1` (the OpenAI SDK appends `/chat/completions`)
- Anthropic-compatible: `<base>/anthropic` (the Anthropic SDK appends `/v1/messages`)

Per internal Grove docs/GUIDEs (Grove GenAI Hub, grove-python architecture,
`grove.py` in 10gen repos, OpenCode config): the Anthropic Python SDK wants the
base URL **without** `/v1` (it appends it), and the OpenAI Python SDK wants it
**with** `/v1`. The conversion encodes exactly that. If a future Grove doc says
otherwise, revisit these two URL spellings.

For a file that uses an LLM, `convert` applies:

1. Ensure `import os`.
2. Rewrite client construction, **preserving the exact name that matched**
   (`anthropic.Anthropic(...)` from `import anthropic`, or bare
   `Anthropic(...)`/`OpenAI(...)` from a `from … import`) and swapping in
   Grove args:
   - `Anthropic(base_url=f"{GROVE_BASE_URL}/anthropic", api_key="unused",
     default_headers={"api-key": GROVE_API_KEY})`
   - `OpenAI(base_url=f"{GROVE_BASE_URL}/openai/v1", api_key="unused",
     default_headers={"api-key": GROVE_API_KEY})`
   Only constructor calls that carry `api_key=` (or are empty) are rewritten,
   so unrelated call sites are untouched.
3. Override **LLM model literals** (from `llm_models`) with
   `os.environ.get("GROVE_MODEL", "<original>")` — both inline `model="…"`
   arguments and `ANTHROPIC_MODEL = "…"` / `OPENAI_MODEL = "…"` assignments.
   `GROVE_MODEL` lets a release point all LLM calls at one Grove-hosted model
   without editing source. Voyage model ids (`voyage-*`, `rerank-*`) are never
   touched — they stay on the Voyage API.
4. Inject a runtime guard that aborts unless `GROVE_API_KEY` is set.
5. Stamp the header with `CONVERTED_BANNER` (idempotent — re-converting is a no-op).

Files that never touch an LLM (every `semantic_search_*.py`,
`generate-embeddings.py`, the inline `tokenize`/`count_tokens`/`count_usage`
blocks) are copied **verbatim** into `converted/` — they still need to be run,
just not converted.

### App assembly

The MongoDB-RAG app (`rag/shared/main.py`) imports flat sibling modules
(`config`, `ingest_data`, `retrieve_data`, `generate_response`). `convert`
therefore assembles a **flat runnable directory** per provider variant under
`converted/…/rag/<example-name>/` with the doc-specified `main.py` as
entrypoint, and feeds interactive stdin (`Y`, then the question) at run time.

## 6. Run semantics

`run` executes each example in its converted directory with the venv python,
feeds `stdin` when configured, and writes captured stdout to:

### Grove model fallback

Grove only deploys the model ids your entitlement provisions; a docs example
pins a literal model id that may 404 with `DeploymentNotFound` (this happened
for `claude-sonnet-4-5-20250929`, while `gpt-4o` was fine). The `run` stage
therefore tries models in this order and records which one actually produced
each output (`manifest.yaml` → per-run `model`, also printed per run):

1. `$GROVE_MODEL` — user pin; if set, it is the *only* model tried;
2. the doc's literal model id (`derived_from[].model`);
3. `grove.model_fallbacks[<model>]` — exact-id overrides, generated into
   `inventory.yaml` from `GROVE_MODEL_FALLBACKS` and editable there;
4. provider-level defaults (`ANTHROPIC_FALLBACK_MODELS` /
   `OPENAI_FALLBACK_MODELS` in the script).

A retry only happens when the failure plausibly means "model unavailable"
(DeploymentNotFound / "does not exist" / model_not_found, …) — auth, timeout,
and code errors are surfaced immediately, never retried. See `--exclude` /
`--limit` to skip or isolate examples while tuning the fallback list. Use
`update_voyage_output.py models` (or the Grove dashboard) to see which model ids
your deployment actually exposes, so the fallbacks match your catalog.

### Output mirror



- **file outputs** → `outputs/<output file repo-relative path>` (first variant)
  and `<stem>.<example-name><ext>` for extra variants;
- **inline outputs** → `outputs/<page-dir>/<page-stem>/<example-name>.txt`.

That is the "directory structure … that parallels its location in the monorepo"
requirement. A `outputs/manifest.yaml` records per-example exit status, model and target.
`run` also prints a human-readable summary at the end, grouping failures by
reason (missing asset `cat.jpg`/`banana.jpg`, missing `MONGODB_URI`, model
unavailable, exit error) with counts, example names, the Grove models used, and
a one-line "To fix" hint — so a writer can spot incomplete runs at a glance.
LLM answers are
non-deterministic by nature — the docs themselves say "output will vary" — so
`run` makes no attempt to match text, only to produce a fresh, honest capture.

Assets (images): `--assets-dir DIR` provides `cat.jpg`/`dog.jpg`/`banana.jpg`;
if the file is missing the example is skipped with an actionable message
(multimodal example + the `count_usage` tokenization block both need them).

## 7. Release checklist (a "major Voyage AI model release")

1. Docs PR updates the example scripts/pages (model ids, expected output text).
2. `uv run --with pyyaml update_voyage_output.py inventory` (or
   `.venv/bin/python update_voyage_output.py inventory`).
   - Inspect: any new output with empty `derived_from`? Any `stale_output_ids`?
   - Edit `inventory.yaml` (or the `CURATED` map) if the scan needs help.
3. Export `GROVE_API_KEY` and `VOYAGE_API_KEY` (and `MONGODB_URI` for the two
   `rag_mongodb_*` app variants).
4. `update_voyage_output.py all --timeout 3600` to run inventory → convert → run.
   - If image assets are missing, populate a dir and pass `--assets-dir`.
5. Diff `outputs/content/voyageai/source/...` against the docs outputs and
   merge the fresh numbers into the docs PR.

## 8. Tested / remaining verification

Verified in this environment (no API keys set):

- `inventory` reproduces all 14 documented outputs, correct `rendered_in`
  attribution, correct `voyage_models_observed`.
- `convert` produces 18 runnable copies; every converted file passes
  `ast.parse`; the Anthropic qualifier (`anthropic.Anthropic`) is preserved;
  Voyage model ids are untouched.
- `run` end-to-end for the offline `tokenize`/`count_tokens` examples produces
  stdout identical to the documented output (`32`, and the exact token lists in
  `tutorials/tokenization.txt`), written to the mirrored paths.

Remaining (requires the keys): a full `run`/`all` with `GROVE_API_KEY` +
`VOYAGE_API_KEY` set to confirm the Grove and Voyage calls succeed and every
non-asset example exits 0. The interactive `rag_mongodb_*` app variants also
need a reachable `MONGODB_URI`.

## 9. Known quirks / watch-outs

- **Inline-block slugs**: `discover_outputs` and `extract_inline_python` both
  derive `block-N` slugs by counting inline output nodes per page in document
  order — they must stay in sync. New inline blocks in `step-run-app.rst` or
  `run-pipeline-instructions-in-memory.rst` (which carry no heading) will
  renumber to `block-1`, `block-2`, … — remember to update the `CURATED` keys.
- **Anthropic `base_url`**: no `/v1`; OpenAI `base_url`: with `/v1`. This is
  based on the SDK `/v1` append behavior and internal Grove examples.
- **`--python` is deliberately `abspath`'d, not `resolve`'d** — resolving would
  flatten the venv symlink and lose site-packages.
- **Large-corpus example** downloads the `mteb/legalbench_consumer_contracts_qa`
  dataset on first run and embeds 154 docs; it is slow and costs Voyage API
  calls. Budget time/cost accordingly (per-example timeout default 1800 s).
- **`count_usage` / multimodal** need the image assets and a Voyage key.
- The tool never edits the docs repo. Everything it produces lives under
  `converted/` and `outputs/` (and `inventory.yaml`) in *this* repo.
