# Experimental local semantic retrieval

The public core uses lexical retrieval. Semantic retrieval is an optional pilot
behind `--experimental`. Similarity scores are not confidence scores; unrelated
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
amem semantic search 'focused question' --experimental --limit 3 --json
amem semantic read '<returned-ref>' --experimental \
  --span-start 0 --span-end 200 --max-tokens 1200
```

Use the exact `span_start` and `span_end` values from search JSON for the read.
They are character offsets within a chunk, not within the whole source file.
`--path-prefix` selects a relative directory and can be repeated. Omitting it
selects the entire chosen collection, subject to the 4,096-chunk pilot cap.
Only enabled document collections are accepted; curated or personal memory
collections are excluded because this pilot does not implement their policy.

Search uses the collection selected at build time. This does not change regular
`search`, `context`, or MCP. Vectors live in `.state/semantic/` and can be rebuilt.
Source edits require a new build. Previous vector files are retained for active
readers; remove the semantic state only after those readers stop.

Core unit tests cover stale references, span budgets, and model-policy changes.
They do not run model inference or establish semantic relevance quality. This
public preparation does not claim that a newly downloaded semantic runtime has
been verified end to end.
