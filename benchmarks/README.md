# LongMemEval-S retrieval benchmark

[한국어](README.ko.md)

This benchmark measures whether Agent Memory retrieves the conversation sessions
that contain the evidence for a question. It needs no model, API key, or GPU.
The [initial results](RESULTS.md) and [retrieval improvements](IMPROVEMENTS.md) cover the full pinned cleaned
LongMemEval-S dataset. They are **session retrieval scores**, not answer accuracy.

## Reproduce

Install the project as described in the root README, then run from the checkout:

```bash
.venv/bin/python -m benchmarks.longmemeval download
.venv/bin/python -m benchmarks.longmemeval run \
  --output "$HOME/.cache/agent-memory-benchmarks/longmemeval/my-run"
```

The output directory must be new. Data, per-question records, and disposable
indexes must remain outside the checkout. The default download location is
`~/.cache/agent-memory-benchmarks/longmemeval/`; `XDG_CACHE_HOME` is supported.
Use `download --destination /absolute/path/to/data.json` and
`run --data /absolute/path/to/data.json` to choose another location.
Download size is about 277 MB; only one question's index exists at a time.

For a smoke run, add `--limit 10`. This selects the first ten questions in
published order and marks the report as partial. It is not a representative
sample. `--allow-unpinned` permits custom or synthetic data and always prevents
the report from being labeled a full official-data run.

Each run creates:

- `summary.json`: aggregate scores, category scores, latency, byte counts,
  dataset checksum, source fingerprints, and environment versions.
- `cases.jsonl`: question IDs, evidence session IDs, ranked candidate session IDs
  and chunk ordinals, timings, and scores. It contains no questions, answers,
  conversation text, or local paths. Its checksum is recorded in the summary.

## Dataset and isolation

The source is the authors' [LongMemEval cleaned release](https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned),
revision `98d7416c24c778c2fee6e6f3006e7a073259d48f`, file
`longmemeval_s_cleaned.json`. The downloader verifies SHA-256:

```text
d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442
```

All 500 questions run independently. Each question gets a fresh private memory
home containing only its own history; the home is deleted before the next case.
Sources contain session dates and each turn's role and content. The adapter does
not copy the question, expected answer, `has_answer`, or `answer_session_ids`
into the index. No summaries, embeddings, query expansion, or answer-derived
index expansion are used. Historical messages are benchmark data, not instructions.

There are 470 answerable questions and 30 abstention questions. Following the
[official evaluation](https://github.com/xiaowu0162/LongMemEval#memory-retrieval),
retrieval scores use only the 470 answerable questions. The other 30 run for
diagnostics; a returned candidate is not treated as an incorrect answer. This
retriever does not generate an answer or make an abstention decision.

Some published histories repeat a session ID with different text. The adapter
preserves every entry as its own source file and scores the published session
IDs. It does not silently drop duplicate entries or rewrite the gold labels.
The summary reports the duplicate-entry count.

## Protocol

Protocol identifier: `longmemeval-s-session-retrieval-v1`.

One text source file represents one session entry, in published order. Neutral
filenames identify positions, rather than embedding questions or answers.
Agent Memory uses its existing 2,400-character chunk target, 240-character
overlap, and ordinary document-source configuration. The evaluation adapter
does not change the engine's retrieval algorithm. Engine revisions are the
subject of the before/after comparison, with source fingerprints recorded.

| System | What is scored |
|---|---|
| `amem_candidates` | The engine's top 20 chunk candidates before CLI output formatting. |
| `amem_cli_4000_bytes` | Only the candidates visible after the real CLI renderer applies its 4,000 UTF-8 byte limit, including provenance headers and snippets. Rendering shares path prefixes and reserves one pointer per file first, then keeps additional chunks when space remains. |
| `fts5_bm25` | A word-only SQLite FTS5 BM25 ablation on exactly the same indexed chunks. |

The CLI experiment corresponds to `amem search ... --collection research
--limit 20`, rather than the default five-result request. Its output cap remains
the existing 4,000-byte CLI cap.

For each system, preserve candidate order, deduplicate by published session ID,
and score the first 1, 5, or 10 distinct sessions. Fewer candidates remain fewer
candidates; the adapter does not fill missing ranks using gold evidence.

The BM25 ablation uses the same word terms, OR/prefix query, FTS column weights,
160-candidate pool, and limit of two chunks per source file as the engine. It
omits rank fusion, trigram, path, and short-term fallback channels. It is a
controlled word-only comparison, **not a reproduction of the paper's
`flat-bm25` implementation**, which has different tokenization and indexing.

| Metric | Definition |
|---|---|
| `session_hit@k` | Fraction of questions with at least one evidence session in the first k distinct sessions. |
| `session_all@k` | Fraction of questions with **every** evidence session in the first k. This has the binary meaning of upstream `recall_all@k`. |
| `session_recall@k` | Mean fraction of a question's evidence sessions retrieved. |
| `session_mrr` | Mean reciprocal rank of the first evidence session among the available distinct candidates. |

The all-evidence definition follows the authors' [evaluation code](https://github.com/xiaowu0162/LongMemEval/blob/9e0b455f4ef0e2ab8f2e582289761153549043fc/src/retrieval/eval_utils.py).
The chunk adapter, finite 20-candidate frontier, and renderer experiment are
specific to this repository. Do not compare the resulting numbers directly to
session-index baselines or QA leaderboards with different retrieval budgets.

## Interpreting results

Session evidence retrieval does not prove that the useful answer span is in a
returned snippet or a later budgeted `amem read`. The CLI results measure the
visibility of session locations under the navigation budget. They do not measure
the completeness of a generated answer. End-to-end QA needs a separately fixed
reader model, prompt, read budget, and answer grader; none was run here.

Scores are unweighted means over answerable questions, with per-category
breakdowns. Latency is one query per history on the reported local environment,
with setup/indexing excluded. The BM25 query follows the Agent Memory query and
does not repeat the engine's policy and file-integrity checks; its timing is
diagnostic and is not a fair standalone speed comparison. No cold-cache or
multi-user throughput claim is made.

Absolute source paths participate in engine IDs and CLI headers. Different
work directories can change score ties or the number of visible snippets.
Fingerprints and version metadata aid reruns; the report does not promise
identical timings or bit-for-bit scores on another machine.

Only scores, ranked session IDs, and this harness are distributed here. Downloaded
conversations, indexes, and the device's operational memory are excluded. The
upstream dataset is marked MIT; see the authors' dataset and
[benchmark repository](https://github.com/xiaowu0162/LongMemEval) for attribution.
