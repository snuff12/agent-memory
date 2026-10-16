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
from agent_memory.config import load_config
from agent_memory.followup import followup_search, format_followup
from agent_memory.indexer import sync_all
from agent_memory.memory_policy import MemoryPolicy
from agent_memory.mcp_server import handle_request
from agent_memory.search import SearchDiagnostics, StaleReferenceError, fetch, search
from tests.test_memory_policy import card
from tests.test_memory_system import make_home
from tests.test_practical_retrieval import write_search_source_before_context


class FollowupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.home = make_home(self.root, self.source)
        self.config = load_config(self.home)

    def tool(self, **arguments):
        return handle_request(self.config, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "memory_search", "arguments": arguments}})

    def test_owner_to_store_followup_keeps_the_original_search(self):
        owner = self.source / "owner.txt"
        owner.write_text("Mina maintains Aurora.")
        (self.source / "store.txt").write_text("Aurora persists in CockroachDB.")
        (self.home / "vault/foreign.md").write_text(card() + "Aurora uses OutsideStore in another collection.")
        sync_all(self.config)
        original = search(self.config, "Mina database", collection="test", limit=3)
        self.assertEqual([hit.relative_path for hit in original], ["owner.txt"])
        self.assertIn("Aurora", fetch(self.config, original[0].ref))
        result = followup_search(self.config, "Aurora", original[0].ref, limit=3)
        self.assertEqual(result.anchor.collection, "test")
        self.assertEqual(owner.read_text()[result.anchor.span_start:result.anchor.span_end], "Aurora")
        self.assertEqual([hit.relative_path for hit in result.results], ["store.txt"])
        self.assertIn("CockroachDB", fetch(self.config, result.results[0].ref))
        self.assertEqual(original, search(self.config, "Mina database", collection="test", limit=3))
        self.assertEqual(owner.read_text(), "Mina maintains Aurora.")

    def test_followup_keeps_definition_and_callsite_as_separate_evidence(self):
        write_search_source_before_context(self.source)
        config = replace(self.config, chunk_chars=2400)
        sync_all(config)
        first = search(config, "_focus_span", collection="test", limit=1)[0]
        first_read = fetch(config, first.ref, query="_focus_span", max_tokens=1200)
        following = followup_search(config, "_focus_span", first.ref, limit=1)
        self.assertEqual(len(following.results), 1)
        self.assertNotEqual(first.ref, following.results[0].ref)
        second_read = fetch(config, following.results[0].ref, query="_focus_span", max_tokens=1200)
        self.assertIn("def _focus_span(", first_read + second_read)
        self.assertIn("focused = _focus_span", first_read + second_read)
        self.assertEqual(first, search(config, "_focus_span", collection="test", limit=1)[0])

    def test_korean_bridge_and_latin_name_with_korean_particle(self):
        for owner_text, query, target in (("소라는 별빛을 담당한다.", "별빛", "별빛은 SQLite에 기록한다."),
                                          ("소라는 Aurora를 담당한다.", "Aurora", "Aurora persists in SQLite.")):
            with self.subTest(query=query):
                (self.source / "owner.txt").write_text(owner_text)
                (self.source / "store.txt").write_text(target)
                sync_all(self.config)
                ref = search(self.config, "소라", collection="test")[0].ref
                result = followup_search(self.config, query, ref, limit=1)
                self.assertEqual([hit.relative_path for hit in result.results], ["store.txt"])
                self.assertIn("SQLite", fetch(self.config, result.results[0].ref))

    def test_bridge_must_be_in_chunk_body_not_a_filename_or_an_invented_name(self):
        (self.source / "Aurora.txt").write_text("A note about AuroraArchive only.")
        sync_all(self.config)
        ref = search(self.config, "Aurora.txt", collection="test")[0].ref
        for query in ("Aurora", "InventedService"):
            with self.subTest(query=query), patch("agent_memory.followup.search") as derived_search:
                with self.assertRaisesRegex(ValueError, "absent from the referenced source chunk"):
                    followup_search(self.config, query, ref)
                derived_search.assert_not_called()

    def test_scope_shareability_and_collection_apply_to_both_anchor_and_results(self):
        vault = self.home / "vault"
        (vault / "owner.md").write_text(card(scope="project:demo", sensitivity="shareable") + "Mina maintains Aurora.")
        (vault / "store.md").write_text(card(scope="project:demo", sensitivity="shareable") + "Aurora persists in CockroachDB.")
        (vault / "private.md").write_text(card(scope="project:demo") + "Aurora uses PrivateStore.")
        (vault / "other.md").write_text(card(scope="project:other", sensitivity="shareable") + "Aurora uses OtherStore.")
        (vault / "inbox.md").write_text(card(scope="project:demo", status="inbox", sensitivity="shareable") + "Aurora uses PendingStore.")
        policy = MemoryPolicy(project="demo", shareable_only=True)
        sync_all(self.config)
        ref = search(self.config, "Mina", collection="vault", policy=policy)[0].ref
        result = followup_search(self.config, "Aurora", ref, policy=policy)
        self.assertEqual({hit.relative_path for hit in result.results}, {"store.md"})
        for denied in (MemoryPolicy(), MemoryPolicy(project="other")):
            with self.assertRaises(StaleReferenceError):
                followup_search(self.config, "Aurora", ref, policy=denied)
        with self.assertRaisesRegex(ValueError, "collection must match"):
            followup_search(self.config, "Aurora", ref, policy=policy, collection="test")

    def test_changed_anchor_before_or_during_search_returns_no_followup(self):
        owner = self.source / "owner.txt"
        owner.write_text("Mina maintains Aurora.")
        (self.source / "store.txt").write_text("Aurora persists in CockroachDB.")
        sync_all(self.config)
        ref = search(self.config, "Mina", collection="test")[0].ref
        def concurrent_change(*args, **kwargs):
            hits = search(*args, **kwargs)
            owner.write_text("Owner changed during the search.")
            return hits
        with patch("agent_memory.followup.search", side_effect=concurrent_change):
            with self.assertRaises(StaleReferenceError):
                followup_search(self.config, "Aurora", ref)
        with self.assertRaises(StaleReferenceError):
            followup_search(self.config, "Aurora", ref)

    def test_review_mode_keeps_inbox_anchor_marked_as_unreviewed(self):
        (self.home / "vault/inbox.md").write_text(card(status="inbox", scope="project:demo") + "Mina maintains Aurora.")
        (self.home / "vault/store.md").write_text(card(scope="project:demo") + "Aurora persists in CockroachDB.")
        sync_all(self.config)
        policy = MemoryPolicy(status="all", project="demo")
        ref = search(self.config, "Mina", collection="vault", policy=policy)[0].ref
        result = followup_search(self.config, "Aurora", ref, policy=policy)
        self.assertEqual(result.anchor.memory_status, "inbox")
        self.assertEqual(result.anchor.memory_scope, "project:demo")
        rendered = format_followup(result)
        self.assertIn('anchor-memory: "inbox"', rendered)
        self.assertIn('scope: "project:demo"', rendered)
        self.assertLessEqual(len(rendered.encode("utf-8")), 4000)

    def test_review_policy_change_during_search_invalidates_anchor(self):
        owner = self.home / "vault/owner.md"
        owner.write_text(card() + "Mina maintains Aurora.")
        (self.home / "vault/store.md").write_text(card() + "Aurora persists in CockroachDB.")
        sync_all(self.config)
        ref = search(self.config, "Mina", collection="vault")[0].ref
        def concurrent_review(*args, **kwargs):
            hits = search(*args, **kwargs)
            owner.write_text(card(status="inbox") + "Mina maintains Aurora.")
            sync_all(self.config, "vault")
            return hits
        with patch("agent_memory.followup.search", side_effect=concurrent_review):
            with self.assertRaises(StaleReferenceError):
                followup_search(self.config, "Aurora", ref)

    def test_changed_linked_source_is_not_returned_and_output_stays_bounded(self):
        (self.source / "owner.txt").write_text("Mina maintains Aurora.")
        target = self.source / "store.txt"
        target.write_text("Aurora persists in CockroachDB.")
        sync_all(self.config)
        ref = search(self.config, "Mina", collection="test")[0].ref
        target.write_text("Changed after indexing.")
        trace = SearchDiagnostics()
        result = followup_search(self.config, "Aurora", ref, diagnostics=trace)
        self.assertEqual(result.results, [])
        self.assertEqual(trace.returned, 0)
        self.assertEqual(trace.unreadable_or_changed_documents, 1)
        for budget in (0, 1, 128, 512, 4000, 6000):
            for diagnostics in (None, trace):
                rendered = format_followup(result, max_units=budget, diagnostics=diagnostics)
                self.assertLessEqual(len(rendered.encode("utf-8")), budget)
        payload = json.loads(format_followup(result, as_json=True, diagnostics=trace))
        self.assertEqual(payload["followup"]["anchor"]["ref"], ref)
        self.assertEqual(payload["results"], [])

    def test_cli_and_mcp_followups_do_not_save_derived_queries_as_feedback(self):
        (self.source / "owner.txt").write_text("Mina maintains OrphanProject.")
        sync_all(self.config)
        ref = search(self.config, "Mina", collection="test")[0].ref
        stdout = io.StringIO()
        with patch("agent_memory.cli._try_record_zero_result") as cli_record, redirect_stdout(stdout):
            self.assertEqual(main(["--home", str(self.home), "search", "OrphanProject", "--follow-ref", ref, "--json"]), 0)
        cli_record.assert_not_called()
        self.assertEqual(json.loads(stdout.getvalue())["results"], [])
        with patch("agent_memory.mcp_server.record_zero_result") as mcp_record:
            response = self.tool(query="OrphanProject", follow_ref=ref, explain=True, limit=3)
        mcp_record.assert_not_called()
        self.assertFalse(response["result"]["isError"])
        self.assertIn("from-ref: " + ref, response["result"]["content"][0]["text"])

    def test_mcp_validates_new_arguments_before_retrieval(self):
        invalid = [dict(query="Aurora", follow_ref=True), dict(query="Aurora", follow_ref=" "),
                   dict(query="Aurora", follow_ref="x" * 241), dict(query="x" * 121, follow_ref="opaque"),
                   dict(query="Aurora\nBeta", follow_ref="opaque")]
        with patch("agent_memory.mcp_server.followup_search") as follow:
            for arguments in invalid:
                self.assertEqual(self.tool(**arguments)["error"]["code"], -32602)
        follow.assert_not_called()


if __name__ == "__main__":
    unittest.main()
