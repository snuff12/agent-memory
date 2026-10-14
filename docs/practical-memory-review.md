# Practical memory review

[한국어](practical-memory-review.ko.md)

Review date: 2026-10-14. Adoption is based on demonstrated usefulness in actual
tasks. Benchmark results are diagnostic evidence, not an optimization target.
The systems below were reviewed through their official documentation and code;
they were not run as a controlled head-to-head comparison.

## A useful source span matters more than a retrieved file ID

The previous bounded renderer always hid the second chunk of a source file.
This made more distinct file IDs visible but could hide complementary evidence.
In a deployment-policy example, rollout instructions appeared while the rollback
conditions disappeared even with more than 3,000 output bytes unused.

The same problem occurred while reviewing this repository's real `search.py`:
the query `short_terms _format_bounded_search` found two relevant chunks, but
only the first appeared in bounded output. This is a practical code-review
problem that a session-level retrieval score cannot diagnose.

Current rendering reserves one pointer per distinct source first, then adds
other chunks if their provenance and short snippets fit. The represented hits
are displayed in original rank order. This preserves cross-file navigation and
complementary sections of the same file under the existing byte cap. A regression
case requires both rollout and rollback instructions to remain visible.

The scores in [the earlier experiment](../benchmarks/IMPROVEMENTS.md) refer to
engine commit `ea53b90`, before this correction. No new benchmark score is
claimed for the correction.

## What other systems suggest

| Reference | Verified mechanism | Current implementation and practical gap |
|---|---|---|
| [qmd](https://github.com/tobi/qmd) | Keyword/vector retrieval, query expansion, model reranking, query intent, and retrieval traces via `--explain` | Default search is lexical. Semantic search is a separate experimental pilot, not fused into normal search; there is no default model reranker or per-request score trace. Paraphrases and diagnosis of an irrelevant result need attention. |
| [HippoRAG 2](https://arxiv.org/abs/2502.14802) | Passage/entity connections and graph propagation for associative retrieval | One query retrieves independently matching chunks. A person-to-project-to-storage question may require an explicit follow-up query. A full graph is only one possible solution. |
| [Letta Filesystem evaluation](https://www.letta.com/blog/benchmarking-ai-agent-memory/) | The agent can rewrite a query and repeatedly search/open files before answering | Our tools and contract permit follow-up searches and reads, but the current benchmark evaluates a single original-query search. The complete agent workflow has not been evaluated here. |
| [Graphiti](https://github.com/getzep/graphiti) | Fact validity intervals, preserved historical relationships, and source episodes | Reviewed-card validity and superseded status exist. They do not provide event-time reasoning, an `as_of` query, or an explicit history of related fact changes across ordinary documents. |
| [Mem0](https://docs.mem0.ai/core-concepts/how-it-works) | Fact extraction, deduplication, embeddings, explicit memory operations | Cards and synthesis are curated manually. Optional drafting and duplicate/conflict review could help; automatic capture or promotion would violate this project's operating contract. |

Mem0's current documentation distinguishes managed Platform retrieval from OSS
retrieval, and describes its automatic extraction path as additive with explicit
update/delete for corrections. Graphiti is the OSS framework; managed Zep is a
different deployment. Their capabilities and published scores must not be mixed.
None of the mechanisms above proves an improvement for this repository.

## Concrete gaps found in local probes

All automated probes used temporary, synthetic data. The repository-source
inspection above was a separate read-only analysis of public project code.

- **Query intent can be discarded.** `_fts_terms` keeps the first 12 distinct
  terms. A synthetic question with the decisive identifier after that boundary
  retrieved a general background document; searching the identifier alone found
  the target. The follow-up should preserve explicit filenames, IDs, and quoted
  terms, rather than merely increasing every query's term count.
- **Paraphrases may have no matching vocabulary.** `car repair` returned nothing
  for a document about `automobile maintenance`; the literal phrase found it.
  This illustrates a lexical limit, not proof that any particular embedding
  model would solve the task.
- **Relationships require another step.** `Mina database` found a record linking
  Mina to Aurora but not the separate record linking Aurora to CockroachDB.
  Searching Aurora exposed the second record. Iterative search can solve this;
  adopting a graph database is not required by this example.
- **A found source is not a read answer.** A protocol document was retrieved,
  but its late retention value was outside the first 1,200-byte read. A larger
  read included it, and continuation was correctly advertised. Improve source
  navigation or follow-up reading where helpful, rather than increasing all
  read budgets to improve a metric.

Existing retrieval artifacts have 85 questions missing all-evidence @10: 35
multi-session, 38 temporal, 5 preference, 5 assistant information, and 2 updates.
Of these, 79 also fail word-only BM25. These counts suggest questions to inspect;
they do not identify causes or establish priorities for real users. The data
is long conversation history, not representative evidence about every coding,
research, or project-memory workflow.

## Next work and adoption criteria

| Order | Candidate | Evidence needed before adoption |
|---|---|---|
| First | Preserve decisive query terms and make retrieval failures explainable | Actual long project questions retain their requested file/ID; a wrong or missing result can be traced to query selection, source freshness, or policy without recording conversations. |
| First | Better navigation from search to useful source spans | Code review and policy tasks expose all required definitions, exceptions, or rollback conditions at a reasonable read/search cost. |
| Next | Iterative search and explicit time/version interpretation | A task correctly joins project decisions across files and distinguishes an old rule from the current one, with source citations. |
| Conditional | Optional semantic candidates and reranking | Real paraphrased questions improve after source reads, with acceptable local latency, memory use, and scope isolation. Model relevance scores are not confidence. |
| Conditional | Memory drafting and duplicate/conflict review | Explicitly authorized captures become easier to review without silently deleting history, changing canonical sources, or promoting inbox candidates. |

Keep the existing strengths: read-only canonical sources, generation-bound refs,
review/scope checks on search and read, explicit capture, and bounded context.
Assess useful evidence, correct task decisions, tool calls, bytes read, latency,
and wrong-version or cross-scope errors. Do not accept a change solely because
more expected session IDs appear in a benchmark result.
