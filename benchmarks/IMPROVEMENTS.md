# Retrieval and bounded-output improvements

Latest: [0.3.0 full remeasurement](V0_3_0.md), with an immediate-parent control.
The historical scores below remain unchanged.

These are historical measurements of engine commit `ea53b90`. A subsequent
practical review found that unconditionally hiding a second chunk of the same
document lost useful evidence. Current rendering keeps additional chunks when
space remains. These scores are not a measurement of that later revision. See
the [practical review](../docs/practical-memory-review.md). Descriptions below
refer to the measured revision.

[한국어](IMPROVEMENTS.ko.md) · [Reproduce](README.md) · [Initial result](RESULTS.md) ·
[Updated summary](results/longmemeval-s-improved.json) ·
[Updated per-question results](results/longmemeval-s-improved-cases.json)

The retrieval fix raises all-evidence session retrieval @10 from **58.94% to
81.91%**. Compact, source-first rendering preserves that **81.91%** after the
existing 4,000-byte CLI cap; the initial CLI result was **39.36%**.
The same word-only BM25 comparison scores **82.34%** in both runs.

## What changed

The short-term substring fallback had treated Latin function words such as
`I`, `is`, `my`, and `in` like short CJK queries. It added reciprocal-rank votes
from an unordered scan of chunks containing those characters, including matches
inside unrelated words. Short Latin terms also matched arbitrary parts of
titles and paths: for example, `in` matched `learning.txt`.

The fix restricts one/two-character body substring fallback to CJK terms and
removes short Latin terms from substring path hints. Word FTS continues to
handle Latin tokens and short names such as `AI.md`. Trigram search, compact
CJK search, exact filename navigation, collection priority, and memory-policy
checks remain available.

The old bounded renderer spent its budget on early snippets and repeated path
metadata. The new renderer first reserves provenance and a short snippet for
distinct source files, then shares the remaining snippet budget. It prints each
canonical path prefix once with a `source-root` label. Each represented source
retains an opaque ref, locator, trust label, and applicable memory warnings.

Bounded plain output shows the highest-ranked chunk for each source file.
Additional chunks remain available in JSON and unbounded output. The tradeoff
is shorter navigation snippets and fewer repeated views of one file; selected
source spans are still read with `amem read`. The MCP renderer uses the same
allocation under its existing 6,000-byte cap.

## Before and after

All-evidence @k means that every evidence session for a question occurs in the
first k distinct sessions, after deduplicating chunk candidates.

| Measurement | Initial | Improved |
|---|---:|---:|
| Agent Memory all evidence @5 | 34.89% | 72.34% |
| Agent Memory all evidence @10 | 58.94% (277/470) | 81.91% (385/470) |
| CLI-visible all evidence @10 | 39.36% (185/470) | 81.91% (385/470) |
| Agent Memory any evidence @10 | 85.74% | 95.32% |
| Word-only BM25 all evidence @10 | 82.34% | 82.34% |
| Mean distinct sessions visible in CLI | 5.68 | 13.76 |

The combined retriever remains two questions behind the word-only comparison
on all-evidence @10. No superiority over BM25 is claimed.

Both runs use the same pinned cleaned LongMemEval-S file, question set,
2,400-character chunk target, 240-character overlap, 20-candidate request,
per-file chunk cap, and 4,000-byte output cap. The evaluation harness is unchanged;
its SHA-256 is recorded in both summaries. All 500 questions run independently,
and the 470 answerable questions determine retrieval scores. Initial artifacts
remain available alongside the new results.

The data was used for diagnosis and verification: this is a development-set
before/after comparison, not an unseen held-out evaluation. Source-root paths
can influence engine ties and renderer overhead, as described in the protocol.
The result measures evidence-session locations, not final answer accuracy or
whether the answer span fits in a later source read.

## Verification

The regression suite covers short English substring false positives,
distinctive evidence displaced by function words, short Latin filenames,
references to ten distinct documents under a fixed byte cap, tiny byte budgets,
and preserved memory warnings. Existing tests cover Korean spacing, Unicode
normalization, two-character search, filename navigation, source integrity,
and memory eligibility. The installed CLI/MCP synthetic flow also passes.

The storage schema and extraction policy do not change. Existing indexes and
generation-bound refs remain compatible; a rebuild is not required for this fix.
