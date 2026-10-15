# External systems and agent workflow comparison, v1

Declared before scoring. This is a local diagnostic for adoption decisions, not
a leaderboard or a reproduction of any project's published benchmark.

## Scope

Attempt the actual upstream implementations of qmd, HippoRAG 2, Graphiti, Mem0
OSS, and Letta. Attempt Sourcegraph's SCIP Python indexer for code navigation;
it is a code indexer, not a general memory system. Record pinned revisions,
installed versions, backend/model choices, setup failures and unsupported tasks.
Never replace a failed system with a similarly named home-grown prototype or
count an installation failure as zero retrieval accuracy.

Use synthetic documents and public project source only. No operational memories,
private configuration, conversation capture, or production index changes.
Credentials remain with the existing Codex CLI. The user requested ChatGPT-plan
Codex rather than a separately billed LLM API. Use its documented non-interactive
interface for structured inference; do not copy or expose authentication tokens.
Record the actual Codex model and usage. Local embedding/reranking models needed
by a native system are separate dependencies and must be reported.

## Data and separation

Preserve all 42 questions from practical-retrieval-variants-v1, with their 38
positive and four absent-answer cases. Freeze an additional small public-source
task set before inference. Report old and new tasks separately. This is still a
small, deliberately difficult sample and does not estimate general reliability.
Freeze corpus, questions, evidence requirements, source revisions, controller
prompt, budgets, adapters and model identities before scoring. Questions and
expected answers never enter an index, extraction prompt, query-rewriting
prompt, candidate-to-source mapping or controller context. The controller gets
only the current question and budgeted retrieval/read observations.

Index the same canonical text. If a native system needs chunks, use the same
source chunks and record source IDs outside the text. Follow native ingestion
and retrieval rather than hardcoding relations, translations or query aliases.
Keep extracted summaries/facts distinct from verified original source spans.
Loss of source linkage is a limitation, not permission to infer provenance from
the gold requirements. Keep any backend-specific corpus projection explicit.

## Measurements

1. Run all original questions through each supported native retriever. Apply
   the common evidence-delivery check: 4,000 UTF-8 bytes of search output, up to
   three original-source reads of 1,200 bytes each, including provenance. Report
   all required source spans read, individual span coverage, per-type failures,
   paired gains/losses, and unnecessary reads on absent-answer questions.
2. Compare a common Codex search/read controller on a predeclared stratified
   subset: first two cases in each positive category plus all four absent cases.
   The controller may rewrite queries and request source-bound follow-ups. Use
   the same total search/read budgets, at most six searches and eight decisions.
   Preserve evidence already read. Report this separately from single-query
   retrieval. A native agent that cannot use this controller/budget is a separate
   demonstration, not a directly comparable ranking entry.
3. Use new public-source tasks as a separate practical check, with the same
   source budgets. Code-only tools report unsupported non-code tasks as N/A.

The shared controller's per-search display cap is 1,200 bytes, within the same
4,000-byte task total. It has no OS, browser or other tools; it emits one JSON
decision for the harness to validate. A source-bound literal is checked for all
retrievers, not only the local engine. Native Letta is a separate filesystem
demonstration with only Read/Grep/Glob, eight native turns, and requested limits
of three reads of 15 lines. These are not equivalent byte budgets: record actual
tool use and do not mix its result into the common-reader ranking. SCIP measures
actual definition/reference coverage, not a natural-language answer score.

Native inference uses a loopback adapter to documented `codex exec`, model
`gpt-6.1-sol`, reasoning `low`, authenticated with the user's existing ChatGPT
plan. This is not a reproduction of each paper's LLM/API configuration. Native
temperature and output-token request fields are recorded but are not exact CLI
decoding controls. Letta's SSE framing is buffered generation, not a streaming
performance measurement. A shared two-slot inference queue can affect native
wall times; report calls/tokens and do not rank isolated latency from this run.
HippoRAG, Graphiti and Mem0 use the same existing multilingual E5-small model
through the loopback embedding transport (query/passage prefixes, normalized
mean pooling). Inputs above its 512-token window are counted as truncated.
qmd uses its own native default embedding/expansion/reranking models, whose
actual file hashes are checked against the downloaded revisions. Results apply
to these configurations, not a model-independent comparison of algorithms.

Before viewing scores, the minimal Mem0 installation was observed to lack its
optional BM25/NLP dependencies. Preserve that arm. Also prepare the native BM25
dependency and source-independent sparse-vector backfill as a declared retrieval
configuration check. Reindex stored facts without changing fact text or adding
questions. Report missing language models/entity enrichment explicitly; never
call a vector-only configuration the fully enabled hybrid implementation.

Report build time, query/read/controller time separately, model startup time,
disk footprint, sampled peak RSS when attributable, LLM calls/tokens, failures,
and budget violations. A native system's internal indexing/retrieval calls count
as costs even if their text is not displayed to the source-reading controller.
Similarity scores are not confidence. Source-span success is not answer accuracy;
do not label an automatic literal-needle check as an LLM quality judgment.

Execution correction before Graphiti scores: the pinned FalkorDB fulltext builder
keeps standalone `_` atoms; RediSearch rejects an OR clause containing this
punctuation-only token during native bulk resolution. A benchmark-only driver
subclass drops punctuation-only atoms after native sanitization, preserving all
words with letters/digits and the native builder/ranking/graph flow. A direct
check of all 389 extracted facts then had no syntax errors. Preserve the failed
unmodified state and retry fresh state with existing exact-request inference
cache. Label this compatibility fix and the reused cache in results; do not
claim unmodified upstream execution or a fresh uncached build time.

Embedded-state execution correction: explicitly SAVE the native graph before
the success marker, then verify its episode count on reload. Async embedded
cleanup otherwise lost the in-memory database between the retrieval pass and
the controller worker. Preserve the valid first in-process single-query trial
and exclude the empty-reload controller attempt. Rerun both final arms on the
persisted native state, without selecting the better of repeated results.

## Decision rule

No tuning cases, weights, cutoffs, prompts, models or expected evidence after
seeing scores. Fix execution bugs only, preserve the failed run and explain the
correction. Do not increase budgets for a losing method without a separately
declared sensitivity run for every applicable method.

Select an approach only for concrete useful recoveries, preserved exact matches,
bounded work and source provenance. State regressions and deployment gaps,
including existing scope/status/version controls. Synthetic gains alone do not
justify a new production default. A documented setup blocker can justify
deferring adoption, but not a claim that the method cannot solve the task.
