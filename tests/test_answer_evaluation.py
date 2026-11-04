from __future__ import annotations

import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from agent_memory.answer_evaluation import digest, evaluate_answers
from agent_memory.cli import _evaluate, main
from agent_memory.config import load_config
from agent_memory.indexer import sync_all
from tests.test_memory_system import make_home


FIXTURES = Path(__file__).parent / 'fixtures/continuity'


class AnswerEvaluationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        source = self.root / 'source'
        shutil.copytree(FIXTURES, source)
        self.home = make_home(self.root, source)
        self.config = replace(load_config(self.home), chunk_chars=3000)
        sync_all(self.config)
        self.fixture = FIXTURES / 'cases.jsonl'
        self.evidence = self.root / 'packet.json'
        self.answers_file = self.root / 'answers.jsonl'
        self.reviews_file = self.root / 'reviews.jsonl'
        self.report = _evaluate(self.config, self.fixture, evidence_output=self.evidence)
        self.packet = json.loads(self.evidence.read_text())
        cases = [json.loads(line) for line in self.fixture.read_text().splitlines()]
        # These are controlled harness inputs, not claimed model judgments.
        texts = [
            'The assistant proposed SQLite. The user approved PostgreSQL for concurrent writers.',
            'Friday deployment was proposed by the assistant. User approval is not established.',
            'The October decision explicitly replaces September: 90 days now; 30 days earlier to limit storage cost.',
            'November 5 and November 12 conflict. No resolved launch date is recorded.',
            'Two events are visible; the inventory says four exist. The other outcomes are unavailable.',
        ]
        self.answers, self.reviews = [], []
        for case, delivered, text in zip(cases, self.packet['cases'], texts):
            first = delivered['evidence'][0]
            answer = dict(id=case['id'], packet_sha256=digest(self.packet), answer=text,
                          model='synthetic-test', prompt_version='test-v1',
                          citations=[dict(ref=first['ref'], quote=first['excerpt'])])
            self.answers.append(answer)
            self.reviews.append(dict(id=case['id'], packet_sha256=digest(self.packet),
                                     answer_sha256=digest(answer), reviewer='synthetic-reviewer',
                                     rubric_version='continuity-v1',
                                     verdicts={key: True for key in case['answer_rubric']},
                                     notes='Controlled positive rubric example.'))
        self.save()

    def save(self):
        for path, rows in ((self.answers_file, self.answers), (self.reviews_file, self.reviews)):
            path.write_text(''.join(json.dumps(row) + '\n' for row in rows))

    def evaluate(self):
        self.save()
        return evaluate_answers(self.fixture, self.evidence, self.answers_file, self.reviews_file)

    def test_continuity_delivery_and_reviewed_answer_report(self):
        self.assertEqual(self.report['passed_cases'], 5)
        self.assertEqual(self.report['requirement_passes'], 5)
        self.assertFalse(self.report['interpretation_evaluated'])
        report = self.evaluate()
        self.assertTrue(report['passed'])
        self.assertEqual(report['interpretation_passes'], 5)
        self.assertEqual(report['delivery_passes'], 5)
        serialized = self.evidence.read_text()
        for gold_key in ('answer_rubric', 'required_evidence', 'required_paths'):
            self.assertNotIn(gold_key, serialized)
        self.assertEqual(self.evidence.stat().st_mode & 0o777, 0o600)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(['--home', str(self.home), 'eval-answers', '--file', str(self.fixture),
                                   '--evidence', str(self.evidence), '--answers', str(self.answers_file),
                                   '--reviews', str(self.reviews_file), '--json']), 0)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(['--home', str(self.home), 'eval-answers', '--file', str(self.fixture),
                                   '--evidence', str(self.evidence), '--answers', str(self.answers_file),
                                   '--reviews', str(self.reviews_file)]), 0)
        self.assertIn('delivery=5/5 interpretation=5/5', output.getvalue())

    def test_wrong_attribution_approval_temporal_conflict_and_count_fail_separately(self):
        for index in range(5):
            with self.subTest(case=index):
                review = self.reviews[index]
                criterion = next(iter(review['verdicts']))
                review['verdicts'][criterion] = False
                review['notes'] = 'This criterion failed in the reviewed answer.'
                report = self.evaluate()
                self.assertEqual(report['delivery_passes'], 5)
                self.assertEqual(report['interpretation_passes'], 4)
                self.assertFalse(report['passed'])
                self.assertEqual(report['details'][index]['failed_criteria'], [criterion])
                review['verdicts'][criterion] = True

    def test_citation_outside_delivered_excerpt_fails_despite_positive_review(self):
        self.answers[0]['citations'][0]['quote'] = 'invented evidence'
        self.reviews[0]['answer_sha256'] = digest(self.answers[0])
        report = self.evaluate()
        self.assertEqual(report['invalid_citations'], 1)
        self.assertEqual(report['delivery_passes'], 5)
        self.assertFalse(report['passed'])

    def test_missing_answer_review_and_changed_answer_cannot_inherit_a_pass(self):
        self.answers[0]['answer'] = 'SQLite was approved.'
        report = self.evaluate()
        self.assertIn('review_binding_mismatch', report['details'][0]['errors'])
        self.reviews.pop()
        self.answers.pop(1)
        report = self.evaluate()
        self.assertEqual(report['unreviewed'], 1)
        self.assertFalse(report['passed'])

    def test_missing_delivered_text_is_not_an_interpretation_failure(self):
        self.packet['cases'][0]['evidence'][0]['excerpt'] = ''
        self.evidence.write_text(json.dumps(self.packet))
        for answer, review in zip(self.answers, self.reviews):
            answer['packet_sha256'] = digest(self.packet)
            if answer is self.answers[0]:
                answer['citations'] = []
            review['packet_sha256'] = digest(self.packet)
            review['answer_sha256'] = digest(answer)
        report = self.evaluate()
        self.assertEqual(report['delivery_passes'], 4)
        self.assertFalse(report['details'][0]['delivery']['passed'])

    def test_changed_fixture_packet_policy_and_review_rubric_are_rejected(self):
        self.reviews[0]['verdicts'] = {'unrelated': True}
        self.assertIn('review_rubric_mismatch', self.evaluate()['details'][0]['errors'])
        self.packet['cases'][0]['policy']['project'] = 'other-project'
        self.evidence.write_text(json.dumps(self.packet))
        with self.assertRaisesRegex(ValueError, 'policy'):
            self.evaluate()
        self.packet['fixture_sha256'] = '0' * 64
        self.evidence.write_text(json.dumps(self.packet))
        with self.assertRaisesRegex(ValueError, 'changed fixture'):
            self.evaluate()

    def test_hidden_results_and_absent_reviews_do_not_pass(self):
        self.evidence.unlink()
        with patch('agent_memory.cli.SEARCH_OUTPUT_MAX_UNITS', 1):
            report = _evaluate(self.config, self.fixture, evidence_output=self.evidence)
        self.assertEqual(report['passed_cases'], 0)
        self.assertEqual(report['read_operations'], 0)
        self.assertEqual(json.loads(self.evidence.read_text())['cases'][0]['evidence'], [])
        self.reviews = []
        self.assertFalse(self.evaluate()['passed'])

    def test_export_never_overwrites_an_existing_packet(self):
        before = self.evidence.read_bytes()
        with self.assertRaises(FileExistsError):
            _evaluate(self.config, self.fixture, evidence_output=self.evidence)
        self.assertEqual(self.evidence.read_bytes(), before)

    def test_answer_report_preserves_contamination_and_search_budget_failures(self):
        cases = [json.loads(line) for line in self.fixture.read_text().splitlines()]
        cases[0]['forbidden_collections'] = ['test']
        cases[1]['max_search_units'] = 1
        self.fixture = self.root / 'guarded.jsonl'
        self.fixture.write_text(''.join(json.dumps(case) + '\n' for case in cases))
        self.evidence.unlink()
        delivery = _evaluate(self.config, self.fixture, evidence_output=self.evidence)
        self.assertEqual(delivery['passed_cases'], 3)
        self.packet = json.loads(self.evidence.read_text())
        for answer, review in zip(self.answers, self.reviews):
            answer['packet_sha256'] = digest(self.packet)
            review['packet_sha256'] = digest(self.packet)
            review['answer_sha256'] = digest(answer)
        report = self.evaluate()
        self.assertEqual(report['delivery_passes'], 3)
        self.assertEqual(report['interpretation_passes'], 5)
        self.assertFalse(report['passed'])

    def test_negative_retrieval_can_have_reviewed_abstention_without_invented_citations(self):
        case = dict(id='negative', query='quasarzzzzz', collection='test', expect_no_results=True,
                    answer_rubric_version='continuity-v1',
                    answer_rubric={'support': 'Abstain because no source evidence was delivered.'})
        self.fixture = self.root / 'negative.jsonl'
        self.fixture.write_text(json.dumps(case) + '\n')
        self.evidence.unlink()
        _evaluate(self.config, self.fixture, evidence_output=self.evidence)
        self.packet = json.loads(self.evidence.read_text())
        answer = dict(id='negative', packet_sha256=digest(self.packet), answer='No evidence was retrieved; I cannot establish a decision.',
                      model='synthetic-test', prompt_version='test-v1', citations=[])
        self.answers = [answer]
        self.reviews = [dict(id='negative', packet_sha256=digest(self.packet), answer_sha256=digest(answer),
                             reviewer='synthetic-reviewer', rubric_version='continuity-v1',
                             verdicts={'support': True}, notes='Abstention without invented evidence.')]
        self.assertTrue(self.evaluate()['passed'])

    def test_malformed_packet_fields_raise_a_clear_validation_error(self):
        self.packet['cases'][0]['evidence'][0]['ref'] = []
        self.evidence.write_text(json.dumps(self.packet))
        with self.assertRaisesRegex(ValueError, 'string refs'):
            self.evaluate()
