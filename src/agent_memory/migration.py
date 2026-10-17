"""Adopt byte-identical legacy projections; changed pipelines are rebuilt by sync."""
from __future__ import annotations

from contextlib import nullcontext
from dataclasses import replace
from os import stat_result
from pathlib import Path
from typing import Sequence

from .config import AppConfig, source_by_name
from .database import CHUNKER_VERSION, PARSER_VERSION, connect
from .indexer import _eligible_file, _excluded_relative, _is_within, sync_lock
from .memory_policy import METADATA_VERSION
from .projection import _chunk_policy, legacy_fingerprint, stored_chunker_version
from .util import sha256_file


def migrate_projections(config: AppConfig, pdfium: str, pypdf: str) -> dict:
    adopted = rebuild = rejected = 0
    with sync_lock(config):
        connection = connect(config)
        try:
            rows = connection.execute("SELECT d.*, col.kind AS old_kind FROM documents d JOIN collections col ON col.name=d.collection_name").fetchall()
            for row in rows:
                try:
                    source = source_by_name(config, row["collection_name"])
                except KeyError:
                    rejected += 1
                    continue
                extension = row["extension"]
                current = stored_chunker_version(config, source, extension)
                if row["chunker_version"] == current:
                    continue
                # Only line-v1 text projections can be byte-identical to legacy rows;
                # PDF and every other chunk policy (AST, fence-aware Markdown) are rebuilt by sync.
                if extension == ".pdf" or _chunk_policy(config, extension) != "line-v1":
                    rebuild += 1
                    continue
                legacy = "%s:%s" % (CHUNKER_VERSION, legacy_fingerprint(config, source, pdfium, pypdf))
                if not source.enabled or row["old_kind"] != source.kind or row["parser_version"] != PARSER_VERSION or row["chunker_version"] != legacy or row["status"] != "ok":
                    rejected += 1
                    continue
                try:
                    path = Path(row["path"])
                    root = source.path.resolve(strict=True)
                    root = root.parent if root.is_file() else root
                    real = path.resolve(strict=True)
                    before = path.stat()
                    if not _is_within(real, root) or str(real) != row["real_path"] or not _eligible_file(path, path.relative_to(root), source):
                        raise ValueError("source policy changed")
                    digest = sha256_file(path)
                    after = path.stat()
                    if (before.st_size, before.st_mtime_ns) != (row["size_bytes"], row["mtime_ns"]) or before != after or digest != row["source_sha256"] or real != path.resolve(strict=True):
                        raise ValueError("source changed")
                except (OSError, ValueError):
                    rejected += 1
                    continue
                # Keep generation and FTS rowids: evidence bytes and locators did not change.
                connection.execute("UPDATE documents SET chunker_version=? WHERE id=?", (current, row["id"]))
                adopted += 1
            connection.commit()
        finally:
            connection.close()
    return {"adopted": adopted, "needs_rebuild": rebuild, "rejected": rejected}


def _file_version(info: stat_result) -> tuple[int, int, int, int, int]:
    # Reads may update access time. Identity, content and metadata changes
    # still invalidate reuse, even when size and mtime were restored.
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def migrate_exclusions(
    config: AppConfig, collection: str, previous_globs: Sequence[str], *, dry_run: bool = False,
) -> dict:
    """Reuse identical projections only after adding exclusions, then require sync.

    Reconstruct the old fingerprint using only the supplied old exclusion list.
    Any other parser, model, chunk, root, or source-policy difference rejects reuse.
    This does not delete excluded rows, extract files, or certify freshness.
    """
    source = source_by_name(config, collection)
    if isinstance(previous_globs, str) or any(not isinstance(p, str) or not p for p in previous_globs):
        raise ValueError("previous exclusion globs must be a list of nonempty strings")
    if not source.enabled or not set(previous_globs) < set(source.exclude_globs):
        raise ValueError("exclusion migration requires an enabled collection and strictly added globs")
    previous = replace(source, exclude_globs=tuple(previous_globs))
    result = dict(reusable=0, adopted=0, already_current=0, excluded=0, needs_rebuild=0, dry_run=dry_run)
    with nullcontext() if dry_run else sync_lock(config):
        connection = connect(config, readonly=dry_run)
        try:
            connection.execute("BEGIN" if dry_run else "BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT d.*, col.kind old_kind, m.version metadata_version FROM documents d "
                "JOIN collections col ON col.name=d.collection_name "
                "LEFT JOIN memory_metadata m ON m.document_id=d.id WHERE d.collection_name=?",
                (collection,),
            ).fetchall()
            root = source.path.resolve(strict=True)
            root = root.parent if root.is_file() else root
            for row in rows:
                extension = row["extension"]
                current = stored_chunker_version(config, source, extension)
                try:
                    path = Path(row["path"])
                    relative = path.relative_to(root)
                    parents = list(relative.parents)[:-1]
                    if (not _eligible_file(path, relative, source)
                            or any(_excluded_relative(p, source) for p in parents)
                            or any(p.name.casefold() in source.exclude_dirs for p in parents)):
                        result["excluded"] += 1
                        continue
                    if row["chunker_version"] == current:
                        result["already_current"] += 1
                        continue
                    if (row["status"] != "ok" or row["old_kind"] != source.kind
                            or row["parser_version"] != PARSER_VERSION
                            or row["chunker_version"] != stored_chunker_version(config, previous, extension)
                            or (source.kind == "curated_memory" and row["metadata_version"] != METADATA_VERSION)):
                        raise ValueError("projection pipeline changed")
                    real = path.resolve(strict=True)
                    before = path.stat()
                    if not _is_within(real, root) or str(real) != row["real_path"]:
                        raise ValueError("source boundary changed")
                    digest = sha256_file(path)
                    after = path.stat()
                    if ((before.st_size, before.st_mtime_ns) != (row["size_bytes"], row["mtime_ns"])
                            or _file_version(before) != _file_version(after) or digest != row["source_sha256"]
                            or real != path.resolve(strict=True)):
                        raise ValueError("source changed")
                except (OSError, ValueError):
                    result["needs_rebuild"] += 1
                    continue
                result["reusable"] += 1
                if not dry_run:
                    # Same evidence and locators; retain generation, chunks and FTS rowids.
                    connection.execute("UPDATE documents SET chunker_version=? WHERE id=?", (current, row["id"]))
                    result["adopted"] += 1
            if not dry_run:
                connection.execute(
                    "UPDATE collections SET last_sync_ok=0, last_sync_message=? WHERE name=?",
                    ("Exclusion projections checked; run sync to reconcile excluded files", collection),
                )
                connection.commit()
            return result
        finally:
            connection.close()
