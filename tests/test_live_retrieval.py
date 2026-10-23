from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import agent_memory.search as retrieval
from agent_memory.config import load_config
from agent_memory.indexer import sync_all
from agent_memory.mcp_server import run_server
from tests.test_memory_policy import card
from tests.test_memory_system import make_home


class LiveRetrievalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.home = make_home(self.root, self.source)
        self.config = load_config(self.home)

    def stale_candidates(self):
        old = []
        for index in range(60):
            path = self.source / ("old-%d.md" % index)
            path.write_text("quartzmarker\n" * 3)
            old.append(path)
        (self.source / "current.md").write_text("quartzmarker " + "background " * 25)
        (self.home / "vault" / "denied.md").write_text(card(scope="project:secret") + "quartzmarker\n")
        sync_all(self.config)
        for path in old:
            path.write_text("replaced")

    def test_refill_finds_current_evidence_without_reading_denied_cards(self):
        self.stale_candidates()
        trace = retrieval.SearchDiagnostics()
        original = retrieval._validate_document
        with patch.object(retrieval, "_validate_document", wraps=original) as validate:
            hits = retrieval.search(self.config, "quartzmarker", limit=3, diagnostics=trace)
        self.assertEqual([hit.relative_path for hit in hits], ["current.md"])
        self.assertEqual(trace.rounds, 2)
        self.assertFalse(trace.refill_exhausted)
        self.assertEqual(trace.unreadable_or_changed_documents, 60)
        paths = [call.args[0]["path"] for call in validate.call_args_list]
        self.assertEqual(len(paths), len(set(paths)))
        self.assertNotIn(str(self.home / "vault" / "denied.md"), paths)
        self.assertIn("quartzmarker", retrieval.fetch(self.config, hits[0].ref))

    def test_exhausted_refill_is_reported_and_diagnostics_reset(self):
        self.stale_candidates()
        trace = retrieval.SearchDiagnostics()
        with patch.object(retrieval, "MAX_SEARCH_CANDIDATES", 40):
            self.assertEqual(retrieval.search(self.config, "quartzmarker", limit=3, diagnostics=trace), [])
        self.assertTrue(trace.refill_exhausted)
        self.assertIn("refill budget exhausted", retrieval.format_search([], diagnostics=trace))
        retrieval.search(self.config, "absentnonce", diagnostics=trace)
        self.assertEqual(trace.rounds, 1)
        self.assertFalse(trace.refill_exhausted)
        self.assertFalse(trace.saturated_channels)

    def test_identical_channels_keep_votes_with_one_query_and_batched_rows(self):
        for index in range(12):
            (self.source / ("notes-%d.md" % index)).write_text("# Notes\n\nquartzmarker\n")
        sync_all(self.config)
        statements = []
        original = retrieval.connect

        def connect(*args, **kwargs):
            connection = original(*args, **kwargs)
            connection.set_trace_callback(statements.append)
            return connection

        with patch.object(retrieval, "connect", side_effect=connect):
            trace = retrieval.SearchDiagnostics()
            hits = retrieval.search(self.config, "quartzmarker", limit=3, diagnostics=trace)
        self.assertEqual(hits[0].score, round((1.4 + .9 + .65) / 61, 8))
        self.assertEqual(trace.rounds, 1)
        self.assertEqual(trace.channels["trigram"], trace.channels["compact"])
        self.assertEqual(sum(sql.startswith("SELECT chunks_trigram.chunk_id") for sql in statements), 1)
        self.assertEqual(sum("c.*, d.id AS doc_id" in sql for sql in statements), 1)

    def test_scope_diagnostics_do_not_search_an_unknown_or_disabled_collection(self):
        raw = json.loads((self.home / "config.json").read_text())
        raw["sources"][0]["enabled"] = False
        (self.home / "config.json").write_text(json.dumps(raw))
        config = load_config(self.home)
        with patch.object(retrieval, "connect", side_effect=AssertionError("must not open index")):
            for collection, state in (("missing", "unknown"), ("test", "disabled")):
                trace = retrieval.SearchDiagnostics()
                self.assertEqual(retrieval.search(config, "quartzmarker", collection=collection, diagnostics=trace), [])
                self.assertEqual(trace.collection_state, state)
                self.assertIn("amem catalog", retrieval.format_search([], diagnostics=trace))

    def run_messages(self, messages):
        output = io.StringIO()
        with patch.object(sys, "stdin", messages), redirect_stdout(output):
            self.assertEqual(run_server(self.config), 0)
        return [json.loads(line) for line in output.getvalue().splitlines()]

    @staticmethod
    def call(identifier, name, **arguments):
        return json.dumps({"jsonrpc": "2.0", "id": identifier, "method": "tools/call",
                           "params": {"name": name, "arguments": arguments}}) + "\n"

    def test_stdio_reloads_disabled_source_for_search_fetch_locate_and_followup(self):
        (self.source / "policy.md").write_text("quartzmarker policy evidence\n")
        sync_all(self.config)
        ref = retrieval.search(self.config, "quartzmarker", collection="test")[0].ref
        path = self.home / "config.json"
        original = path.read_text()

        def messages():
            yield self.call(1, "memory_search", query="quartzmarker", collection="test")
            raw = json.loads(original)
            raw["sources"][0]["enabled"] = False
            path.write_text(json.dumps(raw))
            yield self.call(2, "memory_search", query="quartzmarker", collection="test")
            yield self.call(3, "memory_fetch", ref=ref)
            yield self.call(4, "memory_locate", ref=ref, query="quartzmarker")
            yield self.call(5, "memory_search", follow_ref=ref, query="quartzmarker")
            path.write_text(original)
            yield self.call(6, "memory_search", query="quartzmarker", collection="test")

        responses = self.run_messages(messages())
        self.assertIn(ref, json.dumps(responses[0]))
        self.assertNotIn(ref, json.dumps(responses[1]))
        for response in responses[2:5]:
            self.assertTrue(response["result"]["isError"])
        self.assertIn(ref, json.dumps(responses[5]))

    def test_invalid_config_fails_closed_and_recovers_without_stopping_server(self):
        (self.source / "policy.md").write_text("quartzmarker evidence\n")
        sync_all(self.config)
        path = self.home / "config.json"
        original = path.read_text()

        def messages():
            path.write_text("invalid JSON with private sentinel")
            yield self.call(1, "memory_search", query="quartzmarker")
            yield json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}) + "\n"
            path.write_text(original)
            yield self.call(3, "memory_search", query="quartzmarker")

        responses = self.run_messages(messages())
        self.assertEqual(responses[0]["error"]["code"], -32603)
        self.assertNotIn("private sentinel", json.dumps(responses))
        self.assertIn("result", responses[1])
        self.assertIn("policy.md", json.dumps(responses[2]))

    def test_stdio_applies_new_exclusions_before_sync(self):
        (self.source / "policy.md").write_text("quartzmarker evidence\n")
        sync_all(self.config)
        ref = retrieval.search(self.config, "quartzmarker")[0].ref
        path = self.home / "config.json"

        def messages():
            yield self.call(1, "memory_fetch", ref=ref)
            raw = json.loads(path.read_text())
            raw["sources"][0]["exclude_globs"] = ["policy.md"]
            path.write_text(json.dumps(raw))
            yield self.call(2, "memory_search", query="quartzmarker")
            yield self.call(3, "memory_fetch", ref=ref)

        responses = self.run_messages(messages())
        self.assertIn("quartzmarker evidence", json.dumps(responses[0]))
        self.assertNotIn(ref, json.dumps(responses[1]))
        self.assertTrue(responses[2]["result"]["isError"])

    def test_feedback_mode_change_requires_restart_before_any_write(self):
        sync_all(self.config)
        path = self.home / "config.json"
        raw = json.loads(path.read_text())
        raw["feedback"] = {"auto_record_zero_results": True}
        path.write_text(json.dumps(raw))
        responses = self.run_messages(iter([self.call(1, "memory_search", query="absentnonce")]))
        self.assertIn("restart", responses[0]["error"]["message"])
        self.assertFalse((self.home / "feedback").exists())
