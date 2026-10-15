"""Isolated optional runtime. Inference is local; model files must already exist."""
import json
import os
import sys
from pathlib import Path

MAX_BODY_BYTES = 512  # Leave 688 bytes for provenance in a normal 1,200-byte read.

os.umask(0o077)
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
os.environ["DO_NOT_TRACK"] = "1"


def windows(tokenizer, record):
    body = record["text"]
    header = "passage: %s\n%s\n" % (record["title"][:240], record["heading"][:240])
    # Bound the header independently; unusually long names cannot consume the body budget.
    while len(tokenizer.encode(header).ids) > 128:
        header = header[:len(header) // 2]
    tokens = tokenizer.encode(body, add_special_tokens=False)
    capacity = 512 - len(tokenizer.encode(header).ids) - 8
    start = 0
    if not tokens.offsets:
        if body.strip():
            raise ValueError("Tokenizer returned no offsets for nonempty evidence")
        return
    while start < len(tokens.offsets):
        stop = min(start + capacity, len(tokens.offsets))
        left = 0 if start == 0 else tokens.offsets[start][0]
        right = len(body) if stop == len(tokens.offsets) else tokens.offsets[stop - 1][1]
        while len(tokenizer.encode(header + body[left:right]).ids) > 512 and stop > start + 1:
            stop -= 1
            right = tokens.offsets[stop - 1][1]
        while len(body[left:right].encode("utf-8")) > MAX_BODY_BYTES and stop > start + 1:
            stop -= 1
            right = tokens.offsets[stop - 1][1]
        count = len(tokenizer.encode(header + body[left:right]).ids)
        if count > 512 or len(body[left:right].encode("utf-8")) > MAX_BODY_BYTES:
            raise ValueError("Embedding input exceeds its token/source-byte window")
        yield header + body[left:right], left, right, count
        if stop == len(tokens.offsets):
            break
        overlap = min(64, max(1, (stop - start) // 4))
        start = max(start + 1, stop - overlap)


def main():
    import numpy as np
    from tokenizers import Tokenizer
    from fastembed import TextEmbedding
    from fastembed.common.model_description import ModelSource, PoolingType

    from importlib import metadata
    versions = {name: metadata.version(name) for name in ("fastembed", "tokenizers", "onnxruntime", "numpy")}
    if versions["fastembed"] != "0.9.0":
        raise ValueError("Semantic runtime requires FastEmbed 0.9.0")
    request = json.load(sys.stdin)
    if request["action"] == "search" and request["runtime_versions"] != versions:
        raise ValueError("Semantic runtime changed; rebuild its vectors")
    model_path = Path(request["model_path"])
    TextEmbedding.add_custom_model(model="intfloat/multilingual-e5-small", pooling=PoolingType.MEAN,
        normalization=True, sources=ModelSource(hf="intfloat/multilingual-e5-small"), dim=384,
        model_file="onnx/model.onnx", license="MIT", size_in_gb=0.47)
    model = TextEmbedding(model_name="intfloat/multilingual-e5-small", specific_model_path=str(model_path),
        local_files_only=True, threads=4)
    tokenizer = Tokenizer.from_file(str(model_path / "tokenizer.json"))
    tokenizer.no_truncation()
    tokenizer.no_padding()
    if request["action"] == "build":
        texts, spans = [], []
        for record in request["records"]:
            coverage = []
            for text, left, right, count in windows(tokenizer, record):
                texts.append(text)
                spans.append({"ref": record["ref"], "start": left, "end": right, "tokens": count})
                if len(spans) > 32768:
                    raise ValueError("Semantic build exceeds 32768 windows; select smaller source directories")
                coverage.append((left, right))
            covered = {i for left, right in coverage for i in range(left, right)}
            if any(not char.isspace() and i not in covered for i, char in enumerate(record["text"])):
                raise ValueError("Embedding windows lost evidence characters")
        if not texts:
            raise ValueError("No nonempty semantic passages")
        vectors = np.array(list(model.embed(texts, batch_size=8)), dtype=np.float32)
        np.savez_compressed(request["vectors"], vectors=vectors)
        print(json.dumps({"spans": spans, "max_tokens": max(span["tokens"] for span in spans),
                          "max_body_bytes": MAX_BODY_BYTES, "runtime_versions": versions}))
    else:
        query = "query: " + request["query"]
        if len(tokenizer.encode(query).ids) > 512:
            raise ValueError("Semantic query exceeds 512 tokens")
        vector = np.array(list(model.embed([query]))[0], dtype=np.float32)
        with np.load(request["vectors"], allow_pickle=False) as data:
            scores = data["vectors"] @ vector
        print(json.dumps([[int(i), float(scores[i])] for i in np.argsort(-scores)]))


if __name__ == "__main__":
    main()
