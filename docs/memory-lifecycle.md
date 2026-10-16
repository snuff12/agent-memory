# Memory lifecycle

The document index and durable agent memory solve different problems. Research,
code, papers, and project files stay canonical where they already live. The
vault stores only small, explicitly selected facts, preferences, episodes,
procedures, decisions, and synthesis maps.

## Write path

```text
explicit user request
        │
        ▼
amem capture → vault/inbox (candidate, not fact)
        │ human/agent review
        ▼
provenance + scope + validity + conflict check
        │
        ├─ reject / erase
        ├─ retain in inbox
        └─ promote to semantic / episodic / procedural / decisions
                         │
                         ▼
             vault index + append-only log
```

There is no automatic conversation ingestion. This prevents low-value chat,
prompt injection, and accidental secrets from becoming durable context.

## Retrieval classes

| Class | Use | Required qualification |
|---|---|---|
| Semantic | stable fact, concept, entity, preference | source, scope, confidence, review date if volatile |
| Episodic | event or observation | actor, time, context, what was directly observed |
| Procedural | repeatable workflow | prerequisites, versions, success check, failure modes |
| Decision | chosen direction | rationale, evidence, rejected options, revisit trigger |
| Wiki/synthesis | navigation across sources | citations for every material claim; never final evidence by itself |

## Conflict and expiry

- Never silently overwrite a reviewed memory.
- A correction creates a new card and explicitly supersedes the old path.
- An overdue `review_after` makes the card stale for current claims but does not
  erase its historical value.
- User statements outrank agent inference about the user's own preference, but
  project facts still require canonical evidence.
- Project-scoped facts do not become global preferences.

## Card health

Card validation fails closed. A card with malformed metadata, a misspelled
lifecycle key such as `review-after`, `Review After`, or `validFrom`, an
unsupported `valid_to` expiry, or a `valid_from` timestamp without a timezone is
excluded from default retrieval rather than served without its rule. Key
spelling is compared without case, spaces, dots, hyphens, and underscores;
transposed letters are not caught. `amem sync` prints a one-line count of
excluded vault cards, `amem status` shows `invalid_memory_cards` apart from extraction
`errors`, and `amem doctor` lists each invalid card by vault path with its
validation error. Doctor also warns about a `supersedes` target that is missing
or not marked `superseded`, and counts reviewed cards past `review_after`. The
report is read-only and prints vault paths and validation errors, not card
text. Its warnings do not make doctor fail, because they concern vault content,
not installation safety.

## Upgrades

Run `amem sync` right after upgrading the engine. An upgrade that changes card
validation keeps cards excluded until the next sync re-validates them; doctor
counts them as `awaiting_validation_by_sync` meanwhile. An upgrade that changes
a file type's chunking or extraction policy hides those documents from search,
read, and locate until sync re-indexes them, and their earlier refs fail as
stale; search again after syncing. Fence-aware sectioning is such a change: it
applies to Markdown, Org, and reStructuredText files, including every vault
card. `status` counts both states as `outdated_documents` and reports the
index as not fresh, so `status --require-fresh` fails, and `ensure-fresh`
re-syncs an affected collection even within its age SLA. Plain `search` does
not sync by itself. `amem migrate-projections` reports these rows as
`needs_rebuild`.

## Deletion boundary

Removing a canonical source or vault card makes search fail closed immediately
and the next sync removes its manifest rows. Because database pages and FTS
segments can retain deleted bytes, this installation enables SQLite and FTS5
secure deletion. Run `amem compact` after a secret-removal or erasure event, and
remove obsolete index backups; filesystem snapshots and external backups remain
separate retention domains.

### What deletion removes

The engine never deletes canonical files; the user does. Each derived copy
below has its own removal step.

| Location | After the file is deleted | Remaining step |
|---|---|---|
| Canonical source or vault card | Search, read, and locate recheck size, mtime, and SHA-256, so old hits and refs fail closed immediately. | Also remove the copies below. |
| Index rows | The next complete `sync` of the collection deletes its document and chunk rows. An incomplete scan preserves unvisited rows and fails the freshness gate. | `amem sync --collection NAME` until it reports no incomplete traversal. |
| FTS segments and SQLite free pages | `storage.secure_delete` (default on; SQLite 3.42.0 or newer) enables SQLite and FTS5 secure deletion. | `amem compact` optimizes the FTS projections and rewrites the active index. Delete old index copies that `amem doctor` reports as `orphan_indexes`. |
| Index WAL | Recent page images can remain until a checkpoint. | `compact` checkpoints and truncates the WAL. It refuses while a reader holds the WAL; stop MCP servers and other readers, then retry. |
| `.state/semantic/` manifest and `.npz` files | Semantic search skips the deleted source, but the active vector file keeps its embeddings, and previous vector files are retained for active readers. These files hold vectors, refs, offsets, and selected path prefixes, not text. | After semantic readers stop, delete `.state/semantic/` and rebuild if needed. A rebuild alone keeps earlier vector files. |
| `feedback/query_failures.sqlite3` | Query text (secret-like queries are redacted best-effort), collection, expected path, and notes remain. With opt-in zero-result feedback, a search for the deleted text can itself add a row. The queue uses secure deletion without a WAL. | `amem feedback resolve ID`; `amem feedback prune` then removes resolved or ignored rows older than `resolved_retention_days` (default 180) and, beyond `max_records`, the oldest rows, closed ones before open ones. There is no per-row delete; removing the file while no `amem` process is running drops the whole queue. |
| Eval gold files | `eval/gold_queries.jsonl`, other `amem eval --file` sets, and `feedback export --output` files hold queries and expected paths. | User-maintained; edit or delete them. |
| `.state/logs/` | Optional launchd sync logs keep sync counts and error messages, which can name paths, plus one rotated `.1` copy per log. | Delete the log files. |
| `vault/inbox/` capture files | `amem capture` writes a candidate whose filename includes a title slug. Promotion moves it; a copied or rejected candidate remains. `log.md` or `index.md` lines can also quote the card. | Delete or edit those files, then run `amem sync --collection vault`. |
| Git history, Time Machine, and other backups of the memory home | Outside the engine. | Purge them separately. |
