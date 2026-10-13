#!/usr/bin/env python3
"""Exercise the installed CLI from an unrelated cwd using synthetic private data."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--command", type=Path, required=True, help="Installed amem console command or shell launcher")
    args = parser.parse_args()
    command = args.command.resolve()
    with tempfile.TemporaryDirectory(prefix="amem-public-check-") as temporary:
        root = Path(temporary)
        home = root / "private"
        environment = dict(os.environ)
        environment.pop("PYTHONPATH", None)
        environment.pop("PYTHONHOME", None)
        environment.pop("AMEM_HOME", None)

        def run(*arguments: str, expected: int = 0) -> str:
            completed = subprocess.run([str(command), "--home", str(home), *arguments],
                                       cwd=root, env=environment, capture_output=True, text=True, timeout=60)
            if completed.returncode != expected:
                raise RuntimeError("%s failed (%d): %s" % (arguments[0], completed.returncode, completed.stderr))
            return completed.stdout

        run("init", "--demo")
        assert json.loads(run("doctor", "--json"))["ok"]
        before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in (home / "knowledge").iterdir()}
        run("sync")
        run("status", "--require-fresh")
        for query in ("opaque references", "간격 반복 학습"):
            hits = json.loads(run("search", query, "--collection", "research", "--json"))["results"]
            assert hits, query
            excerpt = run("read", hits[0]["ref"], "--max-tokens", "1200")
            assert query.split()[0] in excerpt
            assert len(excerpt.rstrip("\n").encode("utf-8")) <= 1200
        result = json.loads(run("eval", "--json"))
        assert result["passed_cases"] == 3 and result["negative_cases"] == 1
        run("capture", "--kind", "decision", "--title", "Synthetic inbox marker",
            "--body", "publicdemoinboxmarker", "--source", "synthetic-test")
        assert not json.loads(run("search", "publicdemoinboxmarker", "--collection", "vault", "--json"))["results"]
        inbox = json.loads(run("search", "publicdemoinboxmarker", "--collection", "vault", "--memory-status", "inbox", "--json"))["results"]
        assert inbox
        run("read", inbox[0]["ref"], expected=2)
        assert "publicdemoinboxmarker" in run("read", inbox[0]["ref"], "--memory-status", "inbox")
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "memory_search", "arguments": {"query": "opaque references", "collection": "research", "limit": 3}}},
        ]
        mcp = subprocess.run([str(command), "--home", str(home), "mcp"], cwd=root, env=environment,
                             input="".join(json.dumps(item) + "\n" for item in messages), capture_output=True, text=True, timeout=60)
        assert mcp.returncode == 0, mcp.stderr
        responses = [json.loads(line) for line in mcp.stdout.splitlines()]
        assert len(responses) == 3 and all("result" in item for item in responses), responses
        assert {item["name"] for item in responses[1]["result"]["tools"]} == {"memory_catalog", "memory_search", "memory_fetch"}
        assert not responses[2]["result"].get("isError", False), responses[2]
        after = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in (home / "knowledge").iterdir()}
        assert before == after, "Source documents were changed"
        print("PASS: init, doctor, sync, bilingual search/read, eval, inbox policy, stdio MCP, read-only source files")


if __name__ == "__main__":
    main()
