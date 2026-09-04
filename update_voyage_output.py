#!/usr/bin/env python3
# type: ignore
"""
update_voyage_output.py
=======================

Regenerate the documented example **outputs** under ``content/voyageai`` in the
MongoDB docs monorepo after every major Voyage AI model release, routing LLM
API calls through MongoDB's Grove gateway.

Everything below is implemented as one script with opt-in subcommands:

  python update_voyage_output.py inventory   Scan content/voyageai for documented
                                             outputs and write inventory.yaml.
  python update_voyage_output.py convert     Produce Grove-compatible copies of each
                                             example script (converted/<mirror>).
  python update_voyage_output.py run         Run the converted examples and write their
                                             stdout to outputs/<mirror of monorepo path>.
  python update_voyage_output.py all         inventory -> convert -> run (one release pass).

A model release typically looks like:

  1. A docs PR lands that bumps the Voyage model ids inside the example scripts
     (e.g. voyage-4-large -> voyage-5-large) and re-phrases the documented output
     text accordingly.
  2. Run  update_voyage_output.py inventory  to refresh inventory.yaml so it reflects
     what the docs now contain. Hand-edit inventory.yaml only when the scan needs
     help (brand-new output files, renamed example scripts).
  3. Run  update_voyage_output.py convert  to rebuild the Grove-compatible example
     tree under converted/.
  4. Run  update_voyage_output.py run  to execute each example and write its stdout
     under outputs/ mirroring the monorepo path. Merge those back into the docs PR
     as the new documented outputs.

Environment (required for convert / run / all):

  GROVE_API_KEY    Grove gateway key. It replaces ANTHROPIC_API_KEY /
                   OPENAI_API_KEY anywhere the converted examples make LLM calls.
  VOYAGE_API_KEY   Voyage API key used by the embedding / rerank / tokenizer calls.

Optional:

  GROVE_BASE_URL   Default https://grove-gateway-prod.azure-api.net/grove-foundry-prod
                   Override for other environments.
  GROVE_MODEL      Override the LLM model id routed through Grove. When unset the
                   converted scripts keep the model id already in the docs example
                   (e.g. claude-sonnet-4-5-20250929) and route it via Grove.

Design details and known quirks are recorded in DESIGN.md next to this file.
"""

from __future__ import annotations

import argparse
import ast
import datetime as _dt
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterator, Optional

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

GROVE_DEFAULT_BASE_URL = "https://grove-gateway-prod.azure-api.net/grove-foundry-prod"
GROVE_MODEL_ENV = "GROVE_MODEL"

# --------------------------------------------------------------------------- #
# Grove model fallbacks
# --------------------------------------------------------------------------- #
# Grove only deploys the model ids your entitlement provisions. A docs example
# pins a literal model id (e.g. claude-sonnet-4-5-20250929) which may not be
# deployed. When the run stage sees Grove 404 with "deployment does not exist"
# it retries with a comparable model, in this order:
#
#   $GROVE_MODEL (user pin, wins) >
#   ex["model"] (the doc's literal id) >
#   GROVE_MODEL_FALLBACKS[ex["model"]] (exact-id overrides, configurable in
#       inventory.yaml under grove.model_fallbacks) >
#   provider-level defaults (ANTHROPIC/OPENAI_FALLBACK_MODELS).
#
# Tune these to your Grove catalog (see the `models` subcommand / Grove UI).

ANTHROPIC_FALLBACK_MODELS = ["claude-sonnet-4-6", "claude-sonnet-4-5"]
OPENAI_FALLBACK_MODELS = ["gpt-5.5", "gpt-4.1"]

GROVE_MODEL_FALLBACKS: dict[str, list[str]] = {
    # doc id shown today in the voyageai examples -> comparable, likely-deployed ids
    "claude-sonnet-4-5-20250929": ["claude-sonnet-4-6"],
}

# --------------------------------------------------------------------------- #
# Dependency detection / requirements.txt sync
# --------------------------------------------------------------------------- #
# Example dependencies are not static — a model release can add a package (e.g.
# `datasets` for the large-corpus example). `inventory` therefore scans what the
# examples actually import, diffs it against requirements.txt, rewrites the file,
# and installs the delta with `uv pip install`.

# The tool itself always needs these, regardless of what the examples import.
TOOL_DEPS = ["pyyaml"]

# Packages the examples depend on at runtime WITHOUT importing directly (e.g.
# langchain's PyPDFLoader loads PDFs via `pypdf`). Keep this list curated; an
# import-only scan cannot discover these.
EXTRA_RUNTIME_DEPS = ["pypdf"]

# import module name -> pip package name where they differ (default: hyphenate).
MODULE_TO_PIP = {
    "yaml": "pyyaml",
    "PIL": "Pillow",
    "dotenv": "python-dotenv",
    "cv2": "opencv-python",
    "sklearn": "scikit-learn",
}

_STDLIB_MODULES = frozenset(getattr(sys, "stdlib_module_names", ()))
_REQ_NAME_RE = re.compile(r"^([A-Za-z0-9_.\-]+)")

# Scope: only this subtree of the docs repo is inventoried.
DOCS_SCOPE = "content/voyageai"

# Documented outputs are .out files under source/includes/ (namely example-code-output/),
# pulled into pages via a  .. output:: /includes/example-code-output/<name>.out  directive.
OUTPUT_FILE_SUFFIX = ".out"

# Voyage model ids / rerank ids appearing in example source. Anything matching is a
# Voyage API call target and must NOT be redirected to Grove.
VOYAGE_MODEL_RE = re.compile(r"(?:voyage|rerank)-[a-zA-Z0-9.\-]+")

# Documented output blocks are referenced with a custom  .. output::  directive.
RST_DIRECTIVE_RE = re.compile(r"^(\s*)\.\.\s+([a-zA-Z0-9_-]+)::\s*(.*)$")
RST_OPTION_RE = re.compile(r"^\s*:([a-zA-Z0-9_-]+):\s*(.*)$")
UNDERLINE_RE = re.compile(r"^[=\-~^+*'#.]+$")

CONVERTED_BANNER = "# Grove-compatible — regenerated by update_voyage_output.py convert; do not edit."

PRELUDE = ("GROVE_BASE_URL = os.environ.get(\"GROVE_BASE_URL\", "
           f"{GROVE_DEFAULT_BASE_URL!r})\n"
           "GROVE_API_KEY = os.environ.get(\"GROVE_API_KEY\")\n"
           "if not GROVE_API_KEY:\n"
           "    raise SystemExit(\n"
           "        \"GROVE_API_KEY is required for the Grove-compatible run of this example. \"\n"
           "        \"Set GROVE_API_KEY and VOYAGE_API_KEY, then re-run.\"\n"
           "    )\n")


# --------------------------------------------------------------------------- #
# Small utilities
# --------------------------------------------------------------------------- #

def log(msg: str, verbose: bool = False) -> None:
    if verbose:
        print(f"[update_voyage_output] {msg}")


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def slugify(text: str) -> str:
    text = re.sub(r"`+", "", text)
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return text or "block"


def resolve_include_argument(arg: str, scope_root: str) -> str:
    """Map a  .. output:: /includes/example-code-output/foo.out  argument to a
    docs-repo-relative path."""
    if arg.startswith("/"):
        return f"{scope_root}/source{arg}"
    return arg


# --------------------------------------------------------------------------- #
# RST parsing (docs pages use a custom  .. output::  directive)
# --------------------------------------------------------------------------- #

