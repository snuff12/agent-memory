from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from .config import AppConfig, validate_database_path


SCHEMA_VERSION = "3"
PARSER_VERSION = "3"
CHUNKER_VERSION = "1"
MINIMUM_FTS_SECURE_DELETE_VERSION = (3, 42, 0)


def connect(config: AppConfig, readonly: bool = False) -> sqlite3.Connection:
    database = validate_database_path(config.home, config.database)
    if (
        not readonly
        and config.secure_delete
        and sqlite3.sqlite_version_info < MINIMUM_FTS_SECURE_DELETE_VERSION
    ):
        raise RuntimeError(
            "storage.secure_delete requires SQLite 3.42.0 or newer; found %s"
            % sqlite3.sqlite_version
        )
    if readonly:
        if not database.exists():
            raise FileNotFoundError("Index not found; run `amem sync` first")
        # Path.as_uri() percent-encodes URI delimiters such as '#', '?', and
        # '%'.  Concatenating an unescaped POSIX path would let a filename be
        # interpreted as URI query/fragment syntax by SQLite.
        uri = database.as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=8.0)
    else:
        _ensure_storage_directories(config, database)
        _create_private_database_file(database)
        connection = sqlite3.connect(str(database), timeout=30.0)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=8000")
        if not readonly and config.secure_delete:
            connection.execute("PRAGMA secure_delete=ON")
        stored_version = _stored_schema_version(connection)
        if readonly:
            _validate_readonly_schema(stored_version)
        else:
            _validate_writable_schema(stored_version)
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            _prepare_schema(connection, stored_version)
            if config.secure_delete:
                _configure_fts_secure_delete(connection)
            _chmod_database_files(database)
        return connection
    except BaseException:
        connection.close()
        raise


def _ensure_storage_directories(config: AppConfig, database: Path) -> None:
    """Create only private directories below ``home/.state``.

    Existing directories are deliberately never chmod'ed.  Changing the mode
    of a configured parent is surprising and was dangerous before the database
    path was constrained to the state boundary; ``amem doctor`` reports an
    insecure existing mode instead.
    """
    state = config.home.expanduser().resolve(strict=False) / ".state"
    if state.is_symlink():
        raise ValueError("Memory state directory must not be a symlink: %s" % state)
    try:
        state.mkdir(mode=0o700)
    except FileExistsError:
        if not state.is_dir():
            raise NotADirectoryError("Memory state path is not a directory: %s" % state)

    current = state
    relative_parent = database.parent.relative_to(state.resolve(strict=False))
    for part in relative_parent.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("Database parent must not escape through a symlink: %s" % current)
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            if not current.is_dir():
                raise NotADirectoryError("Database parent is not a directory: %s" % current)


def _create_private_database_file(database: Path) -> None:
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(str(database), flags, 0o600)
    except FileExistsError:
        return
    else:
        os.close(descriptor)


def _chmod_database_files(database: Path) -> None:
    for path in (database, Path(str(database) + "-wal"), Path(str(database) + "-shm")):
        try:
            os.chmod(str(path), 0o600)
        except FileNotFoundError:
            pass


def _metadata_table_exists(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata'"
    ).fetchone()
    return row is not None


def _stored_schema_version(connection: sqlite3.Connection) -> Optional[str]:
    if not _metadata_table_exists(connection):
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' LIMIT 1"
        ).fetchone()
        if row is not None:
            raise sqlite3.DatabaseError("Index schema metadata is missing; refusing to guess its version")
        return None
    row = connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
    if row is None:
        raise sqlite3.DatabaseError("Index schema_version metadata is missing")
    return str(row[0])


def _validate_readonly_schema(stored_version: Optional[str]) -> None:
    if stored_version is None:
        raise sqlite3.DatabaseError("Index schema is not initialized; run `amem sync`")
    if stored_version != SCHEMA_VERSION:
        raise sqlite3.DatabaseError(
            "Index schema version %s is not readable by version %s; run `amem sync` to migrate"
            % (stored_version, SCHEMA_VERSION)
        )


def _validate_writable_schema(stored_version: Optional[str]) -> None:
    if stored_version not in (None, "1", "2", SCHEMA_VERSION):
        raise sqlite3.DatabaseError(
            "Unsupported index schema version %s (expected %s)" % (stored_version, SCHEMA_VERSION)
        )


