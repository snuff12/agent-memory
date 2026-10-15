# Practical semantic/graph experiment: frozen protocol v1

This is a diagnostic source-use experiment, not a representative benchmark or
an implementation of HippoRAG/Graphiti. The fixture and rules are fixed before
running inference. Do not tune weights, extractors, queries, cutoffs, or examples
against failures. Correct execution bugs separately and disclose them.

Compare: current lexical retrieval; lexical retrieval with a bounded follow-up
based only on names read from sources; the existing local semantic pilot;
equal-weight RRF lexical+semantic; passage/entity graph propagation seeded by
lexical/query matches; and lexical+semantic seeds with the same graph.

The graph is automatic, local, and evidence-backed: passage nodes connect to
literal entity/symbol mentions extracted without question or answer access.
Use capitalized Latin names, camel/underscore code symbols, and explicit
backtick/Markdown-link targets. This deliberately exposes extraction limits
for unmarked Korean names. Personalized PageRank uses restart 0.15 and 30 fixed
iterations. It is untyped co-occurrence, not extracted factual triples, temporal
reasoning, entity disambiguation, or a substitute for source validation.

Each method gets the same 4,000-byte search display, at most three source reads,
and 1,200 bytes per read, including provenance. A source requirement passes only
when its literal evidence is actually present in fetched canonical text; finding
a file ID is insufficient. All required spans must be read for task completion.
The fixed literal needle is an evidence check, not an LLM answer-quality judge.
Candidate lists retain at most two chunks per document, matching the core;
the existing semantic pilot has its stricter one-window-per-document limit.
Report per-case regressions as well as recoveries. Unanswerable cases measure
unnecessary source reads, not answer hallucination or calibrated abstention.

Follow-up baseline: fetch the first lexical candidate, extract literal names
from that read only, remove names already in the query, and search at most three
new names together once. This is a reproducible heuristic, not a capable agent
or an optimal human baseline. Its extra search output counts toward the same
total 4,000-byte search budget. Graph traversal has no access to answer labels.

Use `intfloat/multilingual-e5-small` at the existing pinned revision and the
existing FastEmbed worker. No training, model selection, alias dictionary, query
paraphrasing model, external API, or gold labels enter indexing or retrieval.
Report cold per-request worker latency separately from graph/lexical work.
One local run and this small deliberately mixed set do not establish general
accuracy or performance at corpus scale.

Record source/fixture/code/model hashes, runtime versions, construction times,
state sizes, source bytes, search/read counts, median/p95 retrieval-to-read
latency, and sampled process-tree RSS. RSS sampling can miss short peaks and
sum shared pages more than once. Reuse semantic candidates for combined methods
without charging them twice, but include the measured semantic call time in
each method's reported latency. Warm-model speed is not inferred from this run.

Keep run indexes, vectors, models and logs outside the checkout. Publish only
synthetic/public sources, reproducible code and compact result records.
Check stale-source rejection, collection isolation, and the semantic pilot's
existing fail-closed rejection of curated memory. These checks do not establish
semantic support for reviewed-card project/domain/sensitivity policies.
Do not enable a production default based only on this experiment.

## Declared sensitivity run after the first broad-display probe

The initial run displayed up to 20 candidates. Distinct-source budget reservation
could hide a second code chunk before the three reads, unlike the recommended
`--limit 3` workflow. Retain that result; also run all methods with a display
limit of 3, unchanged cases/corpus/model/ranking rules/read budgets. This is a
workflow sensitivity check, not a tuned weight or a replacement for a failed
case. Record the limit in the frozen manifest. The initial memory sampler could
attribute a just-exited semantic worker to the next lexical phase; only the
corrected phase-stable/effective memory measurements are used for comparisons.
The original result remains available with this limitation disclosed.
