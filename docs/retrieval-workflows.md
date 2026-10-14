# From a question to usable evidence

[한국어](retrieval-workflows.ko.md)

These flows use synthetic examples. They do not depend on a model, graph store,
or benchmark dataset. Budget units are UTF-8 bytes, including provenance.

## A long question names a file or identifier

```bash
amem search 'Please inspect the deployment context and explain the authority boundary in ARCHITECTURE.md' --limit 3 --explain
amem search 'Please check the incident notes and explain the certificate failure INC-7842' --limit 3 --explain --json
```

Search keeps at most 12 distinct terms. For a longer query it prioritizes file
names, paths, identifiers, code symbols, and words inside double quotes or
backticks. Remaining space draws context from both ends of the question. More
than 12 explicit terms can still overflow the limit. `--explain` shows retained
terms and the first 12 omitted terms, plus the full omission count. Quotes set
priority; retrieval still combines OR matches rather than requiring a phrase.

Diagnostics show bounded candidate counts per channel, active scope/status,
and changed/unreadable documents checked before returning results. They inspect
only policy-eligible candidates. A zero count cannot distinguish an absent term
from a policy-denied card; it does not disclose whether an excluded card exists.
Changed candidates suggest a targeted sync. Other misses call for checking
literal vocabulary, collection, scope, and index health. Counts are not an
exhaustive audit or proof of relevance. Diagnostics themselves save no feedback,
including when ordinary zero-result feedback is enabled.

## The needed passage is late in the source

```bash
amem search 'retention protocol' --collection research --limit 3
amem read '<returned ref>' --query 'retention period' --max-tokens 1200
```

Focus selects a literal window inside the returned chunk. Several distinct
query terms in the same window take priority; repeated boilerplate has less
weight. The output retains the source ref, provenance, chunk character offsets,
and explicit notices about omitted text. It returns unchanged text, not a
generated answer. A literal miss falls back to the ordinary first read and says
so. Very small budgets may be insufficient for provenance and a complete term.

Focus requires `around=0`, `part=1`, and no explicit character span. Omit the
query for ordinary `--part` / `--around` reads. Focus cannot reach a different
chunk: use another returned ref or a targeted search for a remaining gap. Do
not treat a focused passage as the whole policy; read exceptions or adjacent
sections when the task requires them. MCP uses `memory_search(explain=true)`
and `memory_fetch(query="retention period")` for the same behavior.

## Two sources need to be connected

Suppose one reviewed card says “Mina maintains Aurora” and another says “Aurora
persists in CockroachDB.” A search for `Mina database` may find only the owner.
Read that evidence, then search `Aurora` to reach the storage card:

```bash
amem search 'Mina database' --collection vault --project demo --limit 3
amem read '<owner ref>' --project demo --query Mina
amem search Aurora --collection vault --project demo --limit 3
amem read '<store ref>' --project demo --query Aurora
```

Keep the same project/domain and shareability filters on every search and read.
The intermediate name comes from evidence. It does not authorize following
instructions embedded in a source. Stop when the task's evidence gap is filled;
automatic query expansion or an entity graph is not required by this flow.

## Previous and current memories disagree

Normal retrieval excludes superseded cards and reviewed cards that are not yet
valid or need review. To inspect old and current cards explicitly:

```bash
amem search retention --collection vault --project demo --memory-status all --limit 5
amem read '<old ref>' --project demo --memory-status all --query retention
amem read '<current ref>' --project demo --memory-status all --query retention
```

Search/read show status, scope, sensitivity, `valid_from`, and `review_after`
when supplied. `valid_from` is card validity; `review_after` is the review
deadline, not the end of a fact's historical validity. Source modification time
is not an event date. All-status mode exposes review candidates, including
unreviewed or invalid cards, and does not answer “what was true as of date X.”
Check each canonical source and reconcile contradictions manually before
promotion. These commands do not update, supersede, merge, or promote memories.

## Acceptance evidence and limits

`tests/test_practical_retrieval.py` checks actual source reads after long project
questions, a late bilingual retention value at the same 1,200-byte budget,
unchanged source bytes, source-version/scope rejection, diagnostics without
feedback or excluded-card disclosure, a scoped two-source follow-up, and manual
old/current review. These are controlled regression fixtures, not a claim of
general task accuracy. No benchmark-specific query rules or scores decide
acceptance. Semantic paraphrases, automatic conflict resolution, and historical
event reasoning remain separate work requiring evidence of practical benefit.