def build_directive_tree(lines: list[str]) -> list[dict[str, Any]]:
    """Turn RST lines into a tree of directive nodes.

    Node: {name, arg, line, indent, content: [raw_line, ...] | None, body: [node, ...]}
    ``content`` holds the raw indented text/option lines under a directive,
    including blank lines, so code/output bodies can be dedented faithfully.
    """
    root: list[dict[str, Any]] = []
    stack: list[tuple[int, dict[str, Any]]] = []  # (indent, node)

    for lineno, raw in enumerate(lines, start=1):
        line = raw.rstrip("\n")
        m = RST_DIRECTIVE_RE.match(line)
        if m:
            indent = len(m.group(1))
            node = {"name": m.group(2), "arg": m.group(3).strip(),
                    "line": lineno, "indent": indent, "content": None, "body": []}
            while stack and stack[-1][0] >= indent:
                stack.pop()
            if stack:
                stack[-1][1]["body"].append(node)
            else:
                root.append(node)
            stack.append((indent, node))
            continue

        if not line.strip():
            # Blank line: keep it attached to the innermost active directive so
            # multi-paragraph output bodies survive. Top-level blanks are dropped.
            if stack:
                node = stack[-1][1]
                if node["content"] is None:
                    node["content"] = []
                node["content"].append("")
            continue

        indent = len(line) - len(line.lstrip(" \t"))
        while stack and stack[-1][0] >= indent:
            stack.pop()
        if stack:
            node = stack[-1][1]
            if node["content"] is None:
                node["content"] = []
            node["content"].append(line)

    return root


def _walk(nodes: list[dict[str, Any]], name: str) -> Iterator[dict[str, Any]]:
    for n in nodes:
        if n["name"] == name:
            yield n
        yield from _walk(n["body"], name)


def _parent_chain(nodes: list[dict[str, Any]], target: dict, chain: Optional[list] = None):
    for n in nodes:
        c = (chain or []) + [n]
        if n is target:
            return c
        found = _parent_chain(n["body"], target, c)
        if found is not None:
            return found
    return None


def nearest_heading(lines: list[str], before_line: int) -> Optional[str]:
    heading = None
    for i in range(before_line - 1):
        if i + 1 < len(lines):
            cur = lines[i].rstrip("\n")
            nxt = lines[i + 1].rstrip("\n")
            if cur.strip() and UNDERLINE_RE.match(nxt.strip()) and len(nxt.strip()) >= len(cur.strip()):
                heading = cur
    return heading


def extract_code(content: list[str]) -> str:
    """Drop RST option lines (:language: ...) and dedent the raw text lines."""
    body = [c for c in content if not RST_OPTION_RE.match(c)]
    non_empty = [c for c in body if c.strip()]
    if not non_empty:
        return ""
    base = min(len(c) - len(c.lstrip(" \t")) for c in non_empty)
    out = [c[base:] for c in body]
    return "\n".join(out).strip("\n")


def iter_output_nodes(lines: list[str]) -> Iterator[dict]:
    """Yield every  .. output::  node in a page, in document order."""
    tree = build_directive_tree(lines)
    yield from _walk(tree, "output")


def iter_inline_outputs(lines: list[str]) -> Iterator[tuple[dict, str]]:
    """Yield (output-node, stable-slug) for every inline output block in a page.

    Slugs prefer the nearest preceding RST heading; pages without a heading get
    a positional ``block-N`` slug in document order. discover_outputs() and
    extract_inline_python() must agree on how N is counted — both use this.
    """
    n = 0
    for node in iter_output_nodes(lines):
        if node["arg"]:
            continue
        heading = nearest_heading(lines, node["line"])
        slug = slugify(heading) if heading else f"block-{n}"
        n += 1
        yield node, slug


def _input_python_for(node: dict, tree: list[dict]) -> str:
    """Return the python .. input:: code in the same io-code-block as ``node``.

    ``node`` must come from ``tree`` (identity matters for the parent lookup).
    """
    chain = _parent_chain(tree, node)
    if not chain or len(chain) < 2:
        return ""
    parent = chain[-2]
    if parent["name"] != "io-code-block":
        return ""
    for sib in parent["body"]:
        if sib["name"] != "input":
            continue
        lang = "text"
        for raw in (sib["content"] or []):
            om = RST_OPTION_RE.match(raw)
            if om and om.group(1) == "language":
                lang = om.group(2).strip()
        if lang == "python":
            return extract_code(sib["content"] or [])
    return ""


# --------------------------------------------------------------------------- #
# Output discovery
# --------------------------------------------------------------------------- #

class DetectedOutput:
    def __init__(self, key, kind, path_rel, line, slug=None, rendered_in=None,
                 inline_text=None, source_code=None):
        self.key = key
        self.kind = kind            # "file" | "inline"
        self.path_rel = path_rel    # docs-repo-relative path of the output location
        self.line = line
        self.slug = slug
        self.rendered_in = rendered_in or []
        self.inline_text = inline_text
        self.source_code = source_code


def discover_outputs(docs_repo: Path) -> tuple[list[DetectedOutput], list[str]]:
    """Scan content/voyageai -> (detected outputs, sorted voyage model ids)."""
    scope = docs_repo / DOCS_SCOPE
    if not scope.is_dir():
        sys.exit(f"--docs-repo does not contain {DOCS_SCOPE}: {docs_repo}")

    outputs: list[DetectedOutput] = []
    file_targets: set[str] = set()
    voyage_models: set[str] = set()

    # ---- pass 1: .. output:: directives in pages ----
    for page in sorted(scope.rglob("*")):
        if not page.is_file() or page.suffix not in (".txt", ".rst"):
            continue
        rel = page.relative_to(docs_repo).as_posix()
        lines = page.read_text(encoding="utf-8", errors="replace").splitlines()

        tree = build_directive_tree(lines)
        inline_n = 0
        for node in _walk(tree, "output"):
            arg = node["arg"]
            if arg:
                # File-backed:  .. output:: /includes/example-code-output/main.out
                target = resolve_include_argument(arg, DOCS_SCOPE)
                file_targets.add(target)
                if target in {o.key for o in outputs}:
                    for o in outputs:
                        if o.key == target and rel not in o.rendered_in:
                            o.rendered_in.append(rel)
                    continue
                outputs.append(DetectedOutput(key=target, kind="file",
                                              path_rel=target, line=node["line"],
                                              rendered_in=[rel]))
                continue

            # Inline:  .. output::  with body content.
            heading = nearest_heading(lines, node["line"])
            slug = slugify(heading) if heading else f"block-{inline_n}"
            inline_n += 1
            key = f"{rel}#{slug}"
            outputs.append(DetectedOutput(
                key=key, kind="inline", path_rel=rel, line=node["line"], slug=slug,
                inline_text="\n".join(node["content"] or []).strip("\n"),
                source_code=_input_python_for(node, tree),
                rendered_in=[rel],
            ))

    # ---- pass 2: standalone output .out files not referenced by a directive ----
    for f in sorted((scope / "source" / "includes").rglob("*")):
        if not f.is_file() or f.suffix != OUTPUT_FILE_SUFFIX:
            continue
        rel = f.relative_to(docs_repo).as_posix()
        if rel not in file_targets and rel not in {o.key for o in outputs}:
            outputs.append(DetectedOutput(
                key=rel, kind="file", path_rel=rel, line=0,
                rendered_in=["(not referenced by an output:: directive yet)"],
            ))

    # ---- voyage model ids observed in example sources ----
    for src in (scope / "source").rglob("*.py"):
        voyage_models.update(VOYAGE_MODEL_RE.findall(
            src.read_text(encoding="utf-8", errors="replace")))

    return outputs, sorted(voyage_models)


