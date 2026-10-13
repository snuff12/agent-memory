from __future__ import annotations

import fnmatch
import errno
import os
import sqlite3
import stat as stat_module
import sys
import zipfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterator, Optional, Set

from .config import (
    SENSITIVE_BASENAMES,
    SENSITIVE_SUFFIXES,
    AppConfig,
    SourceConfig,
    source_by_name,
)
from .database import CHUNKER_VERSION, PARSER_VERSION, connect, delete_document, trigram_available, writer_transaction
from .extractors import SUPPORTED_EXTENSIONS, extract_file
from .projection import generation_for, stored_chunker_version
from .memory_policy import METADATA_VERSION, parse_memory, store_memory
from .util import normalize_text, sha256_file, stable_document_id, utc_now


@dataclass
class SyncStats:
    collection: str
    scanned: int = 0
    indexed: int = 0
    unchanged: int = 0
    removed: int = 0
    skipped: int = 0
    errors: int = 0
    chunks: int = 0
    message: str = ""


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _is_disappearance_error(exc: OSError) -> bool:
    """Return true only for a path that vanished during reconciliation."""
    return isinstance(exc, FileNotFoundError) or exc.errno in (errno.ENOENT, errno.ENOTDIR)


def _excluded_relative(relative: Path, source: SourceConfig) -> bool:
    text = relative.as_posix()
    return any(fnmatch.fnmatch(text, pattern) for pattern in source.exclude_globs)


def _eligible_file(path: Path, relative: Path, source: SourceConfig) -> bool:
    mode = path.lstat().st_mode
    if stat_module.S_ISLNK(mode) or not stat_module.S_ISREG(mode):
        return False
    if not source.include_hidden and any(part.startswith(".") for part in relative.parts):
        return False
    if source.max_depth is not None and len(relative.parts) > source.max_depth:
        return False
    name = path.name.casefold()
    if name in SENSITIVE_BASENAMES or path.suffix.casefold() in SENSITIVE_SUFFIXES:
        return False
    if _excluded_relative(relative, source):
        return False
    allowed = source.include_extensions or frozenset(SUPPORTED_EXTENSIONS)
    return path.suffix.casefold() in allowed and path.suffix.casefold() in SUPPORTED_EXTENSIONS


def iter_source_files(source: SourceConfig) -> Iterator[Path]:
    root = source.path.resolve(strict=True)
    if root.is_file():
        if _eligible_file(root, Path(root.name), source):
            yield root
        return

    def raise_walk_error(exc: OSError) -> None:
        raise exc

    for directory, dirs, files in os.walk(
        str(root), topdown=True, followlinks=False, onerror=raise_walk_error
    ):
        directory_path = Path(directory)
        relative_dir = directory_path.relative_to(root)
        depth = 0 if str(relative_dir) == "." else len(relative_dir.parts)
        kept_dirs = []
        for name in dirs:
            candidate = directory_path / name
            candidate_relative = candidate.relative_to(root)
            try:
                candidate_mode = candidate.lstat().st_mode
            except OSError as exc:
                if _is_disappearance_error(exc):
                    continue
                raise
            if stat_module.S_ISLNK(candidate_mode):
                continue
            if name.casefold() in source.exclude_dirs:
                continue
            if not source.include_hidden and name.startswith("."):
                continue
            if source.max_depth is not None and depth + 1 >= source.max_depth:
                continue
            if _excluded_relative(candidate_relative, source):
                continue
            kept_dirs.append(name)
        dirs[:] = kept_dirs

        for name in files:
            candidate = directory_path / name
            relative = candidate.relative_to(root)
            try:
                if _eligible_file(candidate, relative, source):
                    real = candidate.resolve(strict=True)
                    if _is_within(real, root):
                        yield candidate
            except OSError as exc:
                # Files can disappear or be replaced between directory listing
                # and inspection. Reconciliation may remove those old rows, but
                # permission/I/O failures make the scan incomplete and must be
                # propagated so unseen documents are preserved.
                if _is_disappearance_error(exc):
                    continue
                raise


@contextmanager
def sync_lock(config: AppConfig):  # type: ignore[no-untyped-def]
    lock_path = config.database.parent / "sync.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    handle = os.fdopen(descriptor, "a+")
    try:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except ImportError:
            pass
        yield
    finally:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except ImportError:
            pass
        handle.close()


