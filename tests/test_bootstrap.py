from __future__ import annotations

import io
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from agent_memory.bootstrap import initialize
from agent_memory.cli import main
from agent_memory.config import default_home, load_config
from agent_memory.indexer import sync_all
from agent_memory.search import search


class BootstrapTests(unittest.TestCase):
    def test_empty_home_is_private_and_feedback_is_opt_in(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = initialize(Path(temporary) / "private")
            config = load_config(home)
            self.assertFalse(config.feedback.auto_record_zero_results)
            self.assertTrue(config.secure_delete)
            for directory in (home, home / "knowledge", home / "vault", home / "feedback"):
                self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((home / "config.json").stat().st_mode), 0o600)
            self.assertFalse((home / ".state").exists())
            self.assertFalse(any((home / "knowledge").iterdir()))

    def test_demo_has_bilingual_retrieval_and_no_external_source_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = initialize(Path(temporary) / "private", demo=True)
            config = load_config(home)
            for source in config.sources:
                source.path.relative_to(home)
            sync_all(config)
            self.assertEqual(search(config, "opaque references", collection="research")[0].relative_path, "retrieval.md")
            self.assertEqual(search(config, "간격 반복", collection="research")[0].relative_path, "learning.md")
            self.assertEqual(search(config, "quasarzzzz cosmologyzzzz", collection="research"), [])

    def test_reinitialization_never_overwrites_existing_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = initialize(Path(temporary) / "private", demo=True)
            before = {path.relative_to(home): path.read_bytes() for path in home.rglob("*") if path.is_file()}
            with self.assertRaises(FileExistsError):
                initialize(home)
            after = {path.relative_to(home): path.read_bytes() for path in home.rglob("*") if path.is_file()}
            self.assertEqual(before, after)

    def test_nonempty_unrelated_directory_and_symlink_are_untouched(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private = root / "existing"
            private.mkdir(mode=0o700)
            (private / "important.txt").write_text("keep")
            with self.assertRaises(FileExistsError):
                initialize(private)
            link = root / "link"
            link.symlink_to(private, target_is_directory=True)
            with self.assertRaises(ValueError):
                initialize(link)
            self.assertEqual(list(private.iterdir()), [private / "important.txt"])
            self.assertEqual((private / "important.txt").read_text(), "keep")

    def test_environment_home_and_explicit_cli_override(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment_home, explicit_home = root / "environment", root / "explicit"
            with patch.dict(os.environ, {"AMEM_HOME": str(environment_home)}):
                self.assertEqual(default_home(), environment_home)
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(main(["--home", str(explicit_home), "init"]), 0)
                self.assertFalse(environment_home.exists())
                self.assertTrue((explicit_home / "config.json").exists())
                initialize()
                self.assertEqual(load_config().home, environment_home.resolve())

    def test_missing_configuration_has_actionable_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(FileNotFoundError, "init"):
                load_config(Path(temporary) / "missing")
