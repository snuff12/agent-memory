"""Adopt byte-identical legacy projections; changed pipelines are rebuilt by sync."""
from __future__ import annotations

from pathlib import Path

from .config import AppConfig, source_by_name
from .database import CHUNKER_VERSION, PARSER_VERSION, connect
from .indexer import _eligible_file, _is_within, sync_lock
from .projection import legacy_fingerprint, stored_chunker_version
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
                if extension == ".pdf" or (extension == ".py" and config.python_chunking == "ast"):
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
