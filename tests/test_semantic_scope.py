from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_memory.config import load_config
from agent_memory.indexer import sync_all
from agent_memory.semantic import build
from tests.test_memory_system import make_home


class SemanticScopeTests(unittest.TestCase):
    def test_custom_collection_and_literal_directory_prefix(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            for name in ("selected_notes", "selectedXnotes"):
                (source / name).mkdir(parents=True)
                (source / name / "example.md").write_text("# " + name + "\n\nScoped evidence.")
            config = load_config(make_home(root, source))
            sync_all(config)
            def fake_worker(configuration, request):
                self.assertEqual(len(request["records"]), 1)
                self.assertIn("selected_notes", request["records"][0]["text"])
                Path(request["vectors"]).write_bytes(b"synthetic vector placeholder")
                return {"spans": [], "max_tokens": 20, "runtime_versions": {}}
            with patch("agent_memory.semantic._model_fingerprint", return_value="fixture"), patch("agent_memory.semantic._worker", side_effect=fake_worker):
                self.assertEqual(build(config, "test", ["selected_notes"])["chunks"], 1)
            manifest = json.loads((config.database.parent / "semantic/manifest.json").read_text())
            self.assertEqual(manifest["collection"], "test")

    def test_memory_vault_and_escaping_prefixes_are_rejected_before_inference(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            config = load_config(make_home(root, source))
            with patch("agent_memory.semantic._worker") as worker:
                for collection, prefixes in (("vault", []), ("test", ["../secret"]), ("test", ["/secret"])):
                    with self.assertRaises(ValueError):
                        build(config, collection, prefixes)
                worker.assert_not_called()
