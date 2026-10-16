from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_memory_system import make_home
from agent_memory.chunking import chunk_text
from agent_memory.config import load_config, source_by_name
from agent_memory.database import connect
from agent_memory.extractors import MARKDOWN_HEADING_EXTENSIONS, extract_file
from agent_memory.indexer import sync_all
from agent_memory.migration import migrate_projections
from agent_memory.projection import legacy_fingerprint
from agent_memory.search import StaleReferenceError, fetch, search


RUNBOOK = """# Runbook

Restart the worker when the queue stalls.

```bash
# restart the worker
systemctl restart worker
# verify health
curl -s localhost/health
```

## Rollback

Revert the release tag.
"""


def headings(text: str) -> list:
    return [chunk.heading for chunk in chunk_text(text, 2400, 240)]


class MarkdownFenceTests(unittest.TestCase):
    def test_fenced_comments_are_not_headings_and_do_not_split_the_block(self) -> None:
        chunks = chunk_text(RUNBOOK, 2400, 240)
        self.assertEqual([chunk.heading for chunk in chunks], ["Runbook", "Rollback"])
        self.assertIn("```bash\n# restart the worker\nsystemctl restart worker\n# verify health\n", chunks[0].text)
        self.assertTrue(chunks[0].text.endswith("curl -s localhost/health\n```"))
        self.assertEqual((chunks[1].start_line, chunks[1].locator), (12, "Rollback"))

    def test_fence_close_requires_same_character_length_and_no_info(self) -> None:
        tilde = ["# Setup", "~~~~ python", "# not a heading", "~~~", "```", "# still code", "~~~~~  ", "# After", "body"]
        self.assertEqual(headings("\n".join(tilde)), ["Setup", "After"])
        self.assertEqual(headings("```\n# inside\n``` not a close\n# still inside\n```\n# Outside\n"), ["", "Outside"])
        self.assertEqual(headings("   ```\n# inside\n   ```\n    ```\n# Real\n"), ["", "Real"])
        self.assertEqual(headings("```inline``` code\n# Real\n"), ["", "Real"])
        self.assertEqual(headings("~~~ `x`\n# inside\n~~~\n# Real\n"), ["", "Real"])
        # A marker of the other character, even as long and without info, does not close.
        self.assertEqual(headings("~~~\n```\n# inside\n~~~\n# Real\n"), ["", "Real"])
        self.assertEqual(headings("```\n~~~\n# inside\n```\n# Real\n"), ["", "Real"])

    def test_fence_in_a_list_item_ends_with_the_item(self) -> None:
        # Unclosed: the item, and its fence, end at the first line left of the item's content.
        unclosed = "# Setup\n\n1. Install:\n   ```bash\n   make install\n\n## Usage\n\nrun it\n\n## Rollback\n"
        self.assertEqual(headings(unclosed), ["Setup", "Usage", "Rollback"])
        # A closer may sit up to three spaces past the item's content column.
        self.assertEqual(headings("- step\n  ```sh\n  x\n    ```\n\n## B\n\n## C"), ["", "B", "C"])
        # A fenced comment inside the item is still code.
        self.assertEqual(headings("# Steps\n\n- run:\n  ```sh\n  # comment\n  make\n  ```\n\n## Next\n"),
                         ["Steps", "Next"])
        self.assertEqual(headings("- ```sh\n  # comment\n  ```\n## After\n"), ["", "After"])

    def test_unclosed_fence_runs_to_the_end_of_the_document(self) -> None:
        chunks = chunk_text("# Title\n\n```sh\n# step one\necho one\n# step two\necho two\n", 2400, 240)
        self.assertEqual([chunk.heading for chunk in chunks], ["Title"])
        self.assertTrue(chunks[0].text.endswith("# step two\necho two"))

    def test_markdown_title_skips_fenced_comments(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "notes.md"
            path.write_text("```sh\n# install deps\n```\n\n# Deploy notes\n\nbody\n", encoding="utf-8")
            result = extract_file(path, load_config(make_home(root, root)))
            self.assertEqual(result.title, "Deploy notes")
            self.assertEqual([chunk.heading for chunk in result.chunks], ["", "Deploy notes"])

    def test_existing_index_rechunks_markdown_and_keeps_other_refs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "runbook.md").write_text(RUNBOOK, encoding="utf-8")
            (source / "notes.txt").write_text("# plain\n```\n# txtfence sentinel\n```\n", encoding="utf-8")
            (source / "tool.py").write_text("# pyfence sentinel\nVALUE = 1\n", encoding="utf-8")
            config = load_config(make_home(root, source))
            # Reproduce an index written before fence-aware headings: the old
            # chunker output under the old Markdown chunk policy.
            with patch("agent_memory.chunking.FENCE_RE", re.compile(r"(?!)")), patch(
                "agent_memory.projection.MARKDOWN_HEADING_EXTENSIONS", frozenset()
            ):
                sync_all(config)
                old = search(config, "systemctl restart worker", collection="test")[0]
                others = [search(config, term, collection="test")[0] for term in ("txtfence", "pyfence")]
            self.assertEqual(old.heading, "restart the worker")

            with self.assertRaises(StaleReferenceError):
                fetch(config, old.ref)
            stats = sync_all(config)[0]
            self.assertEqual((stats.indexed, stats.unchanged), (1, 2))
            with self.assertRaises(StaleReferenceError):
                fetch(config, old.ref)
            current = search(config, "systemctl restart worker", collection="test")[0]
            self.assertNotEqual(current.ref, old.ref)
            self.assertEqual(current.heading, "Runbook")
            connection = connect(config, readonly=True)
            try:
                rows = connection.execute(
                    "SELECT c.heading FROM chunks c JOIN documents d ON d.id=c.document_id WHERE d.extension='.md' ORDER BY c.ordinal"
                ).fetchall()
            finally:
                connection.close()
            self.assertEqual([row[0] for row in rows], ["Runbook", "Rollback"])
            for term, hit in zip(("txtfence", "pyfence"), others):
                self.assertEqual(search(config, term, collection="test")[0].ref, hit.ref)
                self.assertIn("sentinel", fetch(config, hit.ref))

    def test_legacy_markdown_projection_is_rebuilt_not_adopted(self) -> None:
        for extension in sorted(MARKDOWN_HEADING_EXTENSIONS - {".md"}):
            with self.subTest(extension=extension), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "source"
                source.mkdir()
                (source / "runbook.md").write_text(RUNBOOK, encoding="utf-8")
                (source / ("runbook" + extension)).write_text(RUNBOOK, encoding="utf-8")
                (source / "notes.txt").write_text("legacyneedle evidence", encoding="utf-8")
                home = make_home(root, source)
                settings = json.loads((home / "config.json").read_text(encoding="utf-8"))
                settings["sources"][0]["include_extensions"].append(extension)
                (home / "config.json").write_text(json.dumps(settings), encoding="utf-8")
                config = load_config(home)
                # Chunks written by the fence-unaware chunker, labelled with the legacy projection.
                with patch("agent_memory.chunking.FENCE_RE", re.compile(r"(?!)")):
                    sync_all(config)
                legacy = "1:" + legacy_fingerprint(config, source_by_name(config, "test"), "old-a", "old-b")
                connection = connect(config)
                connection.execute("UPDATE documents SET chunker_version=?", (legacy,))
                connection.commit()
                connection.close()
                self.assertEqual(migrate_projections(config, "old-a", "old-b"), {"adopted": 1, "needs_rebuild": 2, "rejected": 0})
                stats = sync_all(config)[0]
                self.assertEqual((stats.indexed, stats.unchanged), (2, 1))
                connection = connect(config, readonly=True)
                try:
                    rows = connection.execute(
                        "SELECT c.heading FROM chunks c JOIN documents d ON d.id=c.document_id "
                        "WHERE d.extension=? ORDER BY c.ordinal", (extension,)).fetchall()
                finally:
                    connection.close()
                self.assertEqual([row[0] for row in rows], ["Runbook", "Rollback"])


if __name__ == "__main__":
    unittest.main()