# --------------------------------------------------------------------------- #
# Output -> example derivation (source named after the .out file)
# --------------------------------------------------------------------------- #
# Writer's contract, by design: a documented output's ``.out`` file names its
# source script — ``example-code-output/semantic_search_basic.out`` is produced
# by ``semantic_search_basic.py`` somewhere under ``content/voyageai/source``.
# Nothing is curated: ``derive_derived_from`` derives ``derived_from[].source``
# from the .out stem, and when there is no *unique* ``<stem>.py`` it returns an
# error the caller surfaces — the writer fixes the file names, the tool does not
# guess. Metadata you can read off the source (LLM provider/model, env vars,
# image assets) is derived from the source file itself.

# Anthropic/OpenAI model literals in example source -> Grove conversion + run fallback.
LLM_MODEL_RE = re.compile(r"(?:claude|gpt)[-A-Za-z0-9.]+")
_ENV_REF_RE = re.compile(
    r'os\.(?:getenv|environ\.get)\(\s*["\']([A-Z][A-Z0-9_]*)["\']'
    r"|os\.environ\[['\"]([A-Z][A-Z0-9_]*)['\"]\]"
)
_ASSET_RE = re.compile(r"['\"]([A-Za-z0-9_.\-]+\.(?:jpg|jpeg|png|gif|webp))['\"]")
# Env vars the pipeline manages itself — never reported back to the writer.
_TOOL_ENV = {"GROVE_API_KEY", "GROVE_BASE_URL", "GROVE_MODEL",
             "VOYAGE_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"}

# Human-readable titles per .out stem (cosmetic; default falls back to a generic).
OUTPUT_TITLES = {
    "generate-embeddings": "Quick start — Generate Your First Embeddings output",
    "rag-application": "Quick start RAG application output",
    "in-memory-rag": "RAG in-memory — run-app output",
    "main": "RAG with MongoDB — full pipeline output",
    "semantic_search_basic": "Semantic search — basic dot-product example",
    "semantic_search_reranker": "Semantic search — reranker example",
    "semantic_search_multilingual": "Semantic search — multilingual example",
    "semantic_search_multimodal": "Semantic search — multimodal example",
    "semantic_search_contextualized": "Semantic search — contextualized embeddings example",
    "semantic_search_large_corpus": "Semantic search — large corpus (mteb) example",
    "tokenization-tokenize": "Tokenization — tokenize method output",
    "tokenization-count-tokens": "Tokenization — count_tokens method output",
    "tokenization-count-usage": "Tokenization — count_usage method output",
}


def derive_derived_from(docs_repo: Path, det) -> tuple[list[dict], Optional[str]]:
    """Map a file-backed ``.. output::`` output to its example script.

    ``<name>.out`` must be produced by ``<name>.py``. Returns
    ``(derived_from, error_or_None)``: a unique stem match yields one script
    entry with metadata read off the source; zero or several matches yield
    ``([], error)`` so the caller alerts the writer instead of inferring.
    """
    stem = Path(det.path_rel).stem
    matches = sorted(
        f.relative_to(docs_repo).as_posix()
        for f in (docs_repo / DOCS_SCOPE / "source").rglob("*.py")
        if f.stem == stem
    )
    if not matches:
        return [], (f"no source script {stem!r}.py under {DOCS_SCOPE}/source — "
                    "name the .out file after its source (minus the extension)")
    if len(matches) > 1:
        return [], (f"{len(matches)} source scripts stem {stem!r}: "
                    + ", ".join(matches) + " — rename them so exactly one matches "
                    f"{stem}.out")
    rel = matches[0]
    text = (docs_repo / rel).read_text(encoding="utf-8", errors="replace")
    llm_models = list(dict.fromkeys(LLM_MODEL_RE.findall(text)))
    providers = [p for p in ("anthropic", "openai")
                 if re.search(rf"(?im)^\s*(?:import {p}\b|from {p}\b)", text)]
    env_vars = sorted({(a or b) for a, b in _ENV_REF_RE.findall(text)} - _TOOL_ENV)
    assets = sorted(set(_ASSET_RE.findall(text)))
    return [{
        "name": stem,
        "kind": "script",
        "source": rel,
        "files": [],
        "entrypoint": None,
        "needs_grove": bool(llm_models),
        "providers": providers,
        "env_vars": env_vars,
        "assets": assets,
        "llm_models": llm_models,
        "model": llm_models[0] if llm_models else "",
        "stdin": [],
    }], None


# --------------------------------------------------------------------------- #
# Inventory
# --------------------------------------------------------------------------- #

def stable_output_id(det: DetectedOutput) -> str:
    """Deterministic, order-independent id so releases don't shuffle/invalidate ids."""
    if det.kind == "file":
        p = Path(det.path_rel)
        return f"file-{p.parent.name}-{p.stem}"
    return f"inline-{Path(det.path_rel).stem}-{det.slug}"


def _example_mirror(output: dict, ex: dict, ex_idx: int) -> str:
    """Repo-relative mirror path (interpreted under the outputs/ root).

    Every documented output is one file-backed ``.out`` under
    ``source/includes/example-code-output/``. The strict derivation yields at
    most one example per output, so its mirror is simply the output's own
    ``location.path`` — stdout lands exactly on the ``.out`` the docs reference.
    (If a hand-written interpretation ever lists several ``derived_from``
    entries, they still share the one ``.out``; the last run wins on disk and
    ``manifest.yaml`` records each run/model.)
    """
    p = Path(output["location"]["path"])
    if output["location"]["kind"] == "inline":
        # defensive only — the docs have no inline outputs since the .out refactor
        return (p.parent / p.stem / (ex["name"] + ".txt")).as_posix()
    return output["location"]["path"]


def build_inventory(docs_repo: Path, existing: Optional[dict]) -> dict:
    outputs, voyage_models = discover_outputs(docs_repo)

    inv: dict[str, Any] = {
        "generated_at": now_iso(),
        "docs_repo": str(docs_repo),
        "scope": DOCS_SCOPE,
        "grove": {
            "base_url": GROVE_DEFAULT_BASE_URL,
            "model_env": GROVE_MODEL_ENV,
            "model_fallbacks": dict(GROVE_MODEL_FALLBACKS),
        },
        "voyage_models_observed": voyage_models,
        "outputs": [],
        "stale_output_ids": [],
    }
    existing_by_id = {o.get("id"): o for o in (existing or {}).get("outputs", [])}

    for det in outputs:
        stem = Path(det.path_rel).stem if det.kind == "file" else (det.slug or "block")
        entry = {
            "id": stable_output_id(det),
            "title": OUTPUT_TITLES.get(stem, f"Output generated by {stem}"),
            "location": {"kind": det.kind, "path": det.path_rel, "line": det.line},
            "rendered_in": det.rendered_in,
            "derived_from": [],
        }
        if det.kind == "inline":
            entry["location"]["block"] = det.slug
            entry["match_error"] = ("inline .. output:: blocks are not supported — "
                                    "move them to example-code-output/<stem>.out")
        else:
            derived, err = derive_derived_from(docs_repo, det)
            entry["derived_from"] = derived
            if err:
                entry["match_error"] = err

        # Only free-form writer notes survive a regeneration; derived_from is
        # recomputed from the docs each time (never carried over from a prior run).
        prev = existing_by_id.get(entry["id"])
        if prev and prev.get("notes"):
            entry["notes"] = prev["notes"]

        for ex_idx, ex in enumerate(entry["derived_from"]):
            ex["mirror_target"] = _example_mirror(entry, ex, ex_idx)

        inv["outputs"].append(entry)

    # Outputs that were in the previous inventory but are no longer detected.
    if existing:
        for o in existing.get("outputs", []):
            if o.get("id") not in {e["id"] for e in inv["outputs"]}:
                inv["stale_output_ids"].append(o.get("id"))
    return inv


