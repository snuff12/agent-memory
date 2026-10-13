from __future__ import annotations

import hashlib
import json
from importlib import metadata

from .config import AppConfig, SourceConfig
from .database import CHUNKER_VERSION, PARSER_VERSION


def _package_version(name: str) -> str:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return "missing"


def legacy_fingerprint(config: AppConfig, source: SourceConfig, pdfium: str, pypdf: str) -> str:
    """Identify every input that can change indexed bytes or source eligibility."""
    payload = {
        "parser_version": PARSER_VERSION,
        "chunker_version": CHUNKER_VERSION,
        "extractors": {
            "pypdfium2": pdfium,
            "pypdf": pypdf,
        },
        "ingestion": {
            "chunk_chars": config.chunk_chars,
            "chunk_overlap_chars": config.chunk_overlap_chars,
            "max_text_file_bytes": config.max_text_file_bytes,
            "max_archive_uncompressed_bytes": config.max_archive_uncompressed_bytes,
            "max_pdf_file_bytes": config.max_pdf_file_bytes,
        },
        "source_policy": {
            "name": source.name,
            "path": str(source.path),
            "enabled": source.enabled,
            "include_extensions": sorted(source.include_extensions),
            "exclude_dirs": sorted(source.exclude_dirs),
            "exclude_globs": list(source.exclude_globs),
            "max_depth": source.max_depth,
            "include_hidden": source.include_hidden,
        },
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]



def projection_fingerprint(config: AppConfig, source: SourceConfig, extension: str = ".md") -> str:
    extension = extension.casefold()
    limits = {"chunk_chars": config.chunk_chars, "chunk_overlap_chars": config.chunk_overlap_chars}
    if extension == ".pdf":
        limits["max_pdf_file_bytes"] = config.max_pdf_file_bytes
    elif extension in {".docx", ".pptx", ".xlsx", ".hwpx"}:
        limits["max_archive_uncompressed_bytes"] = config.max_archive_uncompressed_bytes
    else:
        limits["max_text_file_bytes"] = config.max_text_file_bytes
    payload = {
        "parser_version": PARSER_VERSION, "chunker_version": CHUNKER_VERSION,
        "extension": extension, "ingestion": limits,
        "chunk_policy": "python-ast-v1" if extension == ".py" and config.python_chunking == "ast" else "line-v1",
        "extractors": {"pypdfium2": _package_version("pypdfium2"), "pypdf": _package_version("pypdf")} if extension == ".pdf" else {},
        "source_policy": {
            "name": source.name, "path": str(source.path), "enabled": source.enabled, "kind": source.kind,
            "include_extensions": sorted(source.include_extensions), "exclude_dirs": sorted(source.exclude_dirs),
            "exclude_globs": list(source.exclude_globs), "max_depth": source.max_depth, "include_hidden": source.include_hidden,
        },
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def stored_chunker_version(config: AppConfig, source: SourceConfig, extension: str = ".md") -> str:
    return "%s:%s" % (CHUNKER_VERSION, projection_fingerprint(config, source, extension))


def generation_for(digest: str, config: AppConfig, source: SourceConfig, extension: str = ".md") -> str:
    return "%s.p%sc%sf%s" % (digest[:16], PARSER_VERSION, CHUNKER_VERSION, projection_fingerprint(config, source, extension))
