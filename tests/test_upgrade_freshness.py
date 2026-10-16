"""A projection or card-metadata version change must not look fresh."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_memory.cli import _status
from agent_memory.config import load_config, source_by_name
from agent_memory.database import connect
from agent_memory.indexer import ensure_fresh, sync_all
from agent_memory.memory_policy import METADATA_VERSION
from agent_memory.search import search
from tests.test_memory_policy import card
from tests.test_memory_system import make_home


class UpgradeFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        source = root / "source"
        source.mkdir()
        (source / "notes.md").write_text("# Notes\n\nupgradeneedle in a Markdown note\n", encoding="utf-8")
        (source / "plain.txt").write_text("plainneedle in a text file\n", encoding="utf-8")
        self.home = make_home(root, source)
        (self.home / "vault" / "card.md").write_text(card())
        self.config = load_config(self.home)
        sync_all(self.config)

    def outdated(self):
        rendered, fresh = _status(self.config, True, max_age_hours=8)
        return fresh, json.loads(rendered)["outdated_documents"]

    def execute(self, statement):
        connection = connect(self.config)
        try:
            connection.execute(statement)
            connection.commit()
        finally:
            connection.close()

    def test_old_chunk_policy_is_not_fresh_and_ensure_fresh_resyncs(self):
        self.assertEqual(self.outdated(), (True, {}))
        # An index written before a Markdown chunk-policy change.
        self.execute("UPDATE documents SET chunker_version='0:older' WHERE extension='.md' AND collection_name='test'")
        self.assertEqual(search(self.config, "upgradeneedle", collection="test"), [])
        self.assertEqual(self.outdated(), (False, {"test": 1}))

        stats = ensure_fresh(self.config, source_by_name(self.config, "test"), max_age_seconds=3600)
        self.assertEqual((stats.indexed, stats.unchanged), (1, 1))
        self.assertEqual(self.outdated(), (True, {}))
        self.assertTrue(search(self.config, "upgradeneedle", collection="test"))
        # Within the SLA and current, ensure-fresh still skips the scan.
        self.assertTrue(ensure_fresh(self.config, source_by_name(self.config, "test"), max_age_seconds=3600).message.startswith("fresh"))

    def test_old_card_metadata_version_is_not_fresh_and_ensure_fresh_resyncs(self):
        self.execute("UPDATE memory_metadata SET version='0:older'")
        self.assertEqual(search(self.config, "policyneedle", collection="vault"), [])
        self.assertEqual(self.outdated(), (False, {"vault": 1}))

        ensure_fresh(self.config, source_by_name(self.config, "vault"), max_age_seconds=3600)
        self.assertEqual(self.outdated(), (True, {}))
        connection = connect(self.config, readonly=True)
        try:
            versions = {row[0] for row in connection.execute("SELECT version FROM memory_metadata")}
        finally:
            connection.close()
        self.assertEqual(versions, {METADATA_VERSION})
        self.assertTrue(search(self.config, "policyneedle", collection="vault"))


if __name__ == "__main__":
    unittest.main()
