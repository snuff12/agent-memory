"""Query-side term extraction and literal focus, on synthetic Korean/Latin text."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from agent_memory.config import load_config
from agent_memory.indexer import sync_all
from agent_memory.search import SearchDiagnostics, fetch, search
from tests.test_memory_system import make_home


class QueryTermTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.config = load_config(make_home(self.root, self.source))
        files = {
            "deploy.md": "# 배포 절차\n\n운영 서버 배포는 블루그린 방식으로 진행한다.\n",
            "pets.md": "# 반려동물\n\n고양시 동물병원은 주말에도 진료한다.\n",
            "sqlite.md": "# SQLite 운영\n\nSQLite의 WAL 모드를 켜면 읽기와 쓰기가 동시에 가능하다. 체크포인트는 매시간 실행한다.\n",
            "config-guide.md": "# 설정 안내\n\n설정 파일은 config.json이다. 값을 바꾼 뒤 재시작한다.\n",
            "payment.md": "# 결제 재시도\n\n결제 API의 최대 재시도 횟수는 4회다.\n",
            "retry.py": "def compute_backoff_delay(attempt):\n    return min(30, 2 ** attempt)\n",
        }
        for name, text in files.items():
            (self.source / name).write_text(text, encoding="utf-8")

    def paths(self, query, trace=None, limit=3):
        return [hit.relative_path for hit in search(self.config, query, collection="test", limit=limit, diagnostics=trace)]

    def test_short_korean_terms_inside_compounds_keep_their_channels(self):
        # Current true positives only: the term occurs inside a longer eojeol,
        # where word-prefix matching cannot see it.
        sync_all(self.config)
        for query, expected, channel in (("그린", "deploy.md", "short_cjk"), ("병원", "pets.md", "short_cjk"),
                                         ("포인트", "sqlite.md", "trigram")):
            with self.subTest(query=query):
                trace = SearchDiagnostics()
                self.assertIn(expected, self.paths(query, trace))
                self.assertGreater(trace.channels[channel], 0)
        self.assertIn("deploy.md", self.paths("운영서버"))

    def test_sentence_period_after_korean_does_not_hide_a_term(self):
        sync_all(self.config)
        for query, expected, term in (("포인트.", "sqlite.md", "포인트"), ("그린.", "deploy.md", "그린")):
            with self.subTest(query=query):
                trace = SearchDiagnostics()
                self.assertIn(expected, self.paths(query, trace))
                self.assertEqual(trace.selected_terms, [term])
        trace = SearchDiagnostics()
        self.paths("os. config.json", trace)
        self.assertEqual(trace.selected_terms, ["os.", "config.json"])
        # Only a trailing period goes; Korean file names keep their dots.
        self.paths("회의록.md 포인트.다음.", trace)
        self.assertEqual(trace.selected_terms, ["회의록.md", "포인트.다음"])

    def test_korean_particle_after_latin_name_keeps_the_bare_name(self):
        sync_all(self.config)
        for query, expected in (("SQLite는", "sqlite.md"), ("WAL을", "sqlite.md"),
                                ("config.json을 어디서 바꿔?", "config-guide.md"),
                                ("config-guide.md를 열어줘", "config-guide.md")):
            with self.subTest(query=query):
                self.assertEqual(self.paths(query)[:1], [expected])
        trace = SearchDiagnostics()
        self.paths("config-guide.md를 열어줘", trace)
        self.assertEqual(trace.selected_terms, ["config-guide.md", "config-guide.md를", "열어줘"])
        self.assertGreater(trace.channels["filename"], 0)
        # Only listed particles split; a following noun stays part of the term.
        self.paths("API키는", trace)
        self.assertEqual(trace.selected_terms, ["api키는"])

    def test_particle_base_is_an_anchor_inside_the_term_cap(self):
        (self.source / "store.txt").write_text("openConnection opens the ledger socket.\n")
        (self.source / "release-plan.md").write_text("# Release\n\nShip the signed bundle.\n")
        (self.source / "quill.txt").write_text("Quillstore keeps the ledger snapshots.\n")
        sync_all(self.config)
        # Quoting is the only thing that makes "Quillstore" an anchor.
        for name, base, expected in (("openConnection을", "openconnection", "store.txt"),
                                     ("release-plan.md를", "release-plan.md", "release-plan.md"),
                                     ('"Quillstore는"', "quillstore", "quill.txt")):
            with self.subTest(name=name):
                query = ("please inspect these general project notes before explaining how %s reaches "
                         "the configured service during the current deployment workflow" % name)
                trace = SearchDiagnostics()
                self.assertIn(expected, self.paths(query, trace, limit=5))
                self.assertIn(base, trace.selected_terms)
                self.assertLessEqual(len(trace.selected_terms), 12)

    def test_particle_forms_do_not_take_the_path_channel_slots(self):
        # The named file's body never repeats its name; distractor bodies do.
        (self.source / "배포가이드.md").write_text("# 릴리스 순서\n\n태그를 만들고 서명한다.\n", encoding="utf-8")
        for index in range(30):
            (self.source / ("misc-%02d.md" % index)).write_text(
                "Redis는 로컬 캐시이고 API는 내부용이다. 배포가이드 문서를 참고한다. %d\n" % index, encoding="utf-8")
        sync_all(self.config)
        for query in ("Redis는 API는 배포가이드", "config.json을 Redis는 배포가이드"):
            with self.subTest(query=query):
                trace = SearchDiagnostics()
                self.assertEqual(self.paths(query, trace, limit=5)[:1], ["배포가이드.md"])
                self.assertGreaterEqual(trace.channels["path"], 1)

    def test_particle_forms_do_not_push_the_final_distinctive_word_out_of_the_cap(self):
        (self.source / "target.txt").write_text("The aquamarine protocol is documented here.\n")
        (self.source / "incident.txt").write_text("INC-7846 was caused by an expired certificate.\n")
        sync_all(self.config)
        identifiers = ["INC-%d" % number for number in range(7841, 7847)]
        query = ("please compare " + " ".join(name + "의" for name in identifiers)
                 + " old project meeting notes and explain what happened with aquamarine")
        trace = SearchDiagnostics()
        found = self.paths(query, trace, limit=5)
        self.assertIn("target.txt", found)
        self.assertIn("incident.txt", found)
        self.assertIn("aquamarine", trace.selected_terms)
        self.assertTrue(all(name.casefold() in trace.selected_terms for name in identifiers))
        self.assertLessEqual(len(trace.selected_terms), 12)

    def test_focus_word_bounds_agree_with_the_index(self):
        sync_all(self.config)
        self.assertIn("retry.py", self.paths("backoff"))
        for lookup, query, evidence in (("compute_backoff_delay", "backoff", "def compute_backoff_delay("),
                                        ("결제", "API", "결제 API의 최대 재시도 횟수는 4회다."),
                                        ("WAL", "SQLite는", "SQLite의 WAL 모드")):
            with self.subTest(query=query):
                ref = search(self.config, lookup, collection="test")[0].ref
                focused = fetch(self.config, ref, query=query, max_tokens=1200)
                self.assertIn("focus: literal window", focused)
                self.assertIn(evidence, focused)
                self.assertLessEqual(len(focused.encode("utf-8")), 1200)
        # A term still needs a word start: "off" is not a token of the index.
        ref = search(self.config, "compute_backoff_delay", collection="test")[0].ref
        self.assertIn("no literal match", fetch(self.config, ref, query="off", max_tokens=1200))


if __name__ == "__main__":
    unittest.main()
