# Exact-symbol SCIP integration check

Declared before running the integration arm. The standalone index coverage check
does not show whether actual source spans reach a reader. Therefore also test a
small adapter over the actual generated SCIP index, with lexical fallback.

When the query contains an exact whole function name present in SCIP, return its
native definition/reference locations, definitions first and then references in
path/line order. Map locations to current allowed source chunks; deduplicate with
the existing two-chunk-per-file/20-candidate cap. Append the unchanged lexical
ranking as fallback. Queries without an exact symbol use the lexical engine.
No aliases, gold paths, answer text, category switches or learned weights.

Run every one of the unchanged 50 questions, including non-code, ambiguities,
absent answers and new public-source tasks. Use the same common source budgets.
Also run the unchanged common controller subset. This is a local integration
prototype using Sourcegraph's real indexer, not a run of Cody or a general
natural-language SCIP retriever. Report it under its own name. The source pool
contains only the frozen seven Python files; unresolved external imports and
cross-project references are outside this test's coverage.

This follow-up was chosen to investigate code navigation, not by changing cases
or maximizing a target score. Preserve failed results. Verify useful new cases
and any regressions before recommending adoption; a tiny code sample alone does
not justify indexing every repository or enabling an automatic production path.
