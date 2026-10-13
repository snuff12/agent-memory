from __future__ import annotations

import io
import json
import os
import sqlite3
import stat
import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from agent_memory.cli import _doctor, main
from agent_memory.config import load_config
from agent_memory.database import SCHEMA_VERSION, connect


def _write_home(root: Path, database: str = ".state/index.sqlite3") -> Path:
    home = root / "memory"
    (home / "vault" / "inbox").mkdir(parents=True)
    payload = {
        "version": 1,
        "database": database,
        "ingestion": {},
        "retrieval": {},
        "sources": [],
    }
    (home / "config.json").write_text(json.dumps(payload), encoding="utf-8")
    return home


def _permissions(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class StorageHardeningTests(unittest.TestCase):
    def test_database_must_resolve_below_home_state_even_if_config_is_bypassed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outside = root / "outside.sqlite3"
            home = _write_home(root, str(outside))
            with self.assertRaisesRegex(ValueError, "under .*\\.state"):
                load_config(home)

            (home / "config.json").write_text(
                json.dumps({"version": 1, "database": ".state/../outside.sqlite3", "sources": []}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "under .*\\.state"):
                load_config(home)

            (home / "config.json").write_text(
                json.dumps({"version": 1, "database": ".state/index.sqlite3", "sources": []}),
                encoding="utf-8",
            )
            config = load_config(home)
            with self.assertRaisesRegex(ValueError, "under .*\\.state"):
                connect(replace(config, database=outside))

    def test_state_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = _write_home(root)
            outside = root / "external-state"
            outside.mkdir()
            (home / ".state").symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "must not be a symlink"):
                load_config(home)

    def test_connect_does_not_chmod_an_existing_database_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = _write_home(root, ".state/nested/index.sqlite3")
            parent = home / ".state" / "nested"
            parent.mkdir(parents=True)
            os.chmod(parent, 0o750)

            config = load_config(home)
            connection = connect(config)
            connection.close()

            self.assertEqual(_permissions(parent), 0o750)
            self.assertEqual(_permissions(config.database), 0o600)

    def test_readonly_sqlite_uri_percent_encodes_reserved_filename_characters(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = _write_home(root, ".state/index #?%.sqlite3")
            config = load_config(home)
            writable = connect(config)
            writable.close()

            readonly = connect(config, readonly=True)
            try:
                version = readonly.execute(
                    "SELECT value FROM metadata WHERE key='schema_version'"
                ).fetchone()[0]
            finally:
                readonly.close()
            self.assertEqual(version, SCHEMA_VERSION)

    def test_doctor_reports_home_vault_config_and_state_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = _write_home(root)
            os.chmod(home, 0o755)
            os.chmod(home / "vault", 0o750)
            os.chmod(home / "config.json", 0o644)
            config = load_config(home)

            rendered, ok = _doctor(config, True)
            self.assertFalse(ok)
            checks = {item["name"]: item for item in json.loads(rendered)["checks"]}
            self.assertFalse(checks["home_permissions"]["ok"])
            self.assertFalse(checks["vault_permissions"]["ok"])
            self.assertFalse(checks["config_permissions"]["ok"])
            self.assertTrue(checks["state_permissions"]["ok"])

            os.chmod(home, 0o700)
            os.chmod(home / "vault", 0o700)
            os.chmod(home / "config.json", 0o600)
            rendered, _ = _doctor(config, True)
            checks = {item["name"]: item for item in json.loads(rendered)["checks"]}
            for name in ("home_permissions", "vault_permissions", "config_permissions"):
                self.assertTrue(checks[name]["ok"], checks[name]["detail"])

    def test_doctor_rejects_orphan_indexes_and_insecure_state_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = _write_home(root)
            config = load_config(home)
            active = connect(config)
            active.close()
            legacy = home / ".state" / "legacy"
            legacy.mkdir(mode=0o700)
            orphan = legacy / "old.sqlite3"
            orphan.write_bytes(b"derived plaintext")
            os.chmod(orphan, 0o644)

            rendered, ok = _doctor(config, True)
            self.assertFalse(ok)
            checks = {item["name"]: item for item in json.loads(rendered)["checks"]}
            self.assertFalse(checks["orphan_indexes"]["ok"])
            self.assertIn(str(orphan), checks["orphan_indexes"]["detail"])
            self.assertFalse(checks["state_file_permissions"]["ok"])
            self.assertIn("0644", checks["state_file_permissions"]["detail"])

    def test_main_scopes_private_umask_and_capture_holds_sync_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = _write_home(root)
            events = []

            @contextmanager
            def observed_lock(config):  # type: ignore[no-untyped-def]
                active_mask = os.umask(0o077)
                os.umask(active_mask)
                events.append(("enter", active_mask, config.database.parent))
                try:
                    yield
                finally:
                    events.append(("exit", active_mask, config.database.parent))

            original_mask = os.umask(0o022)
            try:
                stdout = io.StringIO()
                with patch("agent_memory.cli.sync_lock", observed_lock), redirect_stdout(stdout):
                    exit_code = main(
                        [
                            "--home",
                            str(home),
                            "capture",
                            "--kind",
                            "decision",
                            "--title",
                            "private capture",
                            "--body",
                            "preserve this",
                            "--no-sync",
                        ]
                    )
                restored_mask = os.umask(0o022)
            finally:
                os.umask(original_mask)

            self.assertEqual(exit_code, 0)
            self.assertEqual(restored_mask, 0o022)
            self.assertEqual([event[0] for event in events], ["enter", "exit"])
            self.assertTrue(all(event[1] == 0o077 for event in events))
            captured = Path(stdout.getvalue().strip())
            self.assertEqual(_permissions(captured), 0o600)

    def test_v1_migration_preserves_chunks_and_scopes_path_uniqueness(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = _write_home(root)
            state = home / ".state"
            state.mkdir(mode=0o700)
            database = state / "index.sqlite3"
            raw = sqlite3.connect(str(database))
            try:
                raw.executescript(
                    """
                    PRAGMA foreign_keys=ON;
                    CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    INSERT INTO metadata(key, value) VALUES('schema_version', '1');
                    CREATE TABLE collections (
                        name TEXT PRIMARY KEY, root_path TEXT NOT NULL, description TEXT NOT NULL,
                        kind TEXT NOT NULL, trust TEXT NOT NULL, priority REAL NOT NULL,
                        enabled INTEGER NOT NULL, last_sync_at TEXT, last_sync_ok INTEGER,
                        last_sync_message TEXT
                    );
                    CREATE TABLE documents (
                        id TEXT PRIMARY KEY,
                        collection_name TEXT NOT NULL REFERENCES collections(name) ON DELETE CASCADE,
                        path TEXT NOT NULL UNIQUE, relative_path TEXT NOT NULL, real_path TEXT NOT NULL,
                        title TEXT NOT NULL, extension TEXT NOT NULL, size_bytes INTEGER NOT NULL,
                        mtime_ns INTEGER NOT NULL, source_sha256 TEXT NOT NULL, generation TEXT NOT NULL,
                        extractor TEXT NOT NULL, parser_version TEXT NOT NULL, chunker_version TEXT NOT NULL,
                        status TEXT NOT NULL, error TEXT NOT NULL DEFAULT '', indexed_at TEXT NOT NULL
                    );
                    CREATE INDEX documents_collection_idx ON documents(collection_name);
                    CREATE INDEX documents_generation_idx ON documents(generation);
                    CREATE TABLE chunks (
                        id TEXT PRIMARY KEY,
                        document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                        ordinal INTEGER NOT NULL, heading TEXT NOT NULL, locator TEXT NOT NULL,
                        start_line INTEGER, end_line INTEGER, page INTEGER, text TEXT NOT NULL,
                        token_estimate INTEGER NOT NULL, UNIQUE(document_id, ordinal)
                    );
                    INSERT INTO collections(name, root_path, description, kind, trust, priority, enabled)
                    VALUES('a', '/a', '', 'test', 'test', 1.0, 1),
                          ('b', '/b', '', 'test', 'test', 1.0, 1);
                    INSERT INTO documents(
                        id, collection_name, path, relative_path, real_path, title, extension,
                        size_bytes, mtime_ns, source_sha256, generation, extractor,
                        parser_version, chunker_version, status, error, indexed_at
                    ) VALUES('d1', 'a', '/shared/file.md', 'file.md', '/shared/file.md', 'file', '.md',
                             1, 1, 'hash', 'generation', 'text', '2', '1', 'ok', '', 'now');
                    INSERT INTO chunks(id, document_id, ordinal, heading, locator, text, token_estimate)
                    VALUES('c1', 'd1', 0, '', 'line 1', 'body', 1);
                    """
                )
                raw.commit()
            finally:
                raw.close()

            config = load_config(home)
            migrated = connect(config)
            try:
                self.assertEqual(
                    migrated.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0],
                    SCHEMA_VERSION,
                )
                self.assertEqual(migrated.execute("SELECT COUNT(*) FROM chunks").fetchone()[0], 1)
                self.assertEqual(migrated.execute("PRAGMA foreign_key_check").fetchall(), [])
                insert_sql = """INSERT INTO documents(
                    id, collection_name, path, relative_path, real_path, title, extension,
                    size_bytes, mtime_ns, source_sha256, generation, extractor,
                    parser_version, chunker_version, status, error, indexed_at
                ) VALUES(?, ?, '/shared/file.md', 'file.md', '/shared/file.md', 'file', '.md',
                         1, 1, 'hash', 'generation', 'text', '2', '1', 'ok', '', 'now')"""
                migrated.execute(insert_sql, ("d2", "b"))
                with self.assertRaises(sqlite3.IntegrityError):
                    migrated.execute(insert_sql, ("d3", "a"))
            finally:
                migrated.close()

    def test_unknown_schema_version_fails_closed_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = _write_home(root)
            state = home / ".state"
            state.mkdir(mode=0o700)
            database = state / "index.sqlite3"
            raw = sqlite3.connect(str(database))
            raw.executescript(
                "CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);"
                "INSERT INTO metadata(key, value) VALUES('schema_version', '999');"
            )
            raw.close()

            config = load_config(home)
            with self.assertRaisesRegex(sqlite3.DatabaseError, "Unsupported index schema version 999"):
                connect(config)
            with self.assertRaisesRegex(sqlite3.DatabaseError, "not readable"):
                connect(config, readonly=True)

            raw = sqlite3.connect(str(database))
            try:
                value = raw.execute(
                    "SELECT value FROM metadata WHERE key='schema_version'"
                ).fetchone()[0]
            finally:
                raw.close()
            self.assertEqual(value, "999")

    def test_secure_fts_requires_sqlite_342_before_creating_database_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = _write_home(root)
            config = load_config(home)
            with patch("agent_memory.database.sqlite3.sqlite_version_info", (3, 41, 2)), patch(
                "agent_memory.database.sqlite3.sqlite_version", "3.41.2"
            ):
                with self.assertRaisesRegex(RuntimeError, "requires SQLite 3.42.0"):
                    connect(config)
            self.assertFalse(config.database.exists())


if __name__ == "__main__":
    unittest.main()