def write_yaml(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump(obj, sort_keys=False, default_flow_style=False, allow_unicode=True)
    header = (
        "# Documented Voyage AI example outputs under content/voyageai.\n"
        "# Generated by update_voyage_output.py inventory. On a model release:\n"
        "#   1. update the example scripts / .out names in the docs repo. A .out\n"
        "#      file must be named after its source script, minus the extension\n"
        "#      (<name>.out <- <name>.py).\n"
        "#   2. re-run this stage. Outputs whose .out stem has no *unique* source\n"
        "#      are flagged with a `match_error` below — the tool alerts, it never\n"
        "#      guesses the source.\n"
        "#   3. run convert then run.\n"
        "#\n"
    )
    path.write_text(header + body, encoding="utf-8")


# --------------------------------------------------------------------------- #
# Dependency sync (requirements.txt)
# --------------------------------------------------------------------------- #

def _module_to_pip(module: str) -> str:
    if module in _STDLIB_MODULES:
        return ""
    return MODULE_TO_PIP.get(module, module.replace("_", "-"))


def _import_modules(text: str, local_names: set[str]) -> set[str]:
    mods: set[str] = set()
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return mods
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                mods.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            mods.add(node.module.split(".")[0])
    return {m for m in mods if m not in local_names and _module_to_pip(m)}


def detect_requirements(docs_repo: Path, inv: dict) -> list[str]:
    """Sorted union of pip deps the tool + every example under the scope need.

    Scans the whole example tree (all ``.py`` under ``content/voyageai/source``),
    not just the outputs currently derived — so a temporarily-flagged output
    (``match_error``) never causes a package the venv still needs to be dropped
    from requirements.txt. Imports that resolve to an in-tree ``.py`` (local
    modules like ``config.py`` / ``main.py``'s siblings) are never reported as
    third-party deps; only modules with no in-tree ``.py`` become pip names.
    Derived inline blocks are scanned too.
    """
    deps: set[str] = set(TOOL_DEPS) | set(EXTRA_RUNTIME_DEPS)
    root = docs_repo / DOCS_SCOPE / "source"
    local = {f.stem for f in root.rglob("*.py")}
    for f in root.rglob("*.py"):
        deps |= {_module_to_pip(m) for m in _import_modules(
            f.read_text(encoding="utf-8", errors="replace"), local)}
    for out in inv.get("outputs", []):
        for ex in out.get("derived_from", []):
            if ex.get("kind") == "block":
                text = extract_inline_python(docs_repo / ex["source"], ex.get("block", ""))
                deps |= {_module_to_pip(m) for m in _import_modules(text, local)}
    return sorted(deps)


def parse_requirements(path: Path) -> list[tuple[str, str]]:
    """[(full_line, normalized_base_name)] for non-comment lines."""
    if not path.is_file():
        return []
    out = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _REQ_NAME_RE.match(line)
        out.append((line, (m.group(1) if m else line).lower()))
    return out


def sync_requirements(docs_repo: Path, inv: dict, req_path: Path, *,
                      install: bool, python: Path, verbose: bool) -> bool:
    """Rewrite requirements.txt to match what the examples need, then (unless
    install=False) apply it with `uv pip install`. Returns success."""
    desired = detect_requirements(docs_repo, inv)
    entries = parse_requirements(req_path)
    existing = {name: line for line, name in entries}
    desired_lower = {d.lower() for d in desired}

    added = sorted(d for d in desired if d.lower() not in existing)
    removed = sorted(line for line, name in entries if name not in desired_lower)

    changed = bool(added or removed)
    if changed:
        kept = [line for line, name in entries if name in desired_lower]
        body = "\n".join(kept + added)
        req_path.write_text(
            "# Runtime dependencies for update_voyage_output.py — kept in sync "
            "automatically\n"
            "# by `update_voyage_output.py inventory` (edit example code, not "
            "this file).\n"
            "# Install: uv venv --python 3.13 .venv && "
            "uv pip install --python .venv/bin/python -r requirements.txt\n"
            "#\n" + body + "\n",
            encoding="utf-8")
        print(f"  requirements.txt: +{len(added)} -{len(removed)} "
              f"({' '.join(added) or '-'}/{','.join(removed) or '-'})")

    if not install:
        return True
    if not changed:
        print("  requirements.txt: up to date (nothing to install)")
        return True

    uv = shutil.which("uv")
    if not uv:
        print("  !! `uv` not found on PATH — wrote requirements.txt but did not "
              "install; run `uv pip install --python .venv/bin/python -r "
              f"{req_path}`", file=sys.stderr)
        return False
    cmd = [uv, "pip", "install", "--python", str(python),
           "-r", str(req_path), "--quiet"]
    if verbose:
        print("  $ " + " ".join(cmd))
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        print("  !! uv pip install failed:\n" + (r.stderr or "")[-3000:], file=sys.stderr)
        return False
    print("  requirements.txt: updated and installed via uv pip")
    return True


# --------------------------------------------------------------------------- #
# Grove conversion
# --------------------------------------------------------------------------- #

def balance_paren(text: str, open_idx: int) -> int:
    depth = 0
    i = open_idx
    while i < len(text):
        c = text[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def rewrite_constructor_calls(text: str, finders: list[tuple[str, str]]) -> str:
    """Rewrite LLM client constructor calls to point at Grove.

    ``finders`` = [(name_pattern, grove_args_expr), ...]. A call
    ``<pattern>(original_args)`` is rewritten to ``<pattern>`` + ``grove_args``,
    preserving the exact matched name (so ``import anthropic`` + ``anthropic.Anthropic``
    keeps the ``anthropic.`` qualifier, and ``from anthropic import Anthropic`` keeps
    the bare ``Anthropic``). Only calls whose argument list contains ``api_key=`` or
    that are empty are rewritten. Longer patterns (anthropic.Anthropic) must run before
    shorter ones (Anthropic) to avoid double-matching.
    """
    out = text
    for finder, grove_args in finders:
        src, buf, idx, n = out, [], 0, len(finder)
        while True:
            hit = src.find(finder, idx)
            if hit == -1:
                buf.append(src[idx:])
                break
            k = hit + n
            while k < len(src) and src[k] in " \t":
                k += 1
            if k < len(src) and src[k] == "(":
                end = balance_paren(src, k)
                if end != -1:
                    callargs = src[k + 1:end]
                    if "api_key" in callargs or not callargs.strip():
                        buf.append(src[idx:hit])
                        buf.append(finder + grove_args)
                        idx = end + 1
                        continue
            # not a rewritten constructor; skip past the matched name only
            buf.append(src[idx:k])
            idx = k
        out = "".join(buf)
    return out


def inject_prelude(text: str) -> str:
    """Ensure `import os`, then append CONVERTED_BANNER + Grove prelude after the
    last top-level import so the environment checks run before any API call."""
    if not re.search(r"(?m)^\s*import os\b", text) and not re.search(r"(?m)^\s*from os\b", text):
        m = list(re.finditer(r"(?m)^\s*(?:from \S+\s+)?import\b.*$", text))
        if m:
            idx = m[-1].end()
            text = text[:idx] + "\nimport os" + text[idx:]
        else:
            text = "import os\n" + text
    m = list(re.finditer(r"(?m)^\s*(?:from \S+\s+)?import\b.*$", text))
    idx = m[-1].end() if m else 0
    return text[:idx] + "\n" + CONVERTED_BANNER + "\n" + PRELUDE + "\n" + text[idx:]


def override_llm_models(text: str, models: list[str]) -> str:
    if not models:
        return text
    for m in sorted(set(models), key=len, reverse=True):
        mm = re.escape(m)
        # inline call argument:  model="claude-sonnet-4-5-20250929"
        text = text.replace(f'model="{m}"',
                            f'model=os.environ.get("GROVE_MODEL", "{m}")')
        # config-style assignment:  ANTHROPIC_MODEL = "claude-...", OPENAI_MODEL = "gpt-4o"
        text = re.sub(
            r"(?m)^(\s*[A-Za-z_]+MODEL\s*=\s*)\"(" + mm + r")\"\s*$",
            lambda mt: mt.group(1) + f"os.environ.get(\"GROVE_MODEL\", \"{m}\")",
            text,
        )
    return text


def to_grove_compatible(text: str, llm_models: list[str]) -> str:
    if "GROVE_API_KEY" in text and "GROVE_BASE_URL" in text:
        return text  # idempotent
    anthropic_args = ('(base_url=f"{GROVE_BASE_URL}/anthropic", '
                      'api_key="unused", default_headers={"api-key": GROVE_API_KEY})')
    openai_args = ('(base_url=f"{GROVE_BASE_URL}/openai/v1", '
                   'api_key="unused", default_headers={"api-key": GROVE_API_KEY})')
    text = rewrite_constructor_calls(text, [
        ("anthropic.Anthropic", anthropic_args),
        ("Anthropic", anthropic_args),
        ("OpenAI", openai_args),
    ])
    text = override_llm_models(text, llm_models)
    return inject_prelude(text)


def file_uses_llm(text: str) -> bool:
    return bool(re.search(r"(?m)^\s*(import anthropic\b|from anthropic\b|"
                          r"import openai\b|from openai\b)", text))


# --------------------------------------------------------------------------- #
# Convert stage
# --------------------------------------------------------------------------- #

def extract_inline_python(page: Path, slug: str) -> str:
    """Return the python from the ``.. input::`` that shares an io-code-block
    with the ``.. output::`` node whose heading slug is ``slug``.

    Output nodes may be inline (``.. output::`` with a body) or file-backed
    (``.. output:: /includes/example-code-output/tokenization-tokenize.out``) —
    both live next to the ``.. input::`` python we want (the tokenize /
    count_tokens / count_usage blocks in ``tutorials/tokenization.txt``).
    """
    lines = page.read_text(encoding="utf-8", errors="replace").splitlines()
    tree = build_directive_tree(lines)
    n = 0
    for node in _walk(tree, "output"):
        heading = nearest_heading(lines, node["line"])
        if node["arg"]:
            s = slugify(heading) if heading else ""
        else:
            s = slugify(heading) if heading else f"block-{n}"
            n += 1
        if s and s == slug:
            return _input_python_for(node, tree) or extract_code(node["content"] or [])
    return ""


def convert_one(docs_repo: Path, ex: dict, converted_root: Path) -> Optional[Path]:
    kind = ex.get("kind")
    use_grove = bool(ex.get("llm_models"))

    if kind == "script":
        src = docs_repo / ex["source"]
        dst = converted_root / ex["source"]
        if not src.is_file():
            log(f"  !! missing script source: {ex['source']}")
            return None
        dst.parent.mkdir(parents=True, exist_ok=True)
        text = src.read_text(encoding="utf-8")
        if use_grove or file_uses_llm(text):
            text = to_grove_compatible(text, ex.get("llm_models", []))
        dst.write_text(text, encoding="utf-8")
        return dst

    if kind == "block":
        page = docs_repo / ex["source"]
        head = Path(ex["source"]).parent / Path(ex["source"]).stem
        dst = converted_root / (head.as_posix() + "/" + ex.get("block", "code") + ".py")
        text = extract_inline_python(page, ex.get("block", ""))
        if not text:
            log(f"  !! could not extract inline block '{ex.get('block')}' from {page}")
            return None
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(text, encoding="utf-8")
        return dst

    if kind == "app":
        target_dir = converted_root / ex["source"] / ex["name"]
        target_dir.mkdir(parents=True, exist_ok=True)
        entry_abs = None
        for f in ex.get("files", []):
            src = docs_repo / f
            if not src.is_file():
                log(f"  !! missing app file: {f}")
                continue
            text = src.read_text(encoding="utf-8")
            if use_grove or file_uses_llm(text):
                text = to_grove_compatible(text, ex.get("llm_models", []))
            (target_dir / src.name).write_text(text, encoding="utf-8")
            if src.name == ex.get("entrypoint"):
                entry_abs = target_dir / src.name
        return entry_abs

    return None


def cmd_convert(args) -> int:
    docs_repo = Path(args.docs_repo).resolve()
    converted_root = Path(args.converted_root).resolve()
    converted_root.mkdir(parents=True, exist_ok=True)
    n = 0
    for output in args.inventory["outputs"]:
        if output.get("match_error"):
            print(f"! {output['id']}: {output['match_error']}  (skipping)")
        for ex in output.get("derived_from", []):
            ok = convert_one(docs_repo, ex, converted_root)
            if ok:
                n += 1
                rel = ok.relative_to(converted_root)
                print(f"✓ {output['id']}  {ex.get('name'):<24} -> {rel}")
                if args.verbose and ok.suffix == ".py":
                    try:
                        ast.parse(ok.read_text(encoding="utf-8"))
                    except SyntaxError as e:
                        print(f"    !! syntax error in {rel}: {e}")
            else:
                print(f"✗ {output['id']}  {ex.get('name')}  (skipped — missing source)")
    (converted_root / "GROVE_NOTES.md").write_text(
        "# Grove-compatible copies of the Voyage AI example scripts.\n"
        "# Regenerated by `update_voyage_output.py convert`. Do not hand-edit.\n"
        f"# GROVE_BASE_URL default: {GROVE_DEFAULT_BASE_URL}\n"
        f"# Override the LLM model at run time with the {GROVE_MODEL_ENV} env var.\n",
        encoding="utf-8")
    print(f"\nconverted {n} example(s) under {converted_root}")
    return 0 if n else 1


# --------------------------------------------------------------------------- #
# Run stage
# --------------------------------------------------------------------------- #

def _model_unavailable(err: str) -> bool:
    """True when a run failed because the requested model/deployment isn't
    available on this Grove (vs. an auth, timeout, or code error)."""
    t = (err or "").lower()
    return any(m in t for m in (
        "deploymentnotfound", "deployment not found", "does not exist",
        "model_not_found", "model not found", "no model available",
        "model is not supported", "not supported for this",
    ))


def _model_candidates(ex: dict, fallback_map: dict, pinned_model: Optional[str]) -> list[str]:
    """Ordered list of GROVE_MODEL ids to try for this example.

    - ``pinned_model`` (GROVE_MODEL env) wins: try only that.
    - Otherwise start with the doc's literal LLM model (ex["model"]), then the
      comparable fallbacks from the exact-id map, then provider-level defaults.
    """
    if pinned_model:
        return [pinned_model]
    model = ex.get("model") or ""
    if not model:
        return []
    fallbacks = list(fallback_map.get(model, []))
    if model.startswith("claude") or model.startswith("anthropic/"):
        for f in ANTHROPIC_FALLBACK_MODELS:
            if f not in fallbacks:
                fallbacks.append(f)
    elif model.startswith("gpt"):
        for f in OPENAI_FALLBACK_MODELS:
            if f not in fallbacks:
                fallbacks.append(f)
    out = [model]
    for f in fallbacks:
        if f not in out:
            out.append(f)
    return out


def _local_module_stems(docs_repo: Path, ex: dict) -> set[str]:
    """Stems of the local/sibling modules this example expects beside its
    source — the files a single-script copy into converted/ won't carry with it.
    (``main.py`` importing ``generate_response``/``config`` is the case in point.)"""
    names: set[str] = set()
    kind = ex.get("kind")
    if kind == "script":
        src = docs_repo / (ex.get("source") or "")
        if src.is_file():
            names |= {f.stem for f in src.parent.glob("*.py")}
    elif kind == "app":
        names |= {Path(f).stem for f in ex.get("files", [])}
    names.discard("__init__")
    return names


def _local_import_hits(err: str, local_stems: set[str]) -> list[str]:
    """Local sibling modules whose import broke the run (a relative/local import
    that has no file beside it in converted/), or a marker when a bare relative
    import failed with no parent package."""
    if "attempted relative import with no known parent package" in err.lower():
        return ["<relative import>"]
    hits: set[str] = set()
    for m in re.finditer(r"No module named '([^']+)'", err):
        name = m.group(1).split(".")[0]
        if name in local_stems:
            hits.add(name)
    for m in re.finditer(r"cannot import name '[^']+' from '([^']+)'", err):
        name = m.group(1).split(".")[0]
        if name in local_stems:
            hits.add(name)
    return sorted(hits)


def run_one(ex: dict, docs_repo: Path, converted_root: Path, venv_python: Path,
            assets_dir: Optional[Path], timeout: int, dry_run: bool, verbose: bool,
            fallback_map: Optional[dict] = None,
            pinned_model: Optional[str] = None) -> tuple[int, str, str, str, str]:
    """Run a single example, retrying with a comparable Grove model when the
    documented model id isn't deployed.
    Returns (exit_code, stdout, logs, model_used, reason_for_summary)."""
    fallback_map = fallback_map or {}
    logs: list[str] = []
    kind = ex.get("kind")
    if kind == "script":
        cwd = (converted_root / ex["source"]).parent
        entry = (converted_root / ex["source"]).name
    elif kind == "app":
        cwd = converted_root / ex["source"] / ex["name"]
        entry = ex.get("entrypoint", "main.py")
    else:
        head = Path(ex["source"]).parent / Path(ex["source"]).stem
        cwd = converted_root / head.as_posix()
        entry = ex.get("block", "code") + ".py"

    # assets
    for a in ex.get("assets", []):
        target = cwd / a
        if target.is_file():
            continue
        if assets_dir and (assets_dir / a).is_file():
            shutil.copy2(assets_dir / a, target)
            continue
        logs.append(f"  asset missing: {a} (--assets-dir {assets_dir} or drop it into {cwd})")
        return 1, "", "\n".join(logs), "", f"missing asset: {a}"

    missing = [v for v in ex.get("env_vars", [])]
    missing = [v for v in missing if not os.environ.get(v)]
    if missing:
        logs.append(f"  missing env var(s): {', '.join(missing)} — set them or skip this variant")
        return 1, "", "\n".join(logs), "", f"missing env var(s): {', '.join(missing)}"

    cmd = [str(venv_python), entry]
    stdin_data = ("\n".join(ex.get("stdin", [])) + "\n").encode() if ex.get("stdin") else None
    logs.append(f"  cwd: {cwd}\n  cmd: {' '.join(cmd)}")
    if dry_run:
        if stdin_data:
            logs.append(f"  stdin: {stdin_data.decode().strip()}")
        return 0, "(dry-run)\n", "\n".join(logs), "", "(dry-run)"

    def execute(model: str) -> tuple[int, str, str]:
        env = dict(os.environ)
        if model:
            env["GROVE_MODEL"] = model
        try:
            proc = subprocess.run(cmd, cwd=str(cwd), env=env,
                                  input=stdin_data, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return 124, "", "timed out after {}s".format(timeout)
        return (proc.returncode,
                proc.stdout.decode("utf-8", errors="replace"),
                proc.stderr.decode("utf-8", errors="replace"))

    candidates = _model_candidates(ex, fallback_map, pinned_model)
    seq = candidates if candidates else [None]
    model_used, rc, out, err = "", 1, "", ""
    for i, model in enumerate(seq):
        rc, out, err = execute(model or "")
        model_used = model or ""
        if rc == 0 or not _model_unavailable(err) or i == len(seq) - 1:
            break
        nxt = seq[i + 1]
        logs.append(f"  model {model!r} unavailable on Grove → retrying with {nxt!r}")

    if rc != 0:
        logs.append(f"  exit={rc}" + (f" (model={model_used})" if model_used else ""))
        if err:
            logs.append("  stderr (tail): " + err[-2000:].strip())

    reason = ""
    if dry_run:
        reason = "(dry-run)"
    elif rc != 0:
        if rc == 124:
            reason = "timed out"
        elif _model_unavailable(err):
            reason = "model unavailable on Grove (tried: " + ", ".join(candidates) + ")"
        else:
            imports = _local_import_hits(err, _local_module_stems(docs_repo, ex))
            if imports:
                reason = "local/relative import unmet: " + ", ".join(imports)
            else:
                reason = f"error (exit {rc})"
    # Stamp the log line with the classified reason so it's visible inline too.
    if rc != 0 and reason and reason != f"error (exit {rc})":
        logs.append(f"  ↳ {reason}")
    return rc, out, "\n".join(logs), model_used, reason


def build_run_summary(runs: list[dict], dry_run: bool,
                      missing_sources: Optional[list[dict]] = None) -> str:
    """Human-readable summary printed at the end of `run` — what succeeded/failed,
    which Grove model was used, which outputs had no source file, and how to fix
    the skips."""
    missing_sources = list(missing_sources or [])
    ok_n = sum(1 for r in runs if r.get("ok"))
    tot = len(runs)
    w = 78
    header = f"RUN SUMMARY — {ok_n}/{tot} examples clean" + (" (dry-run)" if dry_run else "")
    if missing_sources:
        header += f" — {len(missing_sources)} output(s) skipped (no source .py)"
    lines = ["=" * w, header]
    models: dict[str, int] = {}
    for r in runs:
        if r.get("model"):
            models[r["model"]] = models.get(r["model"], 0) + 1
    if models:
        lines.append("Grove models used: " + ", ".join(
            f"{m} ({n})" for m, n in sorted(models.items())))
    elif tot:
        lines.append("Grove models used: none (no LLM-backed examples ran)")

    failed = [r for r in runs if not r.get("ok")]
    if not failed:
        lines.append("All examples succeeded.")
    else:
        lines.append(f"Failed: {len(failed)}")
        grouped: dict[str, list[dict]] = {}
        for r in failed:
            grouped.setdefault(r.get("reason") or "unknown", []).append(r)
        for reason, rs in sorted(grouped.items(), key=lambda kv: -len(kv[1])):
            names = ", ".join(r.get("example", "?") for r in rs[:6])
            if len(rs) > 6:
                names += f" (+{len(rs) - 6} more)"
            lines.append(f"  · {reason}  [{len(rs)}]  {names}")
        hints = []
        if any("missing env var" in (r.get("reason") or "") for r in failed):
            hints.append("export the listed env var(s), e.g. MONGODB_URI")
        if any((r.get("reason") or "").startswith("missing asset") for r in failed):
            hints.append("provide the image(s) via --assets-dir <dir>")
        if any("model unavailable" in (r.get("reason") or "") for r in failed):
            hints.append("pin GROVE_MODEL (see the `models` subcommand) or edit "
                         "grove.model_fallbacks in inventory.yaml")
        if any("local/relative import unmet" in (r.get("reason") or "") for r in failed):
            hints.append("copy the example's sibling modules beside it in "
                         "converted/ (or make it self-contained) so its imports resolve")
        if hints:
            lines.append("To fix: " + " | ".join(hints))

    if missing_sources:
        lines.append("")
        lines.append("Outputs with no source file — the .out stem must match a "
                     "unique <stem>.py; rename source files in the docs PR:")
        for o in missing_sources:
            lines.append(f"  · {Path(o['location']['path']).name}: {o['match_error']}")
    lines.append("=" * w)
    return "\n".join(lines)


def cmd_run(args) -> int:
    # NOTE: abspath() but NOT symlink .resolve() — a venv python is usually a
    # symlink to the base interpreter; resolving it would drop the venv's
    # site-packages. abspath keeps the symlink while making it absolute (the
    # subprocess cwd is the converted dir, so a relative path would break).
    venv_python = Path(os.path.abspath(args.python))
    outputs_root = Path(args.outputs_root).resolve()
    converted_root = Path(args.converted_root).resolve()
    assets_dir = Path(args.assets_dir).resolve() if getattr(args, "assets_dir", None) else None

    # Grove model fallbacks: inventory.yaml overrides the tool defaults.
    fallback_map = dict(GROVE_MODEL_FALLBACKS)
    yaml_fallbacks = args.inventory.get("grove", {}).get("model_fallbacks")
    if isinstance(yaml_fallbacks, dict):
        fallback_map.update({k: list(v) for k, v in yaml_fallbacks.items() if isinstance(v, list)})
    pinned_model = os.environ.get(GROVE_MODEL_ENV) or None

    runs, failures, count = [], 0, 0
    seen_targets: dict[str, str] = {}
    for output in args.inventory["outputs"]:
        if output.get("match_error"):
            print(f"! {output['id']}: {output['match_error']}  (skipping)")
        for ex in output.get("derived_from", []):
            count += 1
            if args.limit and count > args.limit:
                return failures
            if args.exclude and (output["id"] in args.exclude or ex.get("name") in args.exclude):
                print(f"– {output['id']} / {ex.get('name')}  (excluded)")
                continue
            print(f"▶ {output['id']} / {ex.get('name')}  ({ex.get('kind')})")
            rc, stdout, logs, model_used, reason = run_one(
                ex, Path(args.docs_repo), converted_root, venv_python, assets_dir,
                args.timeout, args.dry_run, args.verbose,
                fallback_map, pinned_model)
            print(logs)
            if not args.dry_run and model_used:
                print(f"  (model: {model_used})")
            target = outputs_root / ex.get("mirror_target", output["id"] + ".txt")
            if not args.dry_run:
                target.parent.mkdir(parents=True, exist_ok=True)
                if str(target) in seen_targets:
                    print(f"  ! shared target — {target.name} previously held "
                          f"{seen_targets[str(target)]}; now holds {ex.get('name')} "
                          f"(manifest.yaml records both runs/models)")
                seen_targets[str(target)] = ex.get("name", "?")
                target.write_text(stdout, encoding="utf-8")
            runs.append({"output": output["id"], "example": ex.get("name"),
                         "ok": rc == 0, "exit": rc, "target": ex.get("mirror_target"),
                         "model": model_used or "", "reason": reason})
            if rc != 0:
                failures += 1

    if not args.dry_run:
        manifest = {"generated_at": now_iso(), "runs": runs}
        (outputs_root / "manifest.yaml").write_text(
            yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")

    missing = [o for o in args.inventory["outputs"] if o.get("match_error")]
    print("\n" + build_run_summary(runs, args.dry_run, missing_sources=missing))
    return failures


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def cmd_models(args) -> int:
    """List model ids this Grove deployment exposes (best effort) so you can pick
    comparable fallbacks for `grove.model_fallbacks` or set GROVE_MODEL."""
    base = os.environ.get("GROVE_BASE_URL", GROVE_DEFAULT_BASE_URL)
    key = os.environ.get("GROVE_API_KEY")
    if not key:
        print("GROVE_API_KEY is required to list models.", file=sys.stderr)
        return 1
    url = f"{base}/openai/v1/models"
    try:
        req = urllib.request.Request(url, headers={"api-key": key, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"could not list models from {url} (HTTP {e.code})\n"
              "Some Grove deployments do not expose /openai/v1/models; check the Grove "
              "dashboard instead (https://grove.aix.prod.corp.mongodb.com).", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"could not list models from {url}: {e}", file=sys.stderr)
        return 1

    ids = sorted({m.get("id") for m in data.get("data", []) if m.get("id")})
    if not ids:
        print("no models listed by this Grove deployment.", file=sys.stderr)
        return 1
    for mid in ids:
        print(mid)
    return 0


def require_keys(no_key_check: bool) -> None:
    if no_key_check:
        return
    missing = [k for k in ("GROVE_API_KEY", "VOYAGE_API_KEY") if not os.environ.get(k)]
    if missing:
        sys.exit(
            "Missing required environment variable(s): " + ", ".join(missing) + ".\n"
            "Set both GROVE_API_KEY and VOYAGE_API_KEY, or pass --no-key-check for a "
            "dry-run of the inventory/convert stages."
        )


def build_parser(default_root: Path) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="update_voyage_output.py",
        description="Inventory, convert (to Grove) and run the documented Voyage AI "
                    "example outputs in the docs monorepo.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="stages: inventory -> convert -> run  |  env: GROVE_API_KEY, VOYAGE_API_KEY",
    )
    sub = p.add_subparsers(dest="command", metavar="{inventory,convert,run,all,models}", required=True)

    def add_common(sp):
        sp.add_argument("--docs-repo", default=None, metavar="PATH",
                        help="absolute path to your clone of the docs monorepo "
                             "(docs-mongodb-internal). Resolution order: this flag, "
                             "then $DOCS_REPO, then the docs_repo recorded in "
                             "inventory.yaml, then the sibling ../docs-mongodb-internal")
        sp.add_argument("--root", default=str(default_root),
                        help=f"working root for generated files (default {default_root})")
        sp.add_argument("--inventory-path", default=None,
                        help="path to inventory.yaml (default <root>/inventory.yaml)")
        sp.add_argument("--converted-root", default=None,
                        help="grove-converted tree root (default <root>/converted)")
        sp.add_argument("--outputs-root", default=None,
                        help="run-output tree root (default <root>/outputs)")
        sp.add_argument("--requirements-path", default=None,
                        help="requirements.txt path (default <root>/requirements.txt)")
        sp.add_argument("--no-key-check", action="store_true",
                        help="do not require GROVE_API_KEY/VOYAGE_API_KEY (dry-run only)")
        sp.add_argument("--verbose", action="store_true")

    sp = sub.add_parser("inventory", help="scan content/voyageai and write inventory.yaml")
    add_common(sp)
    sp.add_argument("--force", action="store_true", help="ignore existing inventory.yaml")
    sp.add_argument("--no-sync", action="store_true",
                    help="skip dependency sync (requirements.txt + uv pip install)")

    sp = sub.add_parser("convert", help="build grove-compatible example copies")
    add_common(sp)
    sp.add_argument("--assets-dir", help="directory with asset files (images) for examples that need them")

    sp = sub.add_parser("models", help="list model ids this Grove deployment exposes (best effort)")
    add_common(sp)

    sp = sub.add_parser("run", help="run converted examples and mirror their output")
    add_common(sp)
    sp.add_argument("--python", default=None, help="python interpreter to run examples "
                                                   "(default: the interpreter running this script)")
    sp.add_argument("--timeout", type=int, default=1800, help="per-example timeout (s)")
    sp.add_argument("--dry-run", action="store_true", help="print commands without running")
    sp.add_argument("--limit", type=int, default=0, help="only process first N examples")
    sp.add_argument("--exclude", default=[], nargs="*", help="skip output ids or example names")
    sp.add_argument("--assets-dir", help="directory with asset files (images)")

    sp = sub.add_parser("all", help="inventory + convert + run (end-to-end release pass)")
    add_common(sp)
    sp.add_argument("--force", action="store_true")
    sp.add_argument("--no-sync", action="store_true",
                    help="skip dependency sync (requirements.txt + uv pip install)")
    sp.add_argument("--python", default=None)
    sp.add_argument("--timeout", type=int, default=1800)
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--limit", type=int, default=0)
    sp.add_argument("--exclude", default=[], nargs="*")
    sp.add_argument("--assets-dir", help="directory with asset files (images)")

    return p


def resolve_paths(args, root: Path) -> None:
    # Remember the raw CLI value (may be None) and the sibling default; the
    # final docs-repo is chosen later by resolve_docs_repo() once we may have
    # the inventory loaded.
    args.docs_repo_cli = args.docs_repo or None
    args.docs_repo_fallback = str((root.parent / "docs-mongodb-internal").resolve())
    args.inventory_path = getattr(args, "inventory_path", None) or str(root / "inventory.yaml")
    args.converted_root = getattr(args, "converted_root", None) or str(root / "converted")
    args.outputs_root = getattr(args, "outputs_root", None) or str(root / "outputs")
    args.requirements_path = getattr(args, "requirements_path", None) or str(root / "requirements.txt")
    if not getattr(args, "python", None):
        # Prefer the repo's venv when present (it has voyageai/anthropic/openai/
        # numpy/... installed); sys.executable is a reasonable fallback.
        venv_python = root / ".venv" / "bin" / "python"
        args.python = sys.executable if not _is_exec(venv_python) else str(venv_python)


def _is_exec(p: Path) -> bool:
    return p.is_file() or p.is_symlink()


def resolve_docs_repo(args, inventory: Optional[dict] = None) -> str:
    """Pick the docs monorepo path and fail fast with an actionable message.

    Priority: --docs-repo  >  $DOCS_REPO  >  inventory.yaml's docs_repo  >
    the sibling ../docs-mongodb-internal. The target must be a directory that
    actually contains content/voyageai.
    """
    candidates = [
        ("--docs-repo", getattr(args, "docs_repo_cli", None)),
        ("$DOCS_REPO", os.environ.get("DOCS_REPO")),
        ("inventory.yaml docs_repo", (inventory or {}).get("docs_repo")),
        ("sibling default", getattr(args, "docs_repo_fallback", "")),
    ]
    for source, val in candidates:
        if not val:
            continue
        p = Path(val).expanduser()
        if p.is_dir() and (p / "content" / "voyageai").is_dir():
            resolved = str(p.resolve())
            log(f"docs repo: {resolved} (via {source})", getattr(args, "verbose", False))
            return resolved
        # The first source that is actually given is authoritative: if an explicit
        # --docs-repo or $DOCS_REPO is wrong, fail loudly rather than silently
        # falling through to a different clone.
        sys.exit(
            f"docs repo from {source} is not a usable docs clone: {val}\n"
            "Expected a directory containing content/voyageai (e.g. a checkout of\n"
            "the docs monorepo). Fix --docs-repo (or DOCS_REPO) or point inventory.yaml's\n"
            "`docs_repo` at the correct clone."
        )
    sys.exit(
        "No docs monorepo path configured. Pass the absolute path, e.g.:\n"
        "  update_voyage_output.py <stage> --docs-repo /Users/<you>/path/to/docs-mongodb-internal\n"
        "or set the DOCS_REPO environment variable."
    )


def load_existing_inventory(path: Path, force: bool) -> Optional[dict]:
    if path.is_file() and not force:
        try:
            return yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            print(f"warning: could not read existing inventory ({e}); regenerating",
                  file=sys.stderr)
            return None
    return None


def main(argv: Optional[list[str]] = None) -> int:
    root = Path(__file__).resolve().parent
    parser = build_parser(root)
    args = parser.parse_args(argv)

    if yaml is None:
        print("PyYAML is required:  uv pip install pyyaml", file=sys.stderr)
        return 2

    resolve_paths(args, root)

    if args.command in ("convert", "run", "all"):
        require_keys(getattr(args, "no_key_check", False))

    if args.command == "models":
        return cmd_models(args)

    inv_path = Path(args.inventory_path)

    if args.command == "inventory":
        existing = load_existing_inventory(inv_path, getattr(args, "force", False))
        args.docs_repo = resolve_docs_repo(args, existing)
        inv = build_inventory(Path(args.docs_repo), existing)
        if write_inventory_if_changed(inv_path, inv, existing):
            print(f"wrote {inv_path}")
        else:
            print(f"inventory unchanged ({inv_path} not rewritten)")
        if not getattr(args, "no_sync", False):
            sync_requirements(Path(args.docs_repo), inv, Path(args.requirements_path),
                              install=True, python=Path(args.python),
                              verbose=getattr(args, "verbose", False))
        print(f"  outputs: {len(inv['outputs'])}   "
              f"voyage models: {inv['voyage_models_observed']}")
        if inv.get("stale_output_ids"):
            print("  stale (no longer detected): " + ", ".join(inv["stale_output_ids"]))
        bad = [o for o in inv["outputs"] if o.get("match_error")]
        for o in inv["outputs"]:
            if o.get("derived_from"):
                src = o["derived_from"][0]["source"]
                print(f"  - {o['id']:<44} {o['title'][:50]}  ← {src}")
            else:
                print(f"  - {o['id']:<44} {o['title'][:50]}  ⚠ no source")
        if bad:
            print("  ⚠ " + str(len(bad)) + " output(s) have no unique source script "
                  "(rename the producing .py so <name>.out ↔ <name>.py, or fix the .out name):")
            for o in bad:
                print(f"    - {o['id']}: {o['match_error']}")
        return 0

    if args.command == "all":
        # End-to-end release pass: refresh the inventory, then convert + run.
        existing = load_existing_inventory(inv_path, getattr(args, "force", False))
        args.docs_repo = resolve_docs_repo(args, existing)
        inv = build_inventory(Path(args.docs_repo), existing)
        if write_inventory_if_changed(inv_path, inv, existing):
            print(f"inventory refreshed at {inv_path}")
        else:
            print(f"inventory unchanged ({inv_path} not rewritten)")
        if not getattr(args, "no_sync", False):
            sync_requirements(Path(args.docs_repo), inv, Path(args.requirements_path),
                              install=True, python=Path(args.python),
                              verbose=getattr(args, "verbose", False))
    else:
        try:
            with open(args.inventory_path, encoding="utf-8") as fh:
                inv = yaml.safe_load(fh)
        except OSError as e:
            print(f"cannot read inventory {args.inventory_path}: {e}\n"
                  "Run `update_voyage_output.py inventory` first.", file=sys.stderr)
            return 2
        args.docs_repo = resolve_docs_repo(args, inv)

    args.inventory = inv
    if args.command == "convert":
        return cmd_convert(args)
    if args.command == "run":
        return cmd_run(args)
    if args.command == "all":
        rc = cmd_convert(args)
        if rc:
            return rc
        return cmd_run(args)
    if args.command == "models":
        return cmd_models(args)
    return 0


def write_inventory_if_changed(inv_path: Path, inv: dict, existing: Optional[dict]) -> bool:
    """Write inventory.yaml only when something other than `generated_at`
    changed, so regenerating on a release doesn't churn git history."""
    if existing is None:
        write_yaml(inv_path, inv)
        return True
    new = {k: v for k, v in inv.items() if k != "generated_at"}
    old = {k: v for k, v in existing.items() if k != "generated_at"}
    if new == old:
        return False
    write_yaml(inv_path, inv)
    return True


if __name__ == "__main__":
    sys.exit(main())
