"""Source-use regressions, independent of benchmark questions and scores."""
from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_memory.cli import main
import agent_memory.search as search_module
from agent_memory.config import load_config
from agent_memory.indexer import sync_all
from agent_memory.memory_policy import MemoryPolicy
from agent_memory.mcp_server import handle_request
from agent_memory.search import SearchDiagnostics, StaleReferenceError, fetch, format_search, search
from tests.test_memory_policy import card
from tests.test_memory_system import make_home


def call_tool(config, name, **arguments):
    return handle_request(config, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                  "params": {"name": name, "arguments": arguments}})


class PracticalRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.home = make_home(self.root, self.source)
        self.config = load_config(self.home)

    def test_long_project_question_preserves_named_evidence(self):
        background = "please inspect these general project notes before explaining the current deployment workflow"
        (self.source / "background.txt").write_text(background)
        evidence = {
            "ARCHITECTURE.md": "Authority boundaries and immutable state.",
            "decision_log.md": "Production changes require review.",
            "incident.txt": "INC-7842 was caused by an expired certificate.",
            "handler.py": "def commit_receipt():\n    return 'durable'\n",
            "policy.txt": "The aquamarine transfer gate protects settlement.",
            "store.txt": "openConnection uses the configured store.",
        }
        for name, text in evidence.items():
            (self.source / name).write_text(text)
        sync_all(self.config)
        for tail, expected in (("ARCHITECTURE.md", "ARCHITECTURE.md"), ("decision_log.md", "decision_log.md"), ("INC-7842", "incident.txt"),
                               ("commit_receipt", "handler.py"), ('"aquamarine transfer"', "policy.txt"),
                               ("openConnection", "store.txt")):
            with self.subTest(tail=tail):
                trace = SearchDiagnostics()
                hits = search(self.config, background + " " + tail, collection="test", diagnostics=trace)
                self.assertIn(expected, [hit.relative_path for hit in hits])
                self.assertLessEqual(len(trace.selected_terms), 12)
                self.assertGreater(trace.omitted_count, 0)
                self.assertIn(evidence[expected].splitlines()[0], fetch(self.config, next(
                    hit.ref for hit in hits if hit.relative_path == expected)))

    def test_unquoted_distinctive_word_at_the_end_is_not_discarded(self):
        (self.source / "target.txt").write_text("The aquamarine protocol is documented here.")
        sync_all(self.config)
        query = "please check these old general project meeting notes and explain what happened with aquamarine"
        self.assertEqual(search(self.config, query, collection="test")[0].relative_path, "target.txt")

    def test_code_review_reads_both_definition_and_callsite(self):
        # Real public source, indexed in an isolated home. Finding the file ID
        # alone is not enough to review a function's behavior and its caller.
        selected = replace(self.config.sources[0], path=Path(search_module.__file__).resolve())
        self.config = replace(self.config, sources=(selected,), chunk_chars=2400)
        sync_all(self.config)
        question = "Please inspect the retrieval implementation and explain how a late source passage is chosen by _focus_span"
        for query in (question, question + " in search.py"):
            with self.subTest(query=query):
                hits = search(self.config, query, limit=3)
                excerpts = [fetch(self.config, hit.ref, query="_focus_span", max_tokens=1200) for hit in hits]
                self.assertTrue(any("def _focus_span(" in excerpt for excerpt in excerpts))
                self.assertTrue(any("focused = _focus_span" in excerpt for excerpt in excerpts))
                self.assertTrue(all(len(excerpt.encode("utf-8")) <= 1200 for excerpt in excerpts))

    def test_focused_read_reaches_late_evidence_under_the_same_budget(self):
        text = "Protocol introduction.\n" + "Operational background without the answer.\n" * 45
        text += "Retention period: 37 days.\n한글 보존 기간: 37일입니다.\n" + "Appendix background.\n" * 30
        target = self.source / "protocol.txt"
        target.write_text(text)
        self.config = replace(self.config, chunk_chars=8000)
        sync_all(self.config)
        hit = search(self.config, "Retention", collection="test")[0]
        ordinary = fetch(self.config, hit.ref, max_tokens=1200)
        self.assertNotIn("37 days", ordinary)
        for query, answer in (("retention period", "37 days"), ("보존 기간", "37일")):
            with self.subTest(query=query):
                focused = fetch(self.config, hit.ref, max_tokens=1200, query=query)
                self.assertIn(answer, focused)
                self.assertIn("chunk chars", focused)
                self.assertIn("omitted before: yes", focused)
                self.assertIn(hit.ref, focused)
                self.assertIn(str(target.resolve()), focused)
                self.assertIn("SOURCE_UNTRUSTED", focused)
                self.assertLessEqual(len(focused.encode("utf-8")), 1200)
        self.assertEqual(target.read_text(), text)
        self.assertIn("no literal match", fetch(self.config, hit.ref, query="absentword"))
        response = call_tool(self.config, "memory_fetch", ref=hit.ref, query="retention period", max_tokens=1200)
        self.assertIn("37 days", response["result"]["content"][0]["text"])

    def test_focused_read_keeps_scope_and_source_version_checks(self):
        target = self.home / "vault" / "target.md"
        target.write_text(card(scope="project:demo"))
        sync_all(self.config)
        policy = MemoryPolicy(project="demo")
        hit = search(self.config, "policyneedle", collection="vault", policy=policy)[0]
        with self.assertRaises(StaleReferenceError):
            fetch(self.config, hit.ref, query="policyneedle")
        self.assertIn("policyneedle", fetch(self.config, hit.ref, query="policyneedle", policy=policy))
        target.write_text(card(scope="project:demo") + "changed")
        with self.assertRaises(StaleReferenceError):
            fetch(self.config, hit.ref, query="policyneedle", policy=policy)

    def test_focus_rejects_ambiguous_navigation_and_invalid_tool_types(self):
        for arguments in ({"query": "needle", "around": 1}, {"query": "needle", "part": 2},
                          {"query": 7}, {"query": " "}, {"query": "x" * 2001}):
            response = call_tool(self.config, "memory_fetch", ref="opaque", **arguments)
            self.assertEqual(response["error"]["code"], -32602)
        response = call_tool(self.config, "memory_search", query="needle", explain="true")
        self.assertEqual(response["error"]["code"], -32602)

    def test_explanation_distinguishes_changed_sources_without_disclosing_denied_cards(self):
        target = self.source / "target.txt"
        target.write_text("literalneedle")
        (self.home / "vault" / "private-card.md").write_text(card(scope="project:secret") + "privateonlyneedle")
        sync_all(self.config)
        trace = SearchDiagnostics()
        target.write_text("literalneedle changed")
        self.assertFalse(search(self.config, "literalneedle", collection="test", diagnostics=trace))
        self.assertEqual(trace.unreadable_or_changed_documents, 1)
        self.assertGreater(trace.candidates, 0)
        hits = search(self.config, "privateonlyneedle", collection="vault", diagnostics=trace)
        self.assertFalse(hits)
        self.assertEqual(trace.candidates, 0)
        self.assertEqual(trace.checked_documents, 0)
        self.assertEqual(trace.unreadable_or_changed_documents, 0)
        rendered = format_search(hits, diagnostics=trace, as_json=True)
        self.assertNotIn("private-card", rendered)
        self.assertNotIn("project:secret", rendered)
        self.assertEqual(json.loads(rendered)["explanation"]["selected_terms"], ["privateonlyneedle"])
        for budget in (0, 1, 2, 10, 128, 1200, 4000):
            self.assertLessEqual(len(format_search(hits, diagnostics=trace, max_units=budget).encode("utf-8")), budget)

    def test_diagnostic_cli_and_mcp_do_not_write_opt_in_feedback(self):
        sync_all(self.config)
        stdout = io.StringIO()
        with patch("agent_memory.cli._try_record_zero_result") as cli_record, redirect_stdout(stdout):
            self.assertEqual(main(["--home", str(self.home), "search", "absentword", "--explain", "--json"]), 0)
        cli_record.assert_not_called()
        self.assertIn("explanation", json.loads(stdout.getvalue()))
        with patch("agent_memory.mcp_server.record_zero_result") as mcp_record:
            response = call_tool(self.config, "memory_search", query="absentword", explain=True)
        mcp_record.assert_not_called()
        self.assertIn("SEARCH EXPLANATION", response["result"]["content"][0]["text"])

    def test_followup_search_connects_sources_without_crossing_project_scope(self):
        (self.home / "vault" / "owner.md").write_text(card(scope="project:demo") + "Mina maintains Aurora.")
        (self.home / "vault" / "store.md").write_text(card(scope="project:demo") + "Aurora persists in CockroachDB.")
        (self.home / "vault" / "other.md").write_text(card(scope="project:other") + "Aurora persists in Redis.")
        sync_all(self.config)
        policy = MemoryPolicy(project="demo")
        first = search(self.config, "Mina database", collection="vault", policy=policy)
        self.assertEqual({hit.relative_path for hit in first}, {"owner.md"})
        self.assertIn("Aurora", fetch(self.config, first[0].ref, policy=policy, query="Mina"))
        second = search(self.config, "Aurora", collection="vault", policy=policy)
        self.assertEqual({hit.relative_path for hit in second}, {"owner.md", "store.md"})
        store = next(hit for hit in second if hit.relative_path == "store.md")
        self.assertIn("CockroachDB", fetch(self.config, store.ref, policy=policy, query="Aurora"))

    def test_version_review_exposes_dates_without_promoting_previous_decisions(self):
        vault = self.home / "vault"
        (vault / "old.md").write_text(card(status="superseded", valid_from="2000-01-01", review_after="2001-01-01") + "Retention was 14 days.")
        (vault / "current.md").write_text(card(valid_from="2002-01-01", review_after="2999-01-01") + "Retention is 37 days.")
        before = {path.name: path.read_bytes() for path in vault.glob("*.md")}
        sync_all(self.config)
        self.assertEqual({hit.relative_path for hit in search(self.config, "Retention", collection="vault")}, {"current.md"})
        policy = MemoryPolicy(status="all")
        hits = search(self.config, "Retention", collection="vault", policy=policy)
        self.assertEqual({hit.relative_path for hit in hits}, {"old.md", "current.md"})
        for hit in hits:
            self.assertTrue(hit.memory_valid_from)
            self.assertTrue(hit.memory_review_after)
            rendered = format_search([hit], max_units=1200)
            self.assertIn("valid_from:", rendered)
            self.assertIn("review_after:", rendered)
            excerpt = fetch(self.config, hit.ref, policy=policy, query="Retention")
            self.assertIn(hit.memory_status, excerpt)
            self.assertIn(hit.memory_valid_from, excerpt)
        self.assertEqual(before, {path.name: path.read_bytes() for path in vault.glob("*.md")})


if __name__ == "__main__":
    unittest.main()
