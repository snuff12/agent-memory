#!/usr/bin/env python3
"""Timing-only probe for a resident model; not an agent-memory backend."""
import argparse
import json
import os
from pathlib import Path
import platform
import resource
import statistics
import time

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
os.environ["DO_NOT_TRACK"] = "1"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    args = parser.parse_args()
    started = time.perf_counter()
    import numpy as np
    from fastembed import TextEmbedding
    from fastembed.common.model_description import ModelSource, PoolingType
    from importlib.metadata import version
    workspace = args.workspace.resolve()
    frozen = json.loads((workspace / "frozen.json").read_text())
    state = workspace / "memory/.state/semantic"
    manifest = json.loads((state / "manifest.json").read_text())
    versions = {name: version(name) for name in ("fastembed", "tokenizers", "onnxruntime", "numpy")}
    assert versions == manifest["runtime_versions"]
    TextEmbedding.add_custom_model(model="intfloat/multilingual-e5-small", pooling=PoolingType.MEAN,
        normalization=True, sources=ModelSource(hf="intfloat/multilingual-e5-small"), dim=384,
        model_file="onnx/model.onnx", license="MIT")
    model = TextEmbedding(model_name="intfloat/multilingual-e5-small", specific_model_path=str(args.model.resolve()),
                          local_files_only=True, threads=4)
    with np.load(state / manifest["vectors"], allow_pickle=False) as data:
        vectors = data["vectors"]
    load_ms = (time.perf_counter() - started) * 1000
    rows = []
    for case in frozen["cases"]:
        started = time.perf_counter()
        vector = np.array(list(model.embed(["query: " + case["query"]]))[0], dtype=np.float32)
        scores = vectors @ vector
        ranking = np.argsort(-scores)
        elapsed = (time.perf_counter() - started) * 1000
        refs, seen = [], set()
        for index in ranking:
            ref = manifest["spans"][int(index)]["ref"]
            document = ref.split("@", 1)[0]
            if document not in seen:
                refs.append(ref)
                seen.add(document)
            if len(refs) == 3:
                break
        rows.append(dict(id=case["id"], embedding_and_rank_ms=elapsed, refs=refs))
    times = sorted(row["embedding_and_rank_ms"] for row in rows)
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    report = dict(scope="Embedding and vector rank only; excludes fingerprinting, source validation and fetch.",
                  model_load_ms=load_ms, queries=len(rows), median_ms=statistics.median(times),
                  p95_ms=times[min(len(times) - 1, int(len(times) * .95))],
                  process_peak_rss_bytes=peak if platform.system() == "Darwin" else peak * 1024,
                  runtime_versions=versions, records=rows)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
