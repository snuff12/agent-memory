from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from benchmarks.longmemeval import (
    Candidate, aggregate, download, outside_checkout, prepare_case, render_session,
    run, run_case, score_sessions, session_order, validate_case,
)


def synthetic_case():
    return {
        "question_id": "synthetic-1", "question_type": "multi-session",
        "question": "quartz navigation rule", "answer": "ANSWER-MUST-NOT-BE-INDEXED",
        "question_date": "2026/01/03", "answer_session_ids": ["a", "b"],
        "haystack_session_ids": ["a", "b", "c"],
        "haystack_dates": ["2026/01/01", "2026/01/02", "2026/01/02"],
        "haystack_sessions": [
            [{"role": "user", "content": "The quartz navigation rule uses a blue map.", "has_answer": True}],
            [{"role": "assistant", "content": "The quartz navigation rule points north.", "has_answer": True}],
            [{"role": "user", "content": "Lunch was vegetable soup."}],
        ],
    }


class LongMemEvalTests(unittest.TestCase):
    def test_all_evidence_metric_is_distinct_from_any_and_fraction(self):
        scores = score_sessions(["noise", "a", "a", "other", "b"], {"a", "b"})
        self.assertEqual(scores["session_hit@5"], 1)
        self.assertEqual(scores["session_all@1"], 0)
        self.assertEqual(scores["session_recall@1"], 0)
        self.assertEqual(scores["session_all@5"], 1)
        self.assertEqual(scores["session_mrr"], 0.5)
        partial = score_sessions(["a"], {"a", "b"})
        self.assertEqual(partial["session_hit@5"], 1)
        self.assertEqual(partial["session_all@5"], 0)
        self.assertEqual(partial["session_recall@5"], 0.5)
        with self.assertRaises(ValueError):
            score_sessions([], set())

    def test_chunk_deduplication_preserves_first_session_rank(self):
        self.assertEqual(session_order([Candidate("a", 3), Candidate("a", 1), Candidate("b", 0)]), ["a", "b"])

    def test_allowlisted_serialization_never_copies_gold_annotations(self):
        case = synthetic_case()
        case["question"] = "QUERY-MUST-NOT-BE-INDEXED"
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            config, paths = prepare_case(case, home)
            text = "\n".join(path.read_text() for path in (home / "knowledge").glob("*.txt"))
            self.assertNotIn(case["answer"], text)
            self.assertNotIn(case["question"], text)
            self.assertNotIn("has_answer", text)
            self.assertNotIn("answer_session_ids", text)
            self.assertEqual(list(paths.values()), ["a", "b", "c"])
            self.assertEqual(config.chunk_chars, 2400)
            self.assertEqual(config.chunk_overlap_chars, 240)
            self.assertEqual([source.name for source in config.sources], ["research"])
        self.assertEqual(render_session([{"role": "user", "content": "a\nb", "has_answer": True}], "D"),
                         "Date: D\n\nuser: a\nb\n")

    def test_invalid_alignment_and_missing_evidence_fail_before_ingestion(self):
        for mutation in (
            {"haystack_dates": []}, {"answer_session_ids": ["not-in-history"]},
            {"haystack_session_ids": ["a", "", "b"]}, {"answer_session_ids": []},
        ):
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                validate_case(dict(synthetic_case(), **mutation))

    def test_repeated_published_session_ids_preserve_both_histories(self):
        case = synthetic_case()
        case["haystack_session_ids"] = ["a", "b", "b"]
        validate_case(case)
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            _, paths = prepare_case(case, home)
            self.assertEqual(list(paths.values()), ["a", "b", "b"])
            self.assertEqual(len(list((home / "knowledge").glob("*.txt"))), 3)

    def test_real_index_search_and_baseline_flow_with_synthetic_data(self):
        case = synthetic_case()
        abstention = copy.deepcopy(case)
        abstention["question_id"] = "synthetic-2_abs"
        abstention["answer_session_ids"] = []
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "synthetic.json"
            data.write_text(json.dumps([case, abstention]))
            with self.assertRaisesRegex(ValueError, "checksum"):
                run(data, root / "not-created")
            destination = run(data, root / "run", allow_unpinned=True)
            report = json.loads(destination.read_text())
            self.assertFalse(report["full_dataset"])
            self.assertFalse(report["dataset"]["pinned"])
            self.assertEqual(report["cases"], 2)
            self.assertEqual(report["answerable_cases"], 1)
            self.assertEqual(report["abstention_cases"], 1)
            self.assertLessEqual(report["max_cli_search_bytes"], 4000)
            self.assertFalse((root / "run" / "home").exists())
            for system in report["systems"].values():
                self.assertEqual(system["metrics"]["session_all@5"], 1)
            rows = [json.loads(line) for line in (root / "run" / "cases.jsonl").read_text().splitlines()]
            self.assertIsNone(rows[1]["systems"]["amem_candidates"]["metrics"])
            serialized = json.dumps(rows)
            self.assertNotIn(case["answer"], serialized)
            self.assertNotIn(str(root), serialized)
            self.assertEqual(aggregate(rows)["answerable_cases"], 1)
            with self.assertRaises(FileExistsError):
                run(data, root / "run", allow_unpinned=True)

    def test_existing_home_is_never_deleted(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            home.mkdir()
            sentinel = home / "keep.txt"
            sentinel.write_text("keep")
            with self.assertRaises(FileExistsError):
                run_case(synthetic_case(), home)
            self.assertEqual(sentinel.read_text(), "keep")

    def test_dataset_and_run_data_must_stay_outside_checkout(self):
        with self.assertRaises(ValueError):
            outside_checkout(Path(__file__).resolve().parents[1] / "data.json")

    def test_downloader_does_not_remove_another_partial_download(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "data.json"
            partial = destination.with_name(destination.name + ".partial")
            partial.write_text("another process")
            with patch("urllib.request.urlopen") as request, self.assertRaises(FileExistsError):
                download(destination)
            request.assert_not_called()
            self.assertEqual(partial.read_text(), "another process")