def _register_collection(connection: sqlite3.Connection, source: SourceConfig) -> None:
    connection.execute(
        """INSERT INTO collections(
               name, root_path, description, kind, trust, priority, enabled
           ) VALUES(?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(name) DO UPDATE SET
               root_path=excluded.root_path,
               description=excluded.description,
               kind=excluded.kind,
               trust=excluded.trust,
               priority=excluded.priority,
               enabled=excluded.enabled""",
        (
            source.name,
            str(source.path),
            source.description,
            source.kind,
            source.trust,
            source.priority,
            1 if source.enabled else 0,
        ),
    )


def _record_unavailable_source(
    config: AppConfig,
    source: SourceConfig,
    message: str,
) -> None:
    connection = connect(config)
    try:
        _register_collection(connection, source)
        connection.execute(
            "UPDATE collections SET last_sync_at=?, last_sync_ok=0, last_sync_message=? WHERE name=?",
            (utc_now(), message[:1000], source.name),
        )
        connection.commit()
    finally:
        connection.close()


def _record_error_document(
    connection: sqlite3.Connection,
    config: AppConfig,
    source: SourceConfig,
    path: Path,
    relative: Path,
    stat_result: os.stat_result,
    digest: str,
    error: str,
) -> None:
    document_id = stable_document_id(source.name, path.resolve(strict=False))
    existing = connection.execute("SELECT id FROM documents WHERE id=?", (document_id,)).fetchone()
    if existing:
        delete_document(connection, document_id)
    connection.execute(
        """INSERT INTO documents(
               id, collection_name, path, relative_path, real_path, title, extension,
               size_bytes, mtime_ns, source_sha256, generation, extractor,
               parser_version, chunker_version, status, error, indexed_at
           ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'error', ?, ?)""",
        (
            document_id,
            source.name,
            str(path),
            relative.as_posix(),
            str(path.resolve(strict=False)),
            path.stem,
            path.suffix.casefold(),
            stat_result.st_size,
            stat_result.st_mtime_ns,
            digest,
            generation_for(digest, config, source, path.suffix),
            "error",
            PARSER_VERSION,
            stored_chunker_version(config, source, path.suffix),
            error[:1000],
            utc_now(),
        ),
    )


