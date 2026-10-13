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

## Deletion boundary

Removing a canonical source or vault card makes search fail closed immediately
and the next sync removes its manifest rows. Because database pages and FTS
segments can retain deleted bytes, this installation enables SQLite and FTS5
secure deletion. Run `amem compact` after a secret-removal or erasure event, and
remove obsolete index backups; filesystem snapshots and external backups remain
separate retention domains.
