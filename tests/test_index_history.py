from __future__ import annotations

from pathlib import Path
import shutil
import tempfile
import unittest

from agent_memory.config import load_config
from agent_memory.indexer import sync_all
from agent_memory.search import search
from tests.test_memory_policy import card
from tests.test_memory_system import make_home


QUERIES = ("갱신", "계약 갱신", "자동갱신 조항", "retention policy", "meeting",
           "deployment window agreed", "settle_receipt", "README.md")


class IndexHistoryTests(unittest.TestCase):
    def test_edit_restore_and_rebuild_do_not_change_results(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            (source / "notes").mkdir(parents=True)
            (source / "ko").mkdir()
            home = make_home(root, source)
            # Same-body notes tie in every FTS channel. The Korean notes put more
            # than the candidate limit of chunks on a mid-word 2-character term.
            for index in range(12):
                (source / "notes" / ("meeting-%02d.md" % index)).write_text(
                    "# Meeting\n\nRetention policy review and deployment window agreed.\n")
            for index in range(48):
                (source / "ko" / ("계약-%02d.md" % index)).write_text(
                    "# 계약 메모 %d\n\n거래처 %d의 계약은 자동갱신 조항을 포함한다. 담당 %d번.\n" % (index, index, index % 7))
            (source / "handler.py").write_text("def settle_receipt():\n    return '완료'\n\n\nresult = settle_receipt()\n")
            (source / "README.md").write_text("# Readme\n\nretention policy pointer\n")
            (home / "vault" / "README.md").write_text("# Vault\n\nretention policy navigation\n")
            (home / "vault" / "card.md").write_text(card() + "Retention policy: 자동갱신 계약은 갱신 30일 전에 검토한다.\n")
            config = load_config(home)

            def snapshot():
                return {query: [(hit.relative_path, hit.locator, hit.start_line, hit.end_line, hit.snippet,
                                 hit.rank, hit.ref, hit.score) for hit in search(config, query)]
                        for query in QUERIES}

            sync_all(config)
            fresh = snapshot()
            self.assertTrue(all(fresh.values()))
            # Reopen and undo the first source result of each query: same bytes, newer index rows.
            victims = {next(Path(hit.path) for hit in search(config, query) if hit.collection == "test")
                       for query in QUERIES}
            originals = {path: path.read_bytes() for path in victims}
            for path, data in originals.items():
                path.write_bytes(data + "임시 수정\n".encode("utf-8"))
            sync_all(config)
            for path, data in originals.items():
                path.write_bytes(data)
            sync_all(config)
            self.assertEqual(snapshot(), fresh)
            shutil.rmtree(home / ".state")
            sync_all(config)
            self.assertEqual(snapshot(), fresh)


if __name__ == "__main__":
    unittest.main()
