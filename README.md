# update-voyage-output

Tooling to regenerate the **documented output** of the Voyage AI docs
(`content/voyageai` in `docs-mongodb-internal`) after a major Voyage AI model
release, routing LLM API calls through MongoDB's **Grove** gateway.

> Deep rationale lives in [`DESIGN.md`](DESIGN.md) and `CLAUDE.md`. Read them
> before changing behavior.

## TL;DR

```bash
# 1. one-time setup
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -r requirements.txt

# 2. refresh the inventory after the docs PR updates model ids / outputs
#    (this also re-scans what the examples import, patches requirements.txt,
#    and installs the delta with `uv pip` — skip that with --no-sync)
.venv/bin/python update_voyage_output.py inventory

# 3. convert examples to Grove-compatible + run them, writing output mirrors
export GROVE_API_KEY=...       # required
export VOYAGE_API_KEY=...      # required
export MONGODB_URI=...         # for the two RAG-with-MongoDB app variants
.venv/bin/python update_voyage_output.py all --timeout 3600
```

`requirements.txt` is kept in sync automatically: `inventory` scans what the
examples actually `import`, diffs it against the file, rewrites it, and runs
`uv pip install --python .venv/bin/python -r requirements.txt`. Since docs
examples gain/hand-off packages release over release, that keeps the venv
ready without manual edits.

The location of the docs monorepo clone defaults to the path recorded in
`inventory.yaml`; to point at a different clone (e.g. on another machine), pass
its absolute path:

```bash
update_voyage_output.py inventory --docs-repo /Users/<you>/path/to/docs-mongodb-internal
# or export DOCS_REPO=/Users/<you>/path/to/docs-mongodb-internal
./run_release.sh
```

Resolution order: `--docs-repo` > `$DOCS_REPO` > the `docs_repo` recorded in
`inventory.yaml` > the sibling `../docs-mongodb-internal`. An explicitly given
bad path fails fast rather than falling back.

Outputs land under `outputs/<mirror of the monorepo path>`; converted examples
under `converted/<mirror>`. Diff `outputs/content/voyageai/source/...` against
the docs outputs and merge the fresh numbers into the docs PR.

## Help

```
.venv/bin/python update_voyage_output.py -h
.venv/bin/python update_voyage_output.py inventory -h
.venv/bin/python update_voyage_output.py convert -h
.venv/bin/python update_voyage_output.py run -h
```

## Layout

| Path               | What it is                                   |
|--------------------|----------------------------------------------|
| `update_voyage_output.py` | Single utility: `inventory` / `convert` / `run` / `all` |
| `inventory.yaml`   | Durable, curatable inventory (regenerated on release) |
| `converted/`       | Grove-compatible copies of the example scripts |
| `outputs/`         | stdout of each example, mirroring its monorepo path + `manifest.yaml` |
| `DESIGN.md`        | Design decisions and Grove conversion semantics |
| `CLAUDE.md`        | Orientation for future Claude sessions       |

## Env vars

Required (convert/run/all): `GROVE_API_KEY`, `VOYAGE_API_KEY`.
Optional: `GROVE_BASE_URL` (default `…/grove-foundry-prod`), `GROVE_MODEL`
(pin the LLM model routed through Grove; when unset the run stage auto-falls
back to a comparable model when Grove 404s on the doc's literal model id — see
`grove.model_fallbacks` in `inventory.yaml`, and `update_voyage_output.py models`
to list what your Grove deployment exposes).
