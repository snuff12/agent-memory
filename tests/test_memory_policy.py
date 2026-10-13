from __future__ import annotations

import json
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from tests.test_memory_system import make_home
from agent_memory.config import load_config, source_by_name
from agent_memory.database import connect
from agent_memory.indexer import sync_all
from agent_memory.memory_policy import MemoryPolicy, parse_memory
from agent_memory.mcp_server import handle_request
from agent_memory.migration import migrate_projections
from agent_memory.projection import legacy_fingerprint, projection_fingerprint
from agent_memory.python_chunking import chunk_python
from agent_memory.search import search, fetch, StaleReferenceError
from agent_memory.cli import main
from agent_memory.util import estimate_tokens
from agent_memory.semantic import MODEL_REVISION, search_semantic


def card(**fields):
    metadata = dict(kind="fact", status="reviewed", scope="global", sensitivity="private", confidence="high", source="fixture")
    metadata.update(fields)
    return "---\n" + "\n".join(key + ": " + json.dumps(value) for key, value in metadata.items()) + "\n---\npolicyneedle evidence\n"


class MemoryPolicyTests(unittest.TestCase):
    def test_metadata_backfill_preserves_existing_chunk_ref(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            home = make_home(root, source)
            (home / "vault" / "card.md").write_text(card())
            config = load_config(home)
            sync_all(config)
            ref = search(config, "policyneedle", collection="vault")[0].ref
            connection = connect(config)
            connection.execute("DELETE FROM memory_metadata")
            connection.commit()
            connection.close()
            self.assertFalse(search(config, "policyneedle", collection="vault"))
            result = sync_all(config, "vault")[0]
            self.assertEqual(result.unchanged, 1)
            self.assertEqual(search(config, "policyneedle", collection="vault")[0].ref, ref)
            self.assertIn("policyneedle", fetch(config, ref))

    def test_filter_before_limit_and_fetch_use_same_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            home = make_home(root, source)
            for index in range(60):
                (home / "vault" / ("blocked-%02d.md" % index)).write_text(card(status="inbox") + "policyneedle " * 50)
            target = home / "vault" / "target.md"
            target.write_text(card(scope="project:demo-project"))
            config = load_config(home)
            sync_all(config)
            self.assertEqual(search(config, "policyneedle", collection="vault"), [])
            policy = MemoryPolicy(project="demo-project")
            hits = search(config, "policyneedle", collection="vault", limit=1, policy=policy)
            self.assertEqual(len(hits), 1)
            self.assertEqual(Path(hits[0].path), target.resolve())
            self.assertIn("policyneedle", fetch(config, hits[0].ref, policy=policy))
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(main(["--home", str(home), "read", hits[0].ref, "--project", "demo-project"]), 0)
            self.assertIn("policyneedle", stdout.getvalue())
            for denied in (None, MemoryPolicy(project="other"), MemoryPolicy(project="demo-project", shareable_only=True)):
                with self.assertRaises(StaleReferenceError):
                    fetch(config, hits[0].ref, policy=denied)
            target.write_text(card(scope="project:demo-project") + "changed")
            with self.assertRaises(StaleReferenceError):
                fetch(config, hits[0].ref, policy=policy)

    def test_status_scope_validity_and_types_fail_closed(self):
        cases = [({}, True), ({"status": "inbox"}, False), ({"status": "superseded"}, False),
            ({"scope": "project:demo-project"}, False), ({"scope": "domain:education"}, False),
            ({"valid_from": "2999-01-01"}, False), ({"review_after": "2000-01-01"}, False),
            ({"valid_from": "2000-01-01T12:00:00+09:00"}, True),
            ({"valid_from": "2026-10-12T12:00:00"}, False), ({"review_after": "bad"}, False),
            ({"status": ["reviewed"]}, False), ({"scope": ["global"]}, False),
            ({"kind": "unknown"}, False), ({"sensitivity": "unknown"}, False),
            ({"source": ""}, False), ({"confidence": []}, False),
            ({"valid_from": {"x": 1}}, False), ({"review_after": "2999-01-01"}, True)]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            home = make_home(root, source)
            target = home / "vault" / "target.md"
            config = load_config(home)
            for fields, expected in cases:
                with self.subTest(fields=fields):
                    target.write_text(card(**fields))
                    sync_all(config, "vault")
                    self.assertEqual(bool(search(config, "policyneedle", collection="vault")), expected)

    def test_yaml_duplicates_aliases_and_oversized_frontmatter_are_rejected(self):
        for text in (card().replace('kind: "fact"', 'kind: fact\nkind: decision'),
                     card().replace('source: "fixture"', 'source: &a [*a]'),
                     card().replace('source: "fixture"', 'source: ' + 'a' * 17000)):
            self.assertTrue(parse_memory(Path("card.md"), text)["error"])

    def test_mcp_rejects_invalid_policy_types(self):
        for args in ({"memory_status": []}, {"project": "bad:scope"}, {"shareable_only": "true"}):
            response = handle_request(None, {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                "name": "memory_fetch", "arguments": dict(ref="invalid", **args)}})
            self.assertEqual(response["error"]["code"], -32602)

    def test_pdf_dependency_change_does_not_change_text_projection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            config = load_config(make_home(root, source))
            item = source_by_name(config, "test")
            with patch("agent_memory.projection._package_version", return_value="old"):
                text_old = projection_fingerprint(config, item, ".md")
                pdf_old = projection_fingerprint(config, item, ".pdf")
            with patch("agent_memory.projection._package_version", return_value="new"):
                self.assertEqual(text_old, projection_fingerprint(config, item, ".md"))
                self.assertNotEqual(pdf_old, projection_fingerprint(config, item, ".pdf"))

    def test_legacy_adoption_preserves_ref_and_fts_rowids(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            path = source / "test.md"
            path.write_text("legacyneedle evidence")
            config = load_config(make_home(root, source))
            sync_all(config)
            hit = search(config, "legacyneedle")[0]
            connection = connect(config)
            old = "1:" + legacy_fingerprint(config, source_by_name(config, "test"), "old-a", "old-b")
            connection.execute("UPDATE documents SET chunker_version=?", (old,))
            before = connection.execute("SELECT rowid FROM chunks").fetchall()[0][0]
            connection.commit()
            connection.close()
            self.assertEqual(migrate_projections(config, "wrong", "old-b")["adopted"], 0)
            self.assertEqual(migrate_projections(config, "old-a", "old-b")["adopted"], 1)
            self.assertIn("legacyneedle", fetch(config, hit.ref))
            connection = connect(config, readonly=True)
            self.assertEqual(connection.execute("SELECT rowid FROM chunks").fetchone()[0], before)
            connection.close()

    def test_python_symbols_decorators_and_large_windows_retain_coverage(self):
        small = "@decorator\ndef small():\n    return '한국어'\n"
        text = "# prefix\n" + small + "\ndef large():\n" + "    x = 12345678\n" * 70 + "    return x\n"
        chunks = chunk_python(text, 180, 20)
        self.assertTrue(any(small.strip() in chunk.text for chunk in chunks))
        covered = {line for chunk in chunks for line in range(chunk.start_line, chunk.end_line + 1)}
        self.assertFalse([line for line, body in enumerate(text.splitlines(), 1) if body.strip() and line not in covered])
        self.assertTrue(all("large" in chunk.heading for chunk in chunks if "x =" in chunk.text))
        self.assertTrue(all(len(chunk.text) <= 180 for chunk in chunks))
        self.assertTrue(chunk_python("def broken(\n", 180, 20))
        long_literal = "def literal():\n    return '" + "a" * 250 + "'\n"
        self.assertTrue(all(len(chunk.text) <= 180 for chunk in chunk_python(long_literal, 180, 20)))
        self.assertTrue(all(len(chunk.text) <= 180 for chunk in chunk_python("def broken(\n" + "x" * 250, 180, 20)))
        exact = "def edge():\n    return '" + "a" * (180 - len("def edge():\n    return ''")) + "'"
        self.assertEqual(len(exact), 180)
        self.assertEqual(len(chunk_python(exact, 180, 20)), 1)

    def test_span_read_validates_bounds_generation_and_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            path = source / "span.md"
            path.write_text("before-sentinel 한국어 spanneedle after-sentinel")
            config = load_config(make_home(root, source))
            sync_all(config)
            hit = search(config, "spanneedle")[0]
            output = fetch(config, hit.ref, max_tokens=256, span=(16, 33))
            self.assertNotIn("before-sentinel", output)
            self.assertLessEqual(estimate_tokens(output), 256)
            for span in ((-1, 1), (0, 1000), (1, 1), (True, 2)):
                with self.assertRaises(ValueError):
                    fetch(config, hit.ref, span=span)
            path.write_text("changed spanneedle")
            with self.assertRaises(StaleReferenceError):
                fetch(config, hit.ref, span=(1, 2))

    def test_semantic_model_change_rejects_old_vectors_before_inference(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            config = load_config(make_home(root, source))
            state = config.database.parent / "semantic"
            state.mkdir(parents=True)
            (state / "manifest.json").write_text(json.dumps(dict(revision=MODEL_REVISION,
                collection="research", experimental=True, embedding_version=1, model_fingerprint="old")))
            with patch("agent_memory.semantic._model_fingerprint", return_value="new"), patch("agent_memory.semantic._worker") as worker:
                with self.assertRaises(ValueError):
                    search_semantic(config, "question")
                worker.assert_not_called()