def _prepare_schema(connection: sqlite3.Connection, stored_version: Optional[str]) -> None:
    if stored_version == "1":
        _migrate_v1_to_v2(connection)
    _initialize(connection)


def _migrate_v1_to_v2(connection: sqlite3.Connection) -> None:
    """Make document paths unique within a collection without rebuilding chunks.

    SQLite cannot drop the v1 table-level ``UNIQUE(path)`` constraint in
    place.  Rebuild only the small ``documents`` manifest while foreign-key
    enforcement is temporarily disabled; the potentially large chunks and FTS
    projections remain untouched.  DDL and row copy are one transaction.
    """
    columns = (
        "id, collection_name, path, relative_path, real_path, title, extension, "
        "size_bytes, mtime_ns, source_sha256, generation, extractor, "
        "parser_version, chunker_version, status, error, indexed_at"
    )
    connection.commit()
    connection.execute("PRAGMA foreign_keys=OFF")
    try:
        connection.execute("BEGIN IMMEDIATE")
        original_count = connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        connection.execute(
            """CREATE TABLE documents_v2 (
                id TEXT PRIMARY KEY,
                collection_name TEXT NOT NULL REFERENCES collections(name) ON DELETE CASCADE,
                path TEXT NOT NULL,
                relative_path TEXT NOT NULL,
                real_path TEXT NOT NULL,
                title TEXT NOT NULL,
                extension TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                source_sha256 TEXT NOT NULL,
                generation TEXT NOT NULL,
                extractor TEXT NOT NULL,
                parser_version TEXT NOT NULL,
                chunker_version TEXT NOT NULL,
                status TEXT NOT NULL,
                error TEXT NOT NULL DEFAULT '',
                indexed_at TEXT NOT NULL,
                UNIQUE(collection_name, path)
            )"""
        )
        connection.execute(
            "INSERT INTO documents_v2(%s) SELECT %s FROM documents" % (columns, columns)
        )
        migrated_count = connection.execute("SELECT COUNT(*) FROM documents_v2").fetchone()[0]
        if migrated_count != original_count:
            raise sqlite3.DatabaseError(
                "Document migration row-count mismatch: %d != %d" % (migrated_count, original_count)
            )
        connection.execute("DROP TABLE documents")
        connection.execute("ALTER TABLE documents_v2 RENAME TO documents")
        connection.execute("CREATE INDEX documents_collection_idx ON documents(collection_name)")
        connection.execute("CREATE INDEX documents_generation_idx ON documents(generation)")
        connection.execute(
            "UPDATE metadata SET value=? WHERE key='schema_version'", (SCHEMA_VERSION,)
        )
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise sqlite3.IntegrityError(
                "Foreign-key violations after schema migration: %d" % len(violations)
            )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.execute("PRAGMA foreign_keys=ON")


