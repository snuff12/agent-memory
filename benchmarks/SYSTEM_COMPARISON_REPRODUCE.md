# Reproducing the external-system diagnostic

Read [the protocol](SYSTEM_COMPARISON_PROTOCOL.md) and
[the result limitations](SYSTEM_COMPARISON.md) first. These are experimental
adapters, not production integrations. Run state, upstream checkouts, downloaded
models, tool transcripts and generated indexes belong **outside this checkout**.
Use synthetic/public sources only. No dependency below is needed by `amem`.

## Freeze the source pool

Install this repository in a virtual environment. Choose a new private work
directory. `prepare` refuses to replace a frozen run.

```bash
export AMEM_COMPARISON_WORK="$PWD/../memory-system-comparison"
.venv/bin/python -m benchmarks.comparison_common --workspace "$AMEM_COMPARISON_WORK"
```

This writes separate `corpus.json` (sources), `cases.json` (offline evidence
requirements), `queries.json` (**only** each case's `id` and `query`), and
`frozen.json` (hashes, model and controller subset), plus private work folders.
Never pass the evidence requirements to a native backend or agent. The fixture reads public engine files from the
checkout: changing those files changes the corpus hash. Match the recorded
engine revision and hashes when comparing with the published run.

## Isolated native installations

Use the revisions and actual installed versions in
[`system-comparison-2026-10-15.json`](results/system-comparison-2026-10-15.json).
The package versions for Letta Code and SCIP describe the npm artifacts actually
executed; their separately recorded upstream main commits are source references,
not assertions that those npm artifacts were built from main.

| Workspace path | Runtime and installation |
|---|---|
| `upstream/qmd` | Pinned Git checkout; Node 22, `npm install` and the package build. This revision had no npm lockfile, so `npm ci` was unavailable. |
| `envs/hipporag` | Python 3.11 venv; pinned upstream HippoRAG editable install, Torch 2.5.1 and Transformers 4.45.2. Use this source version's optional vLLM path; the initial PyPI installation required unavailable vLLM. |
| `envs/comparison` | Python 3.11 venv; pinned Mem0 editable install with Qdrant local storage. The second retrieval configuration also uses FastEmbed 0.9.0, spaCy 3.8.16 and its downloaded English model. |
| `envs/graphiti-falkor` | Python 3.14 venv; pinned Graphiti with its FalkorDB extra, `falkordblite==0.10.0`, `falkordb==1.8.0`, `redis==8.1.0`, `httpx==0.28.1`. Docker was unavailable in this run. |
| `envs/letta` | `npm install @letta-ai/letta-code@0.34.15`; isolated local backend state. |
| `envs/scip` | `npm install @sourcegraph/scip-python@0.6.6`; actual native Python indexer. |

Clone HippoRAG, Graphiti, Mem0, Letta Code and scip-python under `upstream` too.
Store `{repository, commit}` receipts as `upstream/NAME.revision.json`. Do not
commit the checkouts, node modules or environment folders. The full dependency
trees are platform dependent; the result is configuration specific and is not
a hermetic, cross-platform reproduction of an upstream paper.

## LLM and embedding transport

The measured run used Codex CLI 0.162.1, its existing ChatGPT-plan sign-in,
`gpt-6.1-sol` and low reasoning. It did not extract credentials or call a billed
LLM API. `codex_inference.py` runs documented non-interactive `codex exec` with
replacement instructions, disabled tools and private exact-request caching.
The shared controller uses the same helper. Review its flags against the CLI
version you install; available models and account usage limits can change.

Run `benchmarks.comparison_server` in an isolated Python environment containing
FastEmbed 0.9.0, ONNX Runtime, NumPy and tokenizers. Supply a local, verified
`intfloat/multilingual-e5-small` ONNX model directory with its tokenizer files.
The recorded E5 revision is `614241f622f53c4eeff9890bdc4f31cfecc418b3`.

```bash
python -m benchmarks.comparison_server \
  --workspace "$AMEM_COMPARISON_WORK" --model-path /absolute/path/to/e5-small
```

It listens on loopback port 55961. Native worker clients use a dummy local key;
the embedding path is local. The two-slot Codex queue is intentional. It affects
elapsed time, so do not treat concurrent job times as standalone latency scores.
qmd downloads its own three native GGUF models into the workspace cache; verify
their exact revisions and SHA-256 values against the result receipt.

## Retrieval and shared-controller runs

From the repository root, run native ingestion/query passes with their isolated
Python runtimes. Each pass writes original-question rankings before scoring.
Run one writer per backend state directory.

```bash
node benchmarks/qmd_comparison.mjs "$AMEM_COMPARISON_WORK"
"$AMEM_COMPARISON_WORK/envs/hipporag/bin/python" -m benchmarks.native_memory_worker \
  --workspace "$AMEM_COMPARISON_WORK" --method hipporag
"$AMEM_COMPARISON_WORK/envs/graphiti-falkor/bin/python" -m benchmarks.native_memory_worker \
  --workspace "$AMEM_COMPARISON_WORK" --method graphiti
"$AMEM_COMPARISON_WORK/envs/comparison/bin/python" -m benchmarks.native_memory_worker \
  --workspace "$AMEM_COMPARISON_WORK" --method mem0
```

Score completed raw runs and invoke the shared controller:

```bash
.venv/bin/python -m benchmarks.system_comparison \
  --workspace "$AMEM_COMPARISON_WORK" --method qmd
.venv/bin/python -m benchmarks.system_comparison \
  --workspace "$AMEM_COMPARISON_WORK" --method qmd --controller
```

Repeat with `lexical`, `hipporag`, `graphiti` and `mem0`. `--controller` starts
native query workers with `--no-build`; it does not reingest sources. Output
JSONL files resume by completed case ID. Do not reuse a result filename after
changing code, configuration or models. Preserve failed trials separately.

For qmd component checks, use `--no-build --mode=lexical`, `vector` or `hybrid`,
then score `qmd-lexical`, `qmd-vector` or `qmd-hybrid`. Read the separately declared
[component protocol](QMD_ABLATION_PROTOCOL.md). The separate warm-cache fresh-DB
build probe is `node benchmarks/qmd_build_probe.mjs WORKSPACE`.

The Mem0 minimal arm came from genuinely missing optional retrieval dependencies;
it was not a deliberately disabled production default. Preserve its raw rankings
as `mem0-minimal-raw.jsonl` and its state before the native BM25 backfill. With
dependencies installed, `--method mem0 --no-build --bm25-backfill` adds native
sparse vectors over unchanged extracted facts. This is not a complete rerun of
all entity enrichment. The scorer accepts `--method mem0-minimal` for the initial
rankings. Do not overwrite the original measured build time with a reload time.

For SCIP, generate the binary with the installed native indexer over the seven
Python source files. An empty environment JSON list avoids collecting private
package environments; unresolved external imports remain outside scope.

```bash
printf '[]\n' > "$AMEM_COMPARISON_WORK/runs/scip-environment.json"
"$AMEM_COMPARISON_WORK/envs/scip/node_modules/.bin/scip-python" index \
  --cwd "$AMEM_COMPARISON_WORK/memory/knowledge" \
  --project-name agent-memory-diagnostic --project-version 17d7260 \
  --environment "$AMEM_COMPARISON_WORK/runs/scip-environment.json" \
  --output "$AMEM_COMPARISON_WORK/runs/index.scip"
node benchmarks/scip_decode.mjs "$AMEM_COMPARISON_WORK"
.venv/bin/python -m benchmarks.scip_comparison "$AMEM_COMPARISON_WORK"
```

The decoder uses pinned upstream generated protobuf code and qmd's installed
TypeScript compiler; it refuses to overwrite an existing decoded index.
The integration scorer uses
`--method lexical-scip`; see [its protocol](SCIP_INTEGRATION_PROTOCOL.md).

## Native agents, reported separately

For actual Codex plus the actual memory MCP, no persistent global server entry
is installed:

```bash
.venv/bin/python -m benchmarks.native_codex_comparison "$AMEM_COMPARISON_WORK"
.venv/bin/python -m benchmarks.native_codex_comparison "$AMEM_COMPARISON_WORK" --score
```

The wrapper accepts source reads only for visibly returned refs. It preserves
the core ref/source checks and enforces the declared output caps. Read-only MCP
annotations and per-invocation tool approval metadata are needed for the CLI's
non-interactive policy. See [the native Codex protocol](NATIVE_CODEX_PROTOCOL.md).

For native Letta, start `benchmarks.comparison_frontdoor` on 55962; configure an
OpenAI-compatible provider in an isolated `LETTA_LOCAL_BACKEND_DIR`, with base
URL `http://127.0.0.1:55962/v1` and dummy key `benchmark-local`. Use Letta's local
backend, not its managed service. Then run `benchmarks.letta_comparison WORKSPACE`
and the same command with `--score`. The runner enables only Read/Grep/Glob.

Preserve that demonstration. For the separate matched-output-cap experiment,
start `benchmarks.comparison_frontdoor --budgeted --workspace WORKSPACE` on
55963 and point the isolated Letta provider there. Use `--budgeted` for both the
runner and scorer. The scorer checks the capped model-visible views, not the
larger native transcript. See [the budget protocol](LETTA_BUDGET_PROTOCOL.md).

## Export and checks

```bash
.venv/bin/python -m benchmarks.comparison_summary \
  --workspace "$AMEM_COMPARISON_WORK" --output /private/path/summary.json
.venv/bin/python -m unittest discover -s tests -t .
.venv/bin/python scripts/check_public_tree.py
```

Inspect completion, unique case IDs, setup errors, missing source IDs, source
linkage and caps before copying a compact result into Git. The exporter removes
answers, tool transcripts and device-specific source paths. It includes counts
and pinned identities; it is not permission to publish arbitrary run files.
Scores compare delivered literal source spans, not final-answer correctness.
Keep the private audit data for failed runs and corrected transport bugs.
