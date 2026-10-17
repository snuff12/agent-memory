from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from agent_memory.config import load_config
from agent_memory.database import connect
from agent_memory.indexer import sync_all
from agent_memory.migration import migrate_exclusions
from agent_memory.search import StaleReferenceError, fetch, search
from tests.test_memory_system import make_home


def add_exclusions(home: Path, globs: list[str], collection: str = "test", **ingestion):
    path = home / "config.json"
    config = json.loads(path.read_text())
    next(s for s in config["sources"] if s["name"] == collection)["exclude_globs"] = globs
    config["ingestion"].update(ingestion)
    path.write_text(json.dumps(config))
    return load_config(home)


def make_pdf(path: Path) -> None:
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=200)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                             NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"):
        DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 30 150 Td (projectionpdfneedle evidence) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    writer.write(path)


class ExclusionMigrationTests(unittest.TestCase):
    def test_reuses_identical_pdf_and_text_but_requires_sync_to_remove_excluded_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            (source / "blocked").mkdir(parents=True)
            (source / "allowed.md").write_text("# Source\nprojectiontextneedle evidence\n")
            (source / "blocked/secret.md").write_text("excludedneedle evidence")
            make_pdf(source / "allowed.pdf")
            home = make_home(root, source)
            initial = load_config(home)
            sync_all(initial)
            text_ref = search(initial, "projectiontextneedle", collection="test")[0].ref
            pdf_ref = search(initial, "projectionpdfneedle", collection="test")[0].ref
            denied_ref = search(initial, "excludedneedle", collection="test")[0].ref
            config = add_exclusions(home, ["blocked"])
            lock = home / ".state/sync.lock"
            lock.unlink()
            before = config.database.read_bytes(), config.database.stat().st_mtime_ns
            preview = migrate_exclusions(config, "test", [], dry_run=True)
            self.assertEqual((preview["reusable"], preview["excluded"], preview["adopted"]), (2, 1, 0))
            self.assertEqual((config.database.read_bytes(), config.database.stat().st_mtime_ns), before)
            self.assertFalse(lock.exists())
            with patch("agent_memory.indexer.extract_file", side_effect=AssertionError("must reuse validated evidence")):
                applied = migrate_exclusions(config, "test", [])
                self.assertEqual(applied["adopted"], 2)
                self.assertIn("projectionpdfneedle", fetch(config, pdf_ref))
                self.assertIn("projectiontextneedle", fetch(config, text_ref))
                with self.assertRaises(StaleReferenceError):
                    fetch(config, denied_ref)
                self.assertFalse(search(config, "excludedneedle", collection="test"))
                connection = connect(config, readonly=True)
                self.assertEqual(connection.execute("SELECT last_sync_ok FROM collections WHERE name='test'").fetchone()[0], 0)
                connection.close()
                synced = sync_all(config, "test")[0]
                self.assertEqual((synced.indexed, synced.unchanged, synced.removed, synced.errors), (0, 2, 1, 0))
            self.assertEqual(search(config, "projectionpdfneedle", collection="test")[0].ref, pdf_ref)

    def test_rejects_widening_and_cannot_adopt_a_fabricated_previous_policy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "allowed.md").write_text("evidence")
            home = make_home(root, source)
            sync_all(add_exclusions(home, ["old*"]))
            wider = add_exclusions(home, ["new*"])
            with self.assertRaises(ValueError):
                migrate_exclusions(wider, "test", ["old*"])
            narrowed = add_exclusions(home, ["old*", "new*"])
            rejected = migrate_exclusions(narrowed, "test", [])
            self.assertEqual((rejected["adopted"], rejected["needs_rebuild"]), (0, 1))
            self.assertEqual(migrate_exclusions(narrowed, "test", ["old*"])["adopted"], 1)

    def test_hash_checks_catch_same_size_mtime_edits_and_pipeline_changes_rebuild(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            path = source / "allowed.md"
            path.write_text("evidence original")
            home = make_home(root, source)
            sync_all(load_config(home))
            stat = path.stat()
            path.write_text("evidence tampered")
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            config = add_exclusions(home, ["skip*"])
            result = migrate_exclusions(config, "test", [])
            self.assertEqual((result["adopted"], result["needs_rebuild"]), (0, 1))
            self.assertFalse(search(config, "original", collection="test"))
            sync_all(config, "test")
            changed_pipeline = add_exclusions(home, ["skip*", "more*"], chunk_chars=350)
            result = migrate_exclusions(changed_pipeline, "test", ["skip*"])
            self.assertEqual((result["adopted"], result["needs_rebuild"]), (0, 1))

    def test_pdf_parser_changes_and_old_card_validation_are_not_adopted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            make_pdf(source / "allowed.pdf")
            home = make_home(root, source)
            card = home / "vault/card.md"
            card.write_text('---\nkind: fact\nstatus: reviewed\nscope: global\nsensitivity: private\nconfidence: high\nsource: fixture\n---\nevidence\n')
            sync_all(load_config(home))
            config = add_exclusions(home, ["skip*"])
            with patch("agent_memory.projection._package_version", return_value="changed"):
                self.assertEqual(migrate_exclusions(config, "test", [])["needs_rebuild"], 1)
            config = add_exclusions(home, ["skip*"], collection="vault")
            connection = connect(config)
            connection.execute("UPDATE memory_metadata SET version='old'")
            connection.commit()
            connection.close()
            result = migrate_exclusions(config, "vault", [])
            self.assertEqual((result["adopted"], result["needs_rebuild"]), (0, 1))

    def test_interruption_rolls_back_adoptions_and_symlinks_are_excluded(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            for name in ("one.md", "two.md"):
                (source / name).write_text("validated evidence")
            home = make_home(root, source)
            sync_all(load_config(home))
            config = add_exclusions(home, ["skip*"])
            from agent_memory.util import sha256_file
            calls = []

            def fail_after_first(path):
                calls.append(path)
                if len(calls) == 2:
                    raise RuntimeError("synthetic interruption")
                return sha256_file(path)

            with patch("agent_memory.migration.sha256_file", fail_after_first):
                with self.assertRaises(RuntimeError):
                    migrate_exclusions(config, "test", [])
            self.assertEqual(migrate_exclusions(config, "test", [], dry_run=True)["reusable"], 2)
            path = source / "two.md"
            outside = root / "outside.md"
            outside.write_bytes(path.read_bytes())
            path.unlink()
            path.symlink_to(outside)
            result = migrate_exclusions(config, "test", [])
            self.assertEqual((result["adopted"], result["excluded"]), (1, 1))

    def test_access_time_change_during_hashing_does_not_require_reextraction(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            path = source / "allowed.md"
            path.write_text("unchanged evidence")
            path = path.resolve()
            home = make_home(root, source)
            sync_all(load_config(home))
            config = add_exclusions(home, ["skip*"])
            from agent_memory.util import sha256_file
            original_stat = Path.stat
            hashed = []

            def hash_then_touch_access_time(candidate):
                digest = sha256_file(candidate)
                hashed.append(candidate)
                return digest

            def stat_after_access(candidate, *args, **kwargs):
                info = original_stat(candidate, *args, **kwargs)
                if candidate == path and hashed:
                    # Model the filesystem updating atime on a read, without
                    # utime(), which would also change ctime on a real file.
                    fields = {name: getattr(info, name) for name in dir(info) if name.startswith("st_")}
                    fields["st_atime"] += 1
                    fields["st_atime_ns"] += 1_000_000_000
                    return SimpleNamespace(**fields)
                return info

            with patch("agent_memory.migration.sha256_file", hash_then_touch_access_time), patch.object(Path, "stat", stat_after_access):
                result = migrate_exclusions(config, "test", [])
            self.assertEqual((result["adopted"], result["needs_rebuild"]), (1, 0))


if __name__ == "__main__":
    unittest.main()
