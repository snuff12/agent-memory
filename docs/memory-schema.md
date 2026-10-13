# Memory vault schema

The vault is durable, human-readable, and agent-independent. External source files remain canonical at paths in `config.json`; this vault stores only curated memory and derived wiki maps.

## Required frontmatter

```yaml
---
title: Human-readable title
kind: fact | preference | episode | procedure | decision | concept | entity | synthesis
status: inbox | reviewed | superseded
confidence: high | medium | low
source: canonical path, URL, user, or explicit observation
created: ISO-8601 timestamp
updated: ISO-8601 timestamp (after first edit)
scope: global | project:<name> | domain:<name>
sensitivity: private | shareable
valid_from: ISO-8601 timestamp or date (when time-bound)
review_after: ISO-8601 date (when volatile)
supersedes: relative vault path or empty
---
```

## Rules

- An inbox item is a candidate, not an established fact.
- Semantic claims need a source pointer. A synthesis cites every material source.
- Decisions record rationale, rejected alternatives, and revisit conditions.
- Episodes record when the observation was true.
- Preferences are scoped to the person, project, or situation that established them.
- Procedures record prerequisites, version assumptions, success checks, and failure modes.
- Volatile facts carry `review_after`; an overdue card is a retrieval lead, not current evidence.
- Corrections supersede; they do not silently rewrite history.
- Conflicting claims remain separate until evidence resolves them. Link both cards and lower confidence rather than merging prose.
- Wiki pages are navigation maps. Answers still verify canonical source spans.
- Never store credentials, private keys, raw authentication files, or secrets.
- Do not store full chat transcripts by default.

## Promotion contract

An inbox card becomes `reviewed` only after provenance, scope, sensitivity,
validity, and conflicts have been checked. Move it to the matching memory class,
append the action to `log.md`, update `index.md` only when the card is important
enough to route future work, and run `amem sync --collection vault`.

Supersession is append-only: create the replacement card, point its
`supersedes` field at the old card, mark the old card `superseded`, and retain
both rationales. Deleting a reviewed card is reserved for secrets or a genuine
erasure request; follow it with `amem compact` so deleted index text is scrubbed.
