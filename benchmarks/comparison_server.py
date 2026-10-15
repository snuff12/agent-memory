"""Loopback-only transport for native benchmark clients; not a product API."""
import argparse
import json
import os
from pathlib import Path
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from benchmarks.codex_inference import CodexInference


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--model-path", type=Path, required=True)
    parser.add_argument("--port", type=int, default=55961)
    parser.add_argument("--codex-model", default="gpt-6.1-sol")
    args = parser.parse_args()
    os.environ.update(HF_HUB_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1", DO_NOT_TRACK="1")
    from fastembed import TextEmbedding
    from fastembed.common.model_description import ModelSource, PoolingType
    from tokenizers import Tokenizer
    TextEmbedding.add_custom_model(model="intfloat/multilingual-e5-small", pooling=PoolingType.MEAN,
        normalization=True, sources=ModelSource(hf="intfloat/multilingual-e5-small"), dim=384,
        model_file="onnx/model.onnx", license="MIT")
    embedder = TextEmbedding(model_name="intfloat/multilingual-e5-small", specific_model_path=str(args.model_path),
                            local_files_only=True, threads=4)
    tokenizer = Tokenizer.from_file(str(args.model_path / "tokenizer.json"))
    tokenizer.no_truncation()
    inference = CodexInference(args.workspace, args.codex_model)
    embedding_lock = threading.Lock()
    log_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *unused):
            pass

        def reply(self, status, body):
            data = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self.reply(200, {"status": "ready", "codex_model": args.codex_model, "embedding_dim": 384})

        def do_POST(self):
            started = time.perf_counter()
            try:
                size = int(self.headers.get("Content-Length", 0))
                if not 0 < size <= 2_000_000:
                    raise ValueError("Invalid request size")
                data = json.loads(self.rfile.read(size))
                if self.path.endswith("/embeddings"):
                    texts = data["input"]
                    if isinstance(texts, str):
                        texts = [texts]
                    if any(not isinstance(text, str) for text in texts):
                        raise ValueError("Only text embedding inputs supported")
                    prefix = "query: " if data.get("model", "").endswith("-query") else "passage: "
                    inputs = [prefix + text for text in texts]
                    token_counts = [len(tokenizer.encode(text).ids) for text in inputs]
                    with embedding_lock:
                        vectors = [vector.tolist() for vector in embedder.embed(inputs, batch_size=8)]
                    response = dict(object="list", model=data.get("model"),
                        data=[dict(object="embedding", index=i, embedding=v) for i, v in enumerate(vectors)],
                        usage=dict(prompt_tokens=sum(token_counts), total_tokens=sum(token_counts)))
                    log = dict(kind="embedding", inputs=len(texts), tokens=sum(token_counts),
                               truncated_inputs=sum(count > 512 for count in token_counts))
                elif self.path.endswith("/chat/completions"):
                    if data.get("stream"):
                        raise ValueError("Streaming is unsupported by this diagnostic transport")
                    response_format = data.get("response_format", {})
                    schema = response_format.get("json_schema", {}).get("schema")
                    result = inference.complete(data["messages"], schema=schema,
                                                tools=data.get("tools"), purpose="native-client")
                    content, tool_calls = result["text"], None
                    if data.get("tools"):
                        parsed = json.loads(content)
                        content = parsed.get("content")
                        tool_calls = [dict(id="call_%s_%d" % (result["request_hash"][:12],i), type="function", function=dict(
                            name=call["name"], arguments=json.dumps(call["arguments"], ensure_ascii=False)))
                            for i, call in enumerate(parsed.get("tool_calls", []))]
                    usage = result["usage"]
                    response = dict(id="bench-" + result["request_hash"][:16], object="chat.completion",
                        created=int(time.time()), model=args.codex_model,
                        choices=[dict(index=0, finish_reason="tool_calls" if tool_calls else "stop",
                            message=dict(role="assistant", content=content, **({"tool_calls": tool_calls} if tool_calls else {})))],
                        usage=dict(prompt_tokens=usage.get("input_tokens", 0), completion_tokens=usage.get("output_tokens", 0),
                                   total_tokens=usage.get("input_tokens", 0)+usage.get("output_tokens", 0)))
                    log = dict(kind="codex", request_hash=result["request_hash"], usage=usage,
                               requested_max_tokens=data.get("max_tokens", data.get("max_completion_tokens")),
                               note="CLI reasoning=low; native temperature/max_tokens are not exact CLI decoding controls")
                else:
                    raise ValueError("Unsupported endpoint")
                log["elapsed_ms"] = (time.perf_counter()-started)*1000
                with log_lock:
                    with (args.workspace / "logs/transport.jsonl").open("a") as stream:
                        stream.write(json.dumps(log) + "\n")
                self.reply(200, response)
            except Exception as exc:
                self.reply(500, {"error": {"message": str(exc), "type": type(exc).__name__}})

    print("loopback transport ready", flush=True)
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
