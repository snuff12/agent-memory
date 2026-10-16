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

Key names are exact. A near-miss spelling of a lifecycle key, such as
`review-after`, `Review After`, `reviewafter`, `validFrom`, `Valid_From`, or
`supersede`, makes the card invalid instead of silently dropping its rule. The
check ignores case, spaces, dots, hyphens, and underscores; it does not catch
transposed letters such as `reveiw_after`. There is no `valid_to` or other
expiry key, and a card that sets `valid_to` in any of these spellings is invalid
because retrieval would not enforce it. End a card's validity by marking it
`superseded` or by setting `review_after`. Other extra keys are ignored when
every key is a unique string. A repeated key, or an unquoted key that YAML reads
as a number, boolean, null, or date (`2026:`, `on:`, `yes:`, `null:`,
`2026-01-01:`), also makes the card invalid, including inside a nested mapping;
quote such a key (`'2026': ...`) to keep it.

Timestamps need a timezone: `valid_from: 2026-10-01T09:00:00` is invalid,
while `2026-10-01T09:00:00+09:00` or a plain date is accepted. An
invalid card is excluded from default retrieval. `amem sync` prints how many
vault cards were excluded, `amem status` reports `invalid_memory_cards`
separately from extraction `errors`, and `amem doctor` lists each invalid card
by vault path with its validation error.

`amem capture` offers five kinds: fact, preference, episode, procedure, and
decision. Concept, entity, and synthesis cards are written by hand.

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
[What deletion removes](memory-lifecycle.md#what-deletion-removes) lists each
derived copy and its removal step.

Retrieval does not enforce `supersedes`; only the old card's `superseded`
status hides it. `amem doctor` checks the link read-only: it warns when a
reviewed or superseded card's `supersedes` target, a path relative to the vault
root such as `decisions/old.md`, is not an indexed vault card or is not marked
`superseded`. It also counts reviewed cards past `review_after`. These warnings
never edit cards or change doctor's exit status.
