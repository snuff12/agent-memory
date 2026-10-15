# Experimental local semantic retrieval

The public core uses lexical retrieval. Semantic retrieval is an optional pilot
available through an explicit `--semantic` search or the legacy
`semantic ... --experimental` commands. Similarity scores are not confidence scores; unrelated
questions can still return nearby documents. Read canonical spans before citing.

No runtime, model files, or vectors are distributed with this repository. The
pilot retains the engine's existing model adapter and requires these resources
under the private memory home:

- `runtime/semantic/bin/python`: a separate Python 3.10+ environment with
  `fastembed==0.9.0` and its dependencies.
- `runtime/models/multilingual-e5-small/`: the tokenizer, configuration, and
  `onnx/model.onnx` files for `intfloat/multilingual-e5-small`, revision
  `614241f622f53c4eeff9890bdc4f31cfecc418b3`.

Model downloads and licensing review are separate from core installation. The
worker sets offline/telemetry flags and requires local model files. It does not
download them automatically. Model and dependency fingerprints bind each vector
index to its build; changes require rebuilding.

```bash
amem semantic build --experimental --collection research --path-prefix selected-notes
amem search 'focused question' --collection research --semantic --limit 3 --json
amem read '<returned-ref>' --span-start 0 --span-end 200 --max-bytes 1200
```

Use the exact `span_start` and `span_end` values from search JSON for the read.
They are character offsets within a chunk, not within the whole source file.
`--path-prefix` selects a relative directory and can be repeated. Omitting it
selects the entire chosen collection, subject to the 4,096-chunk pilot cap.
Only enabled document collections are accepted; curated or personal memory
collections are excluded because this pilot does not implement their policy.

Search uses the collection selected at build time. MCP exposes the same explicit
option as `memory_search(query=..., collection=..., semantic=true)`; fetch its
`span_start`/`span_end` using `memory_fetch`. Without this option, regular search
and context keep the existing word/CJK retrieval. Semantic search cannot combine
with `follow_ref` or lexical `explain`, and does not record feedback.

The 0.3 worker keeps the existing 512-token model-input cap and additionally
limits each source body to 512 UTF-8 bytes. A bounded title/heading prefix also
counts toward the token cap. Adjacent windows overlap by at most 64 tokens or
one quarter of the current window, whichever is smaller. Full non-whitespace
source coverage is checked before embedding. This is for bounded source reading,
not model retraining. Results retain the best window per chunk and at most two
different chunks per file, with exact chunk character offsets. Metadata counts
toward fetch budgets, so long paths or extra policy metadata can still require
a continuation.

New builds use embedding policy version 2. Version 1 indexes remain readable;
rebuild the same selected scope to obtain smaller windows. A build does not sync
sources or widen a previous selection automatically; repeat the same collection
and path prefixes explicitly. Only one selected semantic index is active per
memory home. Vectors live in `.state/semantic/` and can be rebuilt.
Source edits require a new build. Previous vector files are retained for active
readers; remove the semantic state only after those readers stop.

Core unit tests cover stale references, span budgets, and model-policy changes.
They do not run model inference or establish semantic relevance quality. This
public preparation does not verify arbitrary newly downloaded runtimes. A later
[local diagnostic](../benchmarks/PRACTICAL_VARIANTS.md) exercised the pinned
prepared runtime/model with real inference, source reads, stale-source rejection,
and collection guards. It found limited recoveries and regressions, and does not
justify default integration or curated-memory support.

The [0.3 implementation check](navigation-and-semantic.md) records source-navigation
and semantic readability checks, including an unresolved translation miss. These
do not update the scores of earlier frozen benchmark revisions.
