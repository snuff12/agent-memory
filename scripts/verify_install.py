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
        (home / "knowledge/owner.md").write_text("SyntheticOwner maintains BridgeProject.\n")
        (home / "knowledge/store.md").write_text("BridgeProject persists in SyntheticStore.\n")
        (home / "knowledge/obsolete.md").write_text("syntheticexcludedneedle evidence\n")
        before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in (home / "knowledge").iterdir()}
        run("sync")
        run("status", "--require-fresh")
        for query in ("opaque references", "간격 반복 학습"):
            report = json.loads(run("search", query, "--collection", "research", "--explain", "--json"))
            hits = report["results"]
            assert hits, query
            assert report["explanation"]["selected_terms"]
            excerpt = run("read", hits[0]["ref"], "--max-tokens", "1200")
            assert query.split()[0] in excerpt
            assert len(excerpt.rstrip("\n").encode("utf-8")) <= 1200
            focused = run("read", hits[0]["ref"], "--query", query, "--max-tokens", "1200")
            assert query.split()[0] in focused and "chunk chars" in focused
            assert len(focused.rstrip("\n").encode("utf-8")) <= 1200
        owner = json.loads(run("search", "SyntheticOwner", "--collection", "research", "--limit", "1", "--json"))["results"][0]
        assert "BridgeProject" in run("read", owner["ref"], "--max-tokens", "1200")
        following = json.loads(run("search", "BridgeProject", "--follow-ref", owner["ref"], "--limit", "1", "--json"))
        assert following["followup"]["anchor"]["ref"] == owner["ref"]
        assert following["results"][0]["relative_path"] == "store.md"
        assert "SyntheticStore" in run("read", following["results"][0]["ref"], "--max-tokens", "1200")
        located = run("locate", owner["ref"], "BridgeProject", "--max-bytes", "1200")
        assert "1: SyntheticOwner maintains BridgeProject." in located
        source_lines = run("read", owner["ref"], "--start-line", "1", "--end-line", "1", "--max-bytes", "1200")
        assert "1: SyntheticOwner maintains BridgeProject." in source_lines
        span = run("read", owner["ref"], "--span-start", "0", "--span-end", "14", "--max-bytes", "1200")
        assert "SyntheticOwner" in span and "chunk chars 0:14" in span
        for text in (located, source_lines, span):
            assert len(text.rstrip("\n").encode("utf-8")) <= 1200
        result = json.loads(run("eval", "--json"))
        assert result["passed_cases"] == 3 and result["negative_cases"] == 1
        run("capture", "--kind", "decision", "--title", "Synthetic inbox marker",
            "--body", "publicdemoinboxmarker", "--source", "synthetic-test")
        assert not json.loads(run("search", "publicdemoinboxmarker", "--collection", "vault", "--json"))["results"]
        inbox = json.loads(run("search", "publicdemoinboxmarker", "--collection", "vault", "--memory-status", "inbox", "--json"))["results"]
        assert inbox
        run("read", inbox[0]["ref"], expected=2)
        assert "publicdemoinboxmarker" in run("read", inbox[0]["ref"], "--memory-status", "inbox")
        feedback = json.loads(run("feedback", "report", "synthetic evidence gap", "--collection", "research",
                                  "--expected-path", "owner.md", "--json"))
        reviewed = json.loads(run("feedback", "triage", str(feedback["id"]), "--category", "retrieval_miss",
                                  "--notes", "Synthetic source exists but the required evidence is absent.", "--json"))
        assert reviewed["status"] == "open" and not reviewed["needs_triage"]
        stats = json.loads(run("feedback", "stats", "--json"))
        assert stats["records"] == 1 and stats["occurrences"] == 1 and stats["open_pending_triage"] == 0
        assert stats["by_category"][0]["value"] == "retrieval_miss"
        classified = json.loads(run("feedback", "list", "--category", "retrieval_miss", "--json"))
        assert classified[0]["id"] == feedback["id"]
        exported = json.loads(run("feedback", "export", "--format", "review"))
        assert exported["triage_notes"] == reviewed["triage_notes"]
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "memory_search", "arguments": {"query": "opaque references", "collection": "research", "limit": 3, "explain": True}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "memory_fetch", "arguments": {"ref": hits[0]["ref"], "query": "간격 반복", "max_tokens": 1200}}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "memory_search", "arguments": {"query": "BridgeProject", "follow_ref": owner["ref"], "limit": 1}}},
            {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "memory_locate", "arguments": {"ref": owner["ref"], "query": "BridgeProject", "max_bytes": 1200}}},
            {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "memory_fetch", "arguments": {"ref": owner["ref"], "start_line": 1, "end_line": 1, "max_bytes": 1200}}},
            {"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"name": "memory_fetch", "arguments": {"ref": owner["ref"], "span_start": 0, "span_end": 14, "max_bytes": 1200}}},
        ]
        mcp = subprocess.run([str(command), "--home", str(home), "mcp"], cwd=root, env=environment,
                             input="".join(json.dumps(item) + "\n" for item in messages), capture_output=True, text=True, timeout=60)
        assert mcp.returncode == 0, mcp.stderr
        responses = [json.loads(line) for line in mcp.stdout.splitlines()]
        assert len(responses) == 8 and all("result" in item for item in responses), responses
        assert {item["name"] for item in responses[1]["result"]["tools"]} == {"memory_catalog", "memory_search", "memory_fetch", "memory_locate"}
        assert not responses[2]["result"].get("isError", False), responses[2]
        assert "SEARCH EXPLANATION" in responses[2]["result"]["content"][0]["text"]
        assert "chunk chars" in responses[3]["result"]["content"][0]["text"]
        assert "FOLLOW-UP SEARCH" in responses[4]["result"]["content"][0]["text"]
        assert following["results"][0]["ref"] in responses[4]["result"]["content"][0]["text"]
        assert "1: SyntheticOwner maintains BridgeProject." in responses[5]["result"]["content"][0]["text"]
        assert "1: SyntheticOwner maintains BridgeProject." in responses[6]["result"]["content"][0]["text"]
        assert "chunk chars 0:14" in responses[7]["result"]["content"][0]["text"]
        for response in responses[5:]:
            assert not response["result"].get("isError", False), response
            assert len(response["result"]["content"][0]["text"].encode("utf-8")) <= 1200
        config_file = home / "config.json"
        config = json.loads(config_file.read_text())
        next(source for source in config["sources"] if source["name"] == "research")["exclude_globs"] = ["obsolete.md"]
        config_file.write_text(json.dumps(config))
        preview = json.loads(run("migrate-exclusions", "--collection", "research", "--dry-run"))
        assert preview["reusable"] >= 4 and preview["excluded"] == 1 and preview["adopted"] == 0
        reused = json.loads(run("migrate-exclusions", "--collection", "research"))
        assert reused["adopted"] == preview["reusable"]
        run("sync", "--collection", "research")
        run("status", "--require-fresh")
        assert not json.loads(run("search", "syntheticexcludedneedle", "--collection", "research", "--explain", "--json"))["results"]
        assert "SyntheticOwner" in run("read", owner["ref"], "--max-tokens", "1200")
        after = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in (home / "knowledge").iterdir()}
        assert before == after, "Source documents were changed"
        print("PASS: init, doctor, sync, bilingual search/read/focus, source-bound follow-up, locate/lines/character spans, explanations, eval, inbox policy, feedback triage/stats/export, exclusion reuse, stdio MCP, read-only source files")


if __name__ == "__main__":
    main()
