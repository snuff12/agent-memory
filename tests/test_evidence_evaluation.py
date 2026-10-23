from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from agent_memory.cli import _evaluate, main
from agent_memory.config import load_config
from agent_memory.evaluation import requirements
from agent_memory.indexer import sync_all
from agent_memory.search import _fetch_with_excerpt, search
from tests.test_memory_policy import card
from tests.test_memory_system import make_home


class EvidenceEvaluationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.home = make_home(self.root, self.source)
        self.config = load_config(self.home)
        self.gold = self.home / "gold.jsonl"

    def evaluate(self, **case):
        self.gold.write_text(json.dumps(case) + "\n")
        return _evaluate(self.config, self.gold)

    def test_missing_second_document_fails_required_mode_but_keeps_legacy_any_match(self):
        (self.source / "deploy.md").write_text("release deploy in stages\n")
        (self.source / "rollback.md").write_text("revert after failed checks\n")
        sync_all(self.config)
        case = dict(query="release", collection="test", relevant_paths=["deploy.md", "rollback.md"])
        self.assertEqual(self.evaluate(**case)["passed_cases"], 1)
        case["required_paths"] = ["deploy.md", "rollback.md"]
        report = self.evaluate(**case)
        self.assertEqual(report["hit_rate_at_k"], 1.0)
        self.assertEqual(report["requirements_pass_rate"], 0.0)
        self.assertEqual(report["passed_cases"], 0)
        self.assertEqual(report["details"][0]["missing_required_paths"], ["rollback.md"])
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--home", str(self.home), "eval", "--file", str(self.gold)]), 1)

    def test_all_evidence_must_be_in_bounded_reads_and_read_count_is_enforced(self):
        (self.source / "deploy.md").write_text("release uses staged rollout\n")
        (self.source / "rollback.md").write_text("release reverts on failed checks\n")
        sync_all(self.config)
        case = dict(query="release", collection="test", required_evidence=[
            {"path": "deploy.md", "text": "staged rollout"},
            {"path": "rollback.md", "text": "reverts on failed checks"}])
        failed = self.evaluate(**case, read_limit=1)
        self.assertEqual(failed["passed_cases"], 0)
        self.assertEqual(failed["read_operations"], 1)
        passed = self.evaluate(**case)
        self.assertEqual(passed["passed_cases"], 1)
        self.assertEqual(passed["read_operations"], 2)
        self.assertLessEqual(passed["read_units"], 2400)

    def test_metadata_and_a_similar_filename_do_not_count_as_source_evidence(self):
        (self.source / "deploy.md.backup.md").write_text("release uses staged rollout\n")
        sync_all(self.config)
        failed = self.evaluate(query="release", required_paths=["deploy.md"])
        self.assertEqual(failed["passed_cases"], 0)
        failed = self.evaluate(query="release", required_evidence=[
            {"path": "deploy.md.backup.md", "text": "deploy.md.backup.md"}])
        self.assertEqual(failed["passed_cases"], 0)
        hit = search(self.config, "release")[0]
        rendered, excerpt = _fetch_with_excerpt(self.config, hit.ref, query="release")
        self.assertIn("deploy.md.backup.md", rendered)
        self.assertNotIn("deploy.md.backup.md", excerpt)

    def test_file_hit_does_not_credit_text_outside_the_read_window(self):
        self.config = replace(self.config, chunk_chars=6000)
        (self.source / "retention.md").write_text("release " + "background detail " * 200 + "37 days")
        sync_all(self.config)
        case = dict(query="release", required_evidence=[{"path": "retention.md", "text": "37 days"}])
        self.assertEqual(self.evaluate(**case)["passed_cases"], 0)
        self.assertEqual(self.evaluate(**case, read_max_bytes=8000)["passed_cases"], 1)

    def test_policy_is_retained_for_search_and_evidence_reads(self):
        (self.home / "vault" / "decision.md").write_text(card(scope="project:demo") + "release uses staged rollout\n")
        sync_all(self.config)
        case = dict(query="release", collection="vault",
                    required_evidence=[{"path": "decision.md", "text": "staged rollout"}])
        self.assertEqual(self.evaluate(**case)["passed_cases"], 0)
        self.assertEqual(self.evaluate(**case, project="demo")["passed_cases"], 1)

    def test_output_hidden_candidates_are_not_read(self):
        (self.source / "deploy.md").write_text("release uses staged rollout\n")
        sync_all(self.config)
        with patch("agent_memory.cli.SEARCH_OUTPUT_MAX_UNITS", 1):
            report = self.evaluate(query="release", required_evidence=[{"path": "deploy.md", "text": "staged rollout"}])
        self.assertEqual(report["passed_cases"], 0)
        self.assertEqual(report["read_operations"], 0)

    def test_bad_requirements_are_rejected_before_running_searches(self):
        for case in ({"required_paths": "deploy.md"}, {"required_evidence": [{}]},
                     {"required_evidence": [{"path": "a", "text": ""}]},
                     {"read_limit": True}, {"read_limit": 4}, {"read_max_bytes": 100},
                     {"expect_no_results": True, "required_paths": ["a"]}):
            with self.subTest(case=case), self.assertRaises(ValueError):
                requirements(case)
