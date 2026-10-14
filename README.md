# Agent Memory

[한국어](README.ko.md)

A local memory and document retrieval engine that different AI agents can share.
Fork this repository, connect your own files, and use the same CLI from Codex,
Claude Code, Hermes, or an optional MCP client.

The public repository contains code, tests, and synthetic examples. Your files,
memory cards, queries, and indexes stay in a separate private directory.

```mermaid
flowchart LR
    A[Your source files] -->|read only| B[Incremental index]
    B --> C[Short search results]
    C --> D[Read selected source span]
    D --> E[CLI · agent skill · optional MCP]
    F[Explicit request to remember] --> G[Private memory inbox]
    G -->|review| A
```

## Origins and design references

The direct code base for this public repository is the device-local **Agent
Memory 0.2** engine developed for a shared, agent-independent knowledge index.
The original design notes credit the following work. The table distinguishes
design influences from libraries used by the implementation.

| Project or reference | Relationship | Part used in this system |
|---|---|---|
| [Karpathy's LLM Wiki pattern](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) | Design reference | Preserve raw sources; keep curated Markdown memory and navigation maps separate from source evidence. |
| [Cerebras Knowledge Base](https://www.cerebras.ai/blog/how-we-built-our-knowledge-base) | Design reference recorded in the original notes | Section-level retrieval, combining ranked candidates, and reading adjacent sections when needed. |
| [Cerebras Gist Memory](https://inference-docs.cerebras.ai/cookbook/agents/gist-memory) and [ReadAgent](https://arxiv.org/abs/2402.09727) | Design references | Start with a compact navigation layer, then read selected original passages on demand. |
| [SQLite FTS5](https://www.sqlite.org/fts5.html) | Core storage and search technology | Local manifests, BM25 word search, and trigram search projections through Python's SQLite binding. |
| [pypdfium2 / PDFium](https://github.com/pypdfium2-team/pypdfium2) and [pypdf](https://github.com/py-pdf/pypdf) | Runtime dependencies | PDF text extraction with a primary parser and fallback. |
| [PyYAML](https://github.com/yaml/pyyaml) | Runtime dependency | Parse memory-card metadata with additional validation in this engine. |
| [FastEmbed](https://github.com/qdrant/fastembed) | Optional experimental dependency | Local embeddings in a separate semantic pilot runtime. |
| [qmd](https://github.com/tobi/qmd) | Alternative backend considered in the design notes | Comparison for local search, CLI, and MCP; the current engine does not run on qmd. |

The design references are credited for ideas, rather than presented as an
upstream code fork. This implementation uses search snippets and selected source
reads; automatic wiki compilation and LLM-generated gist creation are not part
of the current engine. Hermes, Codex, and Claude Code are clients of the shared
CLI, rather than owners of its storage.

## What Agent Memory adds

These features turn the reference ideas into an operational local engine. They
describe this implementation; they are not claims that the underlying algorithms
or memory concepts were invented here.

| Area | Implemented behavior |
|---|---|
| Verifiable source reads | Content hashes, extraction-policy fingerprints, and generation-bound refs reject stale reads after source or policy changes. Reads retain canonical paths and page/line provenance. |
| English and Korean retrieval | Word and CJK trigram candidates are combined with reciprocal rank fusion; Unicode normalization handles common text representation differences. |
| Bounded context | Search, read, and context outputs have conservative UTF-8 byte budgets that include metadata and provenance. |
| Memory policy | Review status, project/domain scope, sensitivity, and validity are checked before limiting candidates and again during reads. Capture creates inbox candidates; promotion is manual. |
| Incremental ingestion | Reconcile configured collections without modifying sources. Support multiple document formats and optional Python AST chunking; extraction versions apply to affected file types. |
| Freshness and storage | Separate local sync age from upstream fact validity; preserve unvisited records after incomplete scans. Use owner-only state, SQLite/FTS secure deletion, and active-index compaction. |
| Retrieval feedback | Opt-in zero-result records and explicit relevance misses use deduplication, bounded retention, and best-effort secret redaction. Evaluation exports remain disabled candidates. |
| Shared interfaces | One CLI and agent skill support multiple clients. The optional stdio MCP exposes catalog, search, and fetch. Semantic retrieval remains a separate experimental channel. |

### Added for this public release

The following changes adapt the existing local engine for other people to fork
and install:

- **Separate private memory home:** use `AMEM_HOME` or `--home`, with a default
  outside the checkout and site-packages.
- **First-run setup:** `amem init` creates private directories and a generic
  configuration without overwriting existing data. `--demo` adds synthetic
  bilingual documents and a three-case evaluation set.
- **Reusable packaging and integration:** include initialization resources in
  wheels, remove device-specific paths, and provide one shared agent skill.
- **Publication checks:** exclude operating data from Git and provide a bounded
  public-source check. Automatic zero-result feedback defaults to off.
- **Contributor setup:** English/Korean documentation, a GitHub Actions workflow,
  unit tests, and an installed-CLI/MCP verification script using temporary data.
  Fix the test import that depended on a particular discovery command.
- **Configurable semantic pilot scope:** select a document collection and relative
  directory prefixes instead of a private installation's fixed research paths.

## Install and try

Requirements: macOS or Linux, Python 3.9 or newer, and SQLite 3.42.0 or newer
with FTS5 support. The SQLite version used by Python matters; installing a newer
`sqlite3` command alone does not change it. Native Windows is not supported by
the shell launcher and Unix locking; use a Linux environment such as WSL.

Fork and clone this repository, then run these commands from its root:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e .
.venv/bin/python -c 'import sqlite3; print(sqlite3.sqlite_version)'
./amem init --demo
./amem doctor
./amem sync
./amem search '간격 반복 학습' --collection research --limit 3
./amem read '<ref returned by search>' --max-tokens 1200
./amem eval
```

`init --demo` creates a private memory home at `~/.local/share/agent-memory`,
with two synthetic documents and three evaluation cases. It never overwrites an
existing or nonempty directory. Use `./amem init` for an empty setup. The example
evaluation checks basic retrieval; it does not measure performance on your corpus.

Put `--home` **before** the subcommand to use another private directory:

```bash
./amem --home /absolute/path/to/private-memory init --demo
./amem --home /absolute/path/to/private-memory sync
```

Alternatively, set `AMEM_HOME` in the agent's environment. `--home` takes
precedence. The same choice works with editable installs and wheels.

## Connect your files

Edit `config.json` in the private memory home. The complete starting template is
[`config.example.json`](src/agent_memory/resources/config.example.json).
Replace the `research` source path with your own directory, or add collections:

```json
{
  "name": "my-project",
  "path": "~/projects/my-project",
  "description": "Project code and design notes",
  "kind": "project",
  "trust": "working",
  "include_extensions": [".md", ".py", ".ts", ".tsx"]
}
```

Paths can be absolute, begin with `~`, or be relative to the memory home. Sources
remain in place and are read only. Add only directories you intend to search.
Common credential filenames and private-key extensions are excluded; this is
not a content scanner that can identify every secret inside ordinary documents.

```bash
./amem sync --collection my-project
./amem search 'distinctive terms' --collection my-project --limit 3
./amem read '<returned ref>' --max-tokens 1200
```

Supported inputs include Markdown, text, source code, HTML, PDF, DOCX, PPTX,
XLSX, and HWPX. PDF extraction uses PDFium with a pypdf fallback; scanned image
PDFs need OCR before ingestion. English word search and Korean/CJK character
search are combined. Source changes invalidate old references; search again.

## Memory and maintenance

```bash
./amem capture --kind decision --title 'Storage choice' \
  --body 'Use the local index for selective retrieval.' --source user
./amem search 'Storage choice' --collection vault --memory-status inbox
./amem status --require-fresh
./amem ensure-fresh --collection research --max-age-minutes 60
./amem compact
```

Captured cards enter `vault/inbox`; default retrieval includes only reviewed,
global cards. Project and domain filters must match on both search and read.
Review and promotion are manual; see [the lifecycle](docs/memory-lifecycle.md)
and [card schema](docs/memory-schema.md).

Automatic conversation capture is absent. Zero-result query feedback is disabled
by default and can be explicitly enabled in the private configuration. `sync`
refreshes local files; it does not verify whether an upstream website is current.
`--max-tokens` measures conservative UTF-8 byte units, not model tokenizer counts.
Secure deletion and `compact` affect the active index, not filesystem backups.

## Integrations

[Integration instructions](integrations/README.md) cover the shared agent skill,
stdio MCP, and an optional macOS schedule. The core CLI needs no model API key
and does not upload your corpus. Experimental semantic retrieval uses a separate
local runtime and model; see [the pilot](docs/semantic-pilot.md).

## Develop and verify

[LongMemEval-S retrieval benchmarking](benchmarks/README.md) provides a pinned,
LLM-free evaluation with a word-only BM25 comparison and the actual CLI byte
budget. Published numbers measure evidence-session retrieval, not answer accuracy.
The [first full result](benchmarks/RESULTS.md) reports 58.94% all-evidence @10
for combined retrieval and 82.34% for the word-only comparison on 470 answerable
questions; the CLI visibility result is 39.36%. These limitations are published
with the complete conditions and per-question scores.

```bash
.venv/bin/python -m unittest discover -s tests -t .
.venv/bin/python scripts/check_public_tree.py
.venv/bin/python -m pip install build
.venv/bin/python -m build
```

Tests use temporary synthetic corpora. CI runs the suite, demo flow, and public
source checks. See [architecture](docs/architecture.md) and
[contribution notes](CONTRIBUTING.md) before changing retrieval behavior.

## License

[MIT](LICENSE). Documents you connect and models you install have their own
rights and licenses; the engine's license does not change them.
