# qmd component check, v1

Declared after the first full qmd pass, which recovered expression/language cases
but did not complete three-source or real-code cases. This is a follow-up
diagnostic on already seen data, not a new held-out result or model selection.

Run all 50 unchanged questions and the same 93-document corpus, source mapping,
three displayed candidates, 4,000-byte search display and three 1,200-byte reads.
Use the same installed qmd revision, state, model files and native defaults.
Do not select cases, alter expected evidence, tune weights or change models.

Compare native SDK searchLex (BM25), searchVector (embedding only), structured
lex+vec search with rerank=false (native RRF, no generated query expansion), and
the previously frozen full search (expansion and reranking). Use qmd's native
snippet location when a backend returns only a file, and its native chunk
position when available. This exposes configuration differences; it does not
attribute every gain uniquely because several stages differ in the full arm.
Keep native source mapping and display budgets outside the retrieval code.

Report exact-match regressions and public-source failures along with expression
recoveries. Whole-file retrieval is not equivalent to delivering both relevant
code spans. No component becomes a production default from this diagnostic.