def _initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS collections (
            name TEXT PRIMARY KEY,
            root_path TEXT NOT NULL,
            description TEXT NOT NULL,
            kind TEXT NOT NULL,
            trust TEXT NOT NULL,
            priority REAL NOT NULL,
            enabled INTEGER NOT NULL,
            last_sync_at TEXT,
            last_sync_ok INTEGER,
            last_sync_message TEXT
        );
        CREATE TABLE IF NOT EXISTS documents (
            id TEXT PRIMARY KEY,
            collection_name TEXT NOT NULL REFERENCES collections(name) ON DELETE CASCADE,
            path TEXT NOT NULL,
            relative_path TEXT NOT NULL,
            real_path TEXT NOT NULL,
            title TEXT NOT NULL,
            extension TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            source_sha256 TEXT NOT NULL,
            generation TEXT NOT NULL,
            extractor TEXT NOT NULL,
            parser_version TEXT NOT NULL,
            chunker_version TEXT NOT NULL,
            status TEXT NOT NULL,
            error TEXT NOT NULL DEFAULT '',
            indexed_at TEXT NOT NULL,
            UNIQUE(collection_name, path)
        );
        CREATE INDEX IF NOT EXISTS documents_collection_idx ON documents(collection_name);
        CREATE INDEX IF NOT EXISTS documents_collection_id_idx ON documents(collection_name, id);
        CREATE INDEX IF NOT EXISTS documents_generation_idx ON documents(generation);
        CREATE TABLE IF NOT EXISTS memory_metadata (
            document_id TEXT PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
            entry_type TEXT NOT NULL,
            memory_kind TEXT NOT NULL,
            memory_status TEXT NOT NULL,
            scope TEXT NOT NULL,
            sensitivity TEXT NOT NULL,
            valid_from TEXT,
            review_after TEXT,
            error TEXT NOT NULL DEFAULT '',
            version TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS memory_policy_idx ON memory_metadata(memory_status,scope,sensitivity);
        CREATE TABLE IF NOT EXISTS chunks (
            id TEXT PRIMARY KEY,
            document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            ordinal INTEGER NOT NULL,
            heading TEXT NOT NULL,
            locator TEXT NOT NULL,
            start_line INTEGER,
            end_line INTEGER,
            page INTEGER,
            text TEXT NOT NULL,
            token_estimate INTEGER NOT NULL,
            UNIQUE(document_id, ordinal)
        );
        CREATE INDEX IF NOT EXISTS chunks_document_idx ON chunks(document_id, ordinal);
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
            chunk_id UNINDEXED,
            title,
            heading,
            path,
            body,
            tokenize='unicode61 remove_diacritics 2'
        );
        """
    )
    try:
        connection.execute(
            """CREATE VIRTUAL TABLE IF NOT EXISTS chunks_trigram USING fts5(
                chunk_id UNINDEXED, title, heading, path, body, compact,
                tokenize='trigram case_sensitive 0'
            )"""
        )
        trigram = "1"
    except sqlite3.OperationalError:
        trigram = "0"
    for key, value in {
        "schema_version": SCHEMA_VERSION,
        "parser_version": PARSER_VERSION,
        "chunker_version": CHUNKER_VERSION,
        "trigram_available": trigram,
    }.items():
        connection.execute(
            "INSERT INTO metadata(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
    connection.commit()


def trigram_available(connection: sqlite3.Connection) -> bool:
    row = connection.execute("SELECT value FROM metadata WHERE key='trigram_available'").fetchone()
    return bool(row and row[0] == "1")


def _configure_fts_secure_delete(connection: sqlite3.Connection) -> None:
    tables = ["chunks_fts"]
    if trigram_available(connection):
        tables.append("chunks_trigram")
    for table in tables:
        configured = connection.execute(
            "SELECT v FROM %s_config WHERE k='secure-delete'" % table
        ).fetchone()
        if not configured or int(configured[0]) != 1:
            connection.execute(
                "INSERT INTO %s(%s, rank) VALUES('secure-delete', 1)" % (table, table)
            )
    connection.execute(
        "INSERT INTO metadata(key, value) VALUES('secure_delete', '1') "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
    )
    connection.commit()


def delete_document(connection: sqlite3.Connection, document_id: str) -> None:
    rows = connection.execute(
        "SELECT rowid AS storage_rowid, id FROM chunks WHERE document_id=?",
        (document_id,),
    ).fetchall()
    has_trigram = trigram_available(connection)
    for row in rows:
        storage_rowid = int(row["storage_rowid"])
        chunk_id = str(row["id"])
        _delete_projection_row(connection, "chunks_fts", storage_rowid, chunk_id)
        if has_trigram:
            _delete_projection_row(connection, "chunks_trigram", storage_rowid, chunk_id)
    connection.execute("DELETE FROM documents WHERE id=?", (document_id,))


def _delete_projection_row(
    connection: sqlite3.Connection,
    table: str,
    storage_rowid: int,
    chunk_id: str,
) -> None:
    """Delete through the indexed FTS rowid, with a legacy-integrity fallback.

    ``chunk_id`` is an UNINDEXED FTS column, so using it alone makes SQLite
    scan the entire (potentially multi-gigabyte) projection once per chunk.
    New writes deliberately share the ordinary chunk rowid with both FTS
    projections.  The extra chunk-id predicate prevents a mismatched legacy
    rowid from deleting another chunk; only that exceptional case pays for a
    full scan.
    """
    if table not in ("chunks_fts", "chunks_trigram"):
        raise ValueError("Unknown retrieval projection: %s" % table)
    cursor = connection.execute(
        "DELETE FROM %s WHERE rowid=? AND chunk_id=?" % table,
        (storage_rowid, chunk_id),
    )
    if cursor.rowcount != 1:
        connection.execute("DELETE FROM %s WHERE chunk_id=?" % table, (chunk_id,))


@contextmanager
def writer_transaction(connection: sqlite3.Connection) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()
