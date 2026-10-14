# First LongMemEval-S retrieval result

See the [subsequent retrieval improvements](IMPROVEMENTS.md) for current results.
This page preserves the initial baseline.

[한국어](RESULTS.ko.md) · [Protocol and reproduction](README.md) ·
[Aggregate JSON](results/longmemeval-s.json) ·
[Per-question results](results/longmemeval-s-cases.json)

The current combined retriever trails the word-only BM25 comparison on this
English conversation dataset. All evidence sessions were found within the first
ten distinct candidates for **277/470 questions (58.94%)**, compared with
**387/470 (82.34%)** for the word-only comparison. Applying the real CLI byte
budget leaves all evidence-session locations visible for **185/470 (39.36%)**.

These are session retrieval results. No answer-generation or answer-grading
model was used, and no end-to-end QA accuracy is claimed.

## Results

| System | All evidence @5 | All evidence @10 | Any evidence @10 | MRR |
|---|---:|---:|---:|---:|
| Agent Memory candidates | 34.89% | 58.94% | 85.74% | 0.5189 |
| Agent Memory CLI, 4,000 bytes | 34.89% | 39.36% | 74.68% | 0.5000 |
| FTS5 word-only BM25 | 72.77% | 82.34% | 95.53% | 0.8320 |

`@k` counts distinct session IDs after deduplicating up to 20 returned chunk
candidates. The CLI may expose fewer than k sessions: it exposed a mean of
5.68 distinct sessions in this run. The CLI row is a visibility measurement at
the navigation boundary, not a separate ranking algorithm.

| Question category | Answerable questions | Agent Memory all @10 | CLI all @10 | BM25 all @10 |
|---|---:|---:|---:|---:|
| Knowledge update | 72 | 69.44% | 45.83% | 97.22% |
| Multi-session | 121 | 43.80% | 23.14% | 74.38% |
| Assistant information | 56 | 55.36% | 44.64% | 89.29% |
| User preference | 30 | 60.00% | 30.00% | 86.67% |
| User information | 64 | 89.06% | 73.44% | 100.00% |
| Temporal reasoning | 127 | 53.54% | 33.86% | 68.50% |

## Run details

- Started at 2026-10-14 06:51 UTC; full run took 172.558 seconds.
- All 500 questions ran. Retrieval averages use 470 answerable questions;
  the 30 abstention questions have no retrieval score.
- Indexed 23,867 session entries and 134,811 chunks across independent histories.
  Thirteen repeated session-ID entries were preserved.
- Platform: macOS arm64, Python 3.9.6, SQLite 3.54.0. Precise environment and
  engine/harness SHA-256 fingerprints are in the aggregate JSON.
- Agent Memory query latency: median 16.962 ms, p95 24.674 ms. This excludes
  setup/indexing, rendering, source reads, and answer generation.
- Maximum CLI output: 4,000 UTF-8 bytes; mean 3,999.8 bytes, including headers.
- BM25 query latency is available in JSON for diagnostics. It runs after the
  engine query and skips the engine's integrity/policy checks, so it is not a
  fair independent speed comparison.

The published per-question JSON keeps the session rankings and scores from the
local `cases.jsonl`, omitting only the repeated `chunks` arrays. It contains no
question, answer, conversation, or device path. The aggregate's
`case_records_sha256` identifies the original complete local records; it is not
the checksum of this reduced public JSON. Rerun the harness to produce the
complete records, including candidate chunk ordinals.

## What this result supports

For this dataset and protocol, combined ranking performs worse than its
word-only comparison. The CLI output cap also reduces visible evidence-session
coverage. These are two separate measured limitations. A follow-up can inspect
channel contributions and context allocation while retaining this first result
as the baseline.

This run did not tune or change the engine after seeing scores. It does not
establish performance on Korean documents, curated project memories, source
freshness, or final task completion. The original automated tests cover those
implemented behaviors where applicable; they are not benchmark accuracy scores.

The data and its license come from the authors'
[LongMemEval cleaned release](https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned).
See the [protocol](README.md) before comparing to another system: chunking,
candidate counts, session aggregation, input budgets, and QA models can differ.
