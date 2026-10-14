from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from agent_memory.config import load_config
from agent_memory.indexer import sync_all
from agent_memory.search import SearchHit, format_search, format_search_with_visible_hits, search
from tests.test_memory_system import make_home


class RetrievalQualityTests(unittest.TestCase):
    def test_short_english_word_does_not_match_inside_an_unrelated_word(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "learning.txt").write_text("A finch lands nearby.\n")
            config = load_config(make_home(root, source))
            sync_all(config)
            self.assertEqual(search(config, "in", collection="test"), [])

    def test_short_latin_filename_still_uses_word_boundaries(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "AI.md").write_text("# Reference\nUseful design notes.\n")
            config = load_config(make_home(root, source))
            sync_all(config)
            self.assertEqual(search(config, "AI", collection="test")[0].relative_path, "AI.md")

    def test_english_function_words_do_not_displace_distinctive_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            for index in range(40):
                (source / ("note-%03d.txt" % index)).write_text(
                    "This is my notebook. I am in a meeting.\n"
                )
            (source / "target.txt").write_text("The aquamarine protocol protects transfer endpoints.\n")
            config = load_config(make_home(root, source))
            sync_all(config)
            hits = search(config, "Is my aquamarine protocol supported", collection="test", limit=5)
            self.assertEqual(hits[0].relative_path, "target.txt")

    @staticmethod
    def candidate(document: int, ordinal: int) -> SearchHit:
        ref = "%016x@%s.p3c1f%s#%d" % (document, "b" * 16, "c" * 16, ordinal)
        return SearchHit(
            rank=(document - 1) * 2 + ordinal + 1, ref=ref,
            document_id="doc-%d" % document, generation="generation", collection="research",
            kind="research", trust="canonical", title="document-%d.txt" % document,
            path="/knowledge/references/document-%d.txt" % document,
            relative_path="document-%d.txt" % document,
            heading="", locator="", start_line=ordinal + 1, end_line=ordinal + 1,
            page=None, token_estimate=500, snippet="distinctive navigation evidence " * 60, score=1.0,
        )

    def test_bounded_navigation_preserves_refs_for_ten_distinct_documents(self):
        hits = [self.candidate(document, ordinal) for document in range(1, 11) for ordinal in range(2)]
        rendered, visible = format_search_with_visible_hits(hits, max_units=4000)
        self.assertLessEqual(len(rendered.encode("utf-8")), 4000)
        self.assertEqual(list(dict.fromkeys(hit.document_id for hit in visible)),
                         ["doc-%d" % i for i in range(1, 11)])
        self.assertEqual(rendered.count("/knowledge/references"), 1)
        self.assertIn("untrusted source data", rendered)
        for hit in visible:
            self.assertIn(hit.ref, rendered)
            self.assertIn(hit.relative_path, rendered)
        self.assertEqual(len(json.loads(format_search(hits, as_json=True))["results"]), 20)
        self.assertEqual(len(format_search_with_visible_hits(hits)[1]), 20)

    def test_bounded_output_retains_complementary_sections_from_one_document(self):
        first = replace(self.candidate(1, 0), title="Deployment policy", locator="Production rollout",
                        snippet="Use staged rollout for the production deployment.")
        second = replace(self.candidate(1, 1), title="Deployment policy", locator="Rollback conditions",
                         start_line=400, end_line=420,
                         snippet="Rollback when the error rate exceeds the release limit.")
        rendered, visible = format_search_with_visible_hits([first, second], max_units=4000)
        self.assertEqual(visible, [first, second])
        self.assertIn(first.ref, rendered)
        self.assertIn(second.ref, rendered)
        self.assertIn("Use staged rollout", rendered)
        self.assertIn("Rollback when", rendered)
        self.assertLessEqual(len(rendered.encode("utf-8")), 4000)

    def test_tiny_budgets_remain_bounded_with_and_without_candidates(self):
        for budget in (0, 1, 10, 60, 128):
            for hits in ([], [self.candidate(1, 0)]):
                with self.subTest(budget=budget, empty=not hits):
                    rendered, visible = format_search_with_visible_hits(hits, max_units=budget)
                    self.assertLessEqual(len(rendered.encode("utf-8")), budget)
                    self.assertEqual(visible, [])

    def test_compact_output_keeps_memory_warnings_and_scope(self):
        hit = replace(self.candidate(1, 0), memory_status="inbox", memory_scope="project:demo",
                      memory_sensitivity="private", memory_entry_type="invalid")
        rendered, visible = format_search_with_visible_hits([hit], max_units=1200)
        self.assertEqual(visible, [hit])
        self.assertIn("MEMORY_METADATA_INVALID", rendered)
        self.assertIn("scope: project:demo", rendered)
        self.assertIn("sensitivity: private", rendered)
        self.assertIn("trust: canonical", rendered)
