# Source navigation and optional semantic candidates (0.3)

Version 0.3 implements the two selected follow-ups from the
[external-system comparison](../benchmarks/SYSTEM_COMPARISON.md): finding useful
source locations after retrieval, and making optional semantic candidates easier
to read under a small source budget. Default word/CJK search remains unchanged.
The earlier comparison scores still describe its frozen engine revision.

## Available behavior

| Capability | CLI | MCP |
|---|---|---|
| Find literal definitions/call sites in a searched text/code file | `locate REF LITERAL --max-bytes 1200` | `memory_locate(ref=..., query=...)` |
| Read absolute source lines, including across chunks | `read REF --start-line N --end-line M --max-bytes 1200` | `memory_fetch(ref=..., start_line=N, end_line=M)` |
| Request local semantic candidates explicitly | `search QUESTION --collection NAME --semantic` | `memory_search(query=..., collection=..., semantic=true)` |
| Read the exact returned chunk window | `read REF --span-start N --span-end M --max-bytes 1200` | `memory_fetch(ref=..., span_start=N, span_end=M)` |

Line navigation finds literal text; it does not resolve language-server symbols
or infer a call graph. It opens only the current file authorized by the ref,
checks scope/status and source hashes, and preserves indentation and absolute
line numbers. PDF/Office documents retain chunk/page reads. Locate counts toward
the same task budget as fetch; normally use at most three source operations.

Semantic retrieval requires a separately prepared local runtime/model and a
built document scope. It excludes curated memories. Similarity is not confidence,
and no automatic fusion, relation extraction, graph database, or model retraining
was added. Setup and index compatibility are in [the pilot guide](semantic-pilot.md).

The worker already had a 512-token input cap. Version 0.3 adds a 512-byte source
body cap and adapts overlap to small windows. Search preserves at most two
different chunks per file, each with the best exact window. This helps retrieve
complementary evidence while keeping source reads small. Long provenance can
still require continuation. Rebuild the same collection/prefixes to get version 2
windows; existing version 1 vectors remain readable.

## Implementation checks, 2026-10-15

Core validation uses synthetic data: 133 unit tests, installed CLI/MCP flow,
and isolated wheel installation. Tests cover file boundaries, live source bytes,
policy changes during reads, stale refs, Unicode byte budgets, complete source
lines, semantic scope guards, complementary chunks, and full window coverage.
No operational corpus is distributed.

A separate local inference check froze eight synthetic/public files and six
questions before inference. The code sample was the repository's actual
`extractors.py`; semantic cases covered bike maintenance, trace retention,
release ownership, recovery, quiet hours, and remote work. It used the existing
pinned multilingual E5 model and FastEmbed runtime, top three candidates,
1,200-byte reads and unchanged original source text. Questions and source hashes
were kept in the private run receipt. No aliases, gold facts, embedding weights,
or task-specific query rules were added to the engine.

| Check on six questions | Previous token-only windows | New byte-limited windows |
|---|---:|---:|
| Target file present in top three semantic candidates | 5/6 | 5/6 |
| Target answer text present in the first read of at least one candidate | 3/6 | 5/6 |
| Source windows built for the eight files | 22 | 61 |

The comparison varies only window generation: both sides use the new candidate
and reader policy. It does not isolate the two-chunks-per-file change or compare
the complete old engine. New reads were at most 936 bytes and needed no
continuation. Word search found target files in 3/6 cases; semantic candidates
found two additional target files. The English question about a Korean quiet-hours
note still missed. These six checks demonstrate bounded readability, not general
answer accuracy or a new benchmark score. More windows also increase index size
and build work.

The real-source navigation checks locate `_read_text` and its caller and read
both locations through the same generation-bound ref. Unit and installation
checks exercise the new CLI and MCP fields directly. A ChatGPT-plan Codex native
tool-use smoke attempt was blocked before tool execution by the host's approval
policy; it provides no agent task-completion evidence. This limitation is retained
rather than counted as a pass.

No LongMemEval or external-system benchmark score is updated by this release.
Useful source access, preserved policy and bounded output are the acceptance
criteria. Translation misses and native-agent effectiveness remain open work.