def _index_one(
    connection: sqlite3.Connection,
    config: AppConfig,
    source: SourceConfig,
    path: Path,
    root: Path,
) -> tuple[str, int]:
    real_before = path.resolve(strict=True)
    if not _is_within(real_before, root):
        raise PermissionError("resolved path escapes configured root")
    stat_before = path.stat()
    document_id = stable_document_id(source.name, real_before)
    existing = connection.execute(
        "SELECT size_bytes, mtime_ns, source_sha256, parser_version, chunker_version, status FROM documents WHERE id=?",
        (document_id,),
    ).fetchone()
    current_chunker_version = stored_chunker_version(config, source, path.suffix)
    # Metadata alone is not an integrity boundary: editors and restore tools can
    # preserve both size and mtime while replacing content.  Hash every candidate
    # before accepting the incremental fast path so stale generations fail closed.
    digest_before = sha256_file(path)
    if (
        existing
        and existing[0] == stat_before.st_size
        and existing[1] == stat_before.st_mtime_ns
        and existing[2] == digest_before
        and existing[3] == PARSER_VERSION
        and existing[4] == current_chunker_version
        and existing[5] == "ok"
    ):
        if source.kind == "curated_memory" and not connection.execute(
            "SELECT 1 FROM memory_metadata WHERE document_id=? AND version=?", (document_id, METADATA_VERSION)
        ).fetchone():
            parsed = parse_memory(path, normalize_text(path.read_text(encoding="utf-8-sig")))
            stat_after = path.stat()
            if (stat_before.st_size, stat_before.st_mtime_ns) != (stat_after.st_size, stat_after.st_mtime_ns) or digest_before != sha256_file(path) or real_before != path.resolve(strict=True):
                raise RuntimeError("source changed during metadata extraction; retry on the next sync")
            with writer_transaction(connection):
                store_memory(connection, document_id, parsed)
        return "unchanged", 0

    extraction = extract_file(path, config)
    memory = None
    if source.kind == "curated_memory":
        memory = parse_memory(path, normalize_text(path.read_text(encoding="utf-8-sig")))
    stat_after = path.stat()
    digest_after = sha256_file(path)
    real_after = path.resolve(strict=True)
    if (
        stat_before.st_size != stat_after.st_size
        or stat_before.st_mtime_ns != stat_after.st_mtime_ns
        or digest_before != digest_after
        or real_before != real_after
    ):
        raise RuntimeError("source changed during extraction; retry on the next sync")

    relative = path.relative_to(root)
    generation = generation_for(digest_after, config, source, path.suffix)
    with writer_transaction(connection):
        if existing:
            delete_document(connection, document_id)
        connection.execute(
            """INSERT INTO documents(
                   id, collection_name, path, relative_path, real_path, title, extension,
                   size_bytes, mtime_ns, source_sha256, generation, extractor,
                   parser_version, chunker_version, status, error, indexed_at
               ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ok', ?, ?)""",
            (
                document_id,
                source.name,
                str(path),
                relative.as_posix(),
                str(real_after),
                extraction.title,
                path.suffix.casefold(),
                stat_after.st_size,
                stat_after.st_mtime_ns,
                digest_after,
                generation,
                extraction.extractor,
                PARSER_VERSION,
                current_chunker_version,
                extraction.warning,
                utc_now(),
            ),
        )
        if memory is not None:
            store_memory(connection, document_id, memory)
        for draft in extraction.chunks:
            chunk_id = "%s:%d" % (document_id, draft.ordinal)
            chunk_cursor = connection.execute(
                """INSERT INTO chunks(
                       id, document_id, ordinal, heading, locator, start_line,
                       end_line, page, text, token_estimate
                   ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    chunk_id,
                    document_id,
                    draft.ordinal,
                    draft.heading,
                    draft.locator,
                    draft.start_line,
                    draft.end_line,
                    draft.page,
                    draft.text,
                    draft.token_estimate,
                ),
            )
            storage_rowid = int(chunk_cursor.lastrowid)
            connection.execute(
                "INSERT INTO chunks_fts(rowid, chunk_id, title, heading, path, body) VALUES(?, ?, ?, ?, ?, ?)",
                (storage_rowid, chunk_id, extraction.title, draft.heading, relative.as_posix(), draft.text),
            )
            if trigram_available(connection):
                compact = "".join(normalize_text(draft.text).split())
                connection.execute(
                    "INSERT INTO chunks_trigram(rowid, chunk_id, title, heading, path, body, compact) VALUES(?, ?, ?, ?, ?, ?, ?)",
                    (
                        storage_rowid,
                        chunk_id,
                        extraction.title,
                        draft.heading,
                        relative.as_posix(),
                        draft.text,
                        compact,
                    ),
                )
    return "indexed", len(extraction.chunks)


def sync_source(
    config: AppConfig,
    source: SourceConfig,
    dry_run: bool = False,
    verbose: bool = False,
) -> SyncStats:
    stats = SyncStats(collection=source.name)
    if not source.enabled:
        stats.message = "disabled"
        return stats
    try:
        root = source.path.resolve(strict=True)
    except OSError as exc:
        stats.errors = 1
        stats.message = "root is unavailable: %s (%s)" % (source.path, exc)
        if not dry_run:
            _record_unavailable_source(config, source, stats.message)
        return stats

    if root.is_file():
        root = root.parent
    if dry_run:
        # A dry run is a filesystem scan only.  In particular, do not call
        # connect() or sync_lock(): both create persistent state, and
        # collection registration can mutate an existing manifest merely
        # because configuration changed.
        iterator = iter(iter_source_files(source))
        traversal_error: Optional[OSError] = None
        while True:
            try:
                path = next(iterator)
            except StopIteration:
                break
            except OSError as exc:
                traversal_error = exc
                stats.errors += 1
                break
            stats.scanned += 1
            try:
                path.resolve(strict=True)
            except OSError as exc:
                stats.skipped += 1
                if not _is_disappearance_error(exc):
                    traversal_error = exc
                    stats.errors += 1
                    break
        stats.message = (
            "dry-run traversal failed: %s" % traversal_error
            if traversal_error is not None
            else "dry-run scan only"
        )
        return stats
    connection = connect(config)
    try:
        _register_collection(connection, source)
        connection.commit()
        known: Set[str] = set()
        reconciliation_error: Optional[OSError] = None
        iterator = iter(iter_source_files(source))
        while True:
            try:
                path = next(iterator)
            except StopIteration:
                break
            except OSError as exc:
                reconciliation_error = exc
                stats.errors += 1
                if verbose:
                    print("traversal error %s: %s" % (source.path, exc), file=sys.stderr)
                break
            stats.scanned += 1
            try:
                real = path.resolve(strict=True)
            except OSError as exc:
                stats.skipped += 1
                if not _is_disappearance_error(exc):
                    reconciliation_error = exc
                    stats.errors += 1
                    if verbose:
                        print("inspection error %s: %s" % (path, exc), file=sys.stderr)
                    break
                continue
            document_id = stable_document_id(source.name, real)
            try:
                outcome, count = _index_one(connection, config, source, path, root)
                known.add(document_id)
                if outcome == "indexed":
                    stats.indexed += 1
                    stats.chunks += count
                else:
                    stats.unchanged += 1
                if verbose and outcome == "indexed":
                    print("indexed %s" % path, file=sys.stderr)
            except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as exc:
                stats.errors += 1
                if isinstance(exc, OSError) and not _is_disappearance_error(exc):
                    reconciliation_error = exc
                if verbose:
                    print("error %s: %s" % (path, exc), file=sys.stderr)
                try:
                    stat_result = path.stat()
                    digest = sha256_file(path)
                    relative = path.relative_to(root)
                    with writer_transaction(connection):
                        _record_error_document(connection, config, source, path, relative, stat_result, digest, str(exc))
                    known.add(stable_document_id(source.name, path.resolve(strict=True)))
                except OSError as record_exc:
                    if not _is_disappearance_error(record_exc):
                        reconciliation_error = record_exc

        if reconciliation_error is None:
            rows = connection.execute(
                "SELECT id FROM documents WHERE collection_name=?", (source.name,)
            ).fetchall()
            missing = [row[0] for row in rows if row[0] not in known]
            if missing:
                with writer_transaction(connection):
                    for document_id in missing:
                        delete_document(connection, document_id)
                stats.removed = len(missing)
        now = utc_now()
        sync_message = (
            "incomplete traversal: %s" % reconciliation_error
            if reconciliation_error is not None
            else "%d errors" % stats.errors
        )
        connection.execute(
            """UPDATE collections SET last_sync_at=?, last_sync_ok=?, last_sync_message=? WHERE name=?""",
            (now, 0 if stats.errors else 1, sync_message[:1000], source.name),
        )
        connection.commit()
        stats.message = (
            "incomplete traversal: %s" % reconciliation_error
            if reconciliation_error is not None
            else ("ok" if not stats.errors else "completed with errors")
        )
        return stats
    finally:
        connection.close()


def sync_all(
    config: AppConfig,
    collection: Optional[str] = None,
    dry_run: bool = False,
    verbose: bool = False,
) -> list[SyncStats]:
    selected = [source_by_name(config, collection)] if collection else list(config.sources)
    if dry_run:
        return [
            sync_source(config, source, dry_run=True, verbose=verbose)
            for source in selected
        ]
    results = []
    with sync_lock(config):
        connection = connect(config)
        try:
            for source in config.sources:
                _register_collection(connection, source)
            connection.commit()
        finally:
            connection.close()
        for source in selected:
            results.append(sync_source(config, source, dry_run=dry_run, verbose=verbose))
    return results


def _successful_sync_age_seconds(
    connection: sqlite3.Connection,
    collection: str,
) -> Optional[float]:
    row = connection.execute(
        "SELECT last_sync_at, last_sync_ok FROM collections WHERE name=?",
        (collection,),
    ).fetchone()
    if not row or row["last_sync_ok"] != 1 or not row["last_sync_at"]:
        return None
    try:
        synced_at = datetime.fromisoformat(str(row["last_sync_at"]).replace("Z", "+00:00"))
    except ValueError:
        return None
    if synced_at.tzinfo is None:
        synced_at = synced_at.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - synced_at.astimezone(timezone.utc)).total_seconds()
    return age if age >= 0 else None


def ensure_fresh(
    config: AppConfig,
    source: SourceConfig,
    max_age_seconds: float,
    verbose: bool = False,
) -> SyncStats:
    """Reconcile one source only when its last successful sync exceeds an SLA.

    The freshness check is deliberately repeated while holding ``sync_lock``.
    This makes concurrent agents coalesce: the first caller reconciles and the
    next caller observes the new timestamp instead of repeating the expensive
    source scan.
    """
    if not source.enabled:
        raise ValueError("Collection is disabled: %s" % source.name)
    if not 0 < max_age_seconds <= 365 * 24 * 3600:
        raise ValueError("max_age_seconds must be within (0, 31536000]")

    with sync_lock(config):
        if not source.path.exists():
            return sync_source(config, source, verbose=verbose)
        connection = connect(config)
        try:
            for configured_source in config.sources:
                _register_collection(connection, configured_source)
            connection.commit()
            age = _successful_sync_age_seconds(connection, source.name)
        finally:
            connection.close()
        if age is not None and age <= max_age_seconds:
            stats = SyncStats(collection=source.name)
            stats.message = "fresh (last successful sync %ds ago)" % int(age)
            return stats
        return sync_source(config, source, verbose=verbose)


def stats_to_dict(stats: SyncStats) -> Dict[str, object]:
    return asdict(stats)
