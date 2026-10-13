from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


DEFAULT_EXCLUDE_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".cache",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    "build",
    "dist",
    "coverage",
    "playwright-report",
    "test-results",
}

SENSITIVE_BASENAMES = {
    ".env",
    ".env.local",
    ".npmrc",
    ".pypirc",
    "auth.json",
    "credentials.json",
    "id_rsa",
    "id_ed25519",
    "secrets.json",
}

SENSITIVE_SUFFIXES = {".key", ".pem", ".p12", ".pfx", ".keystore"}


@dataclass(frozen=True)
class SourceConfig:
    name: str
    path: Path
    description: str
    kind: str = "knowledge"
    trust: str = "working"
    priority: float = 1.0
    enabled: bool = True
    include_extensions: frozenset[str] = field(default_factory=frozenset)
    exclude_dirs: frozenset[str] = field(default_factory=frozenset)
    exclude_globs: tuple[str, ...] = ()
    max_depth: Optional[int] = None
    include_hidden: bool = False


@dataclass(frozen=True)
class FeedbackConfig:
    auto_record_zero_results: bool = False
    max_records: int = 1000
    resolved_retention_days: int = 180


@dataclass(frozen=True)
class AppConfig:
    home: Path
    database: Path
    chunk_chars: int
    chunk_overlap_chars: int
    max_text_file_bytes: int
    max_archive_uncompressed_bytes: int
    max_pdf_file_bytes: int
    default_results: int
    default_snippet_chars: int
    default_context_tokens: int
    sync_max_age_hours: float
    secure_delete: bool
    feedback: FeedbackConfig
    sources: tuple[SourceConfig, ...]
    python_chunking: str = "line"


def default_home() -> Path:
    """Keep private runtime data outside source checkouts and site-packages."""
    configured = os.environ.get("AMEM_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".local" / "share" / "agent-memory"


def _absolute(home: Path, raw: str) -> Path:
    expanded = Path(raw).expanduser()
    if not expanded.is_absolute():
        expanded = home / expanded
    return expanded.resolve(strict=False)


def validate_database_path(home: Path, database: Path) -> Path:
    """Return the canonical database path after enforcing the storage boundary.

    The index is disposable state, so it must never be redirected into a
    canonical source tree.  Check the ``.state`` entry itself before resolving
    either path: resolving first would hide a symlink that escapes the memory
    home.
    """
    resolved_home = home.expanduser().resolve(strict=False)
    state = resolved_home / ".state"
    if state.is_symlink():
        raise ValueError("Memory state directory must not be a symlink: %s" % state)

    resolved_state = state.resolve(strict=False)
    expanded_database = database.expanduser()
    if not expanded_database.is_absolute():
        expanded_database = resolved_home / expanded_database
    resolved_database = expanded_database.resolve(strict=False)
    try:
        relative = resolved_database.relative_to(resolved_state)
    except ValueError as exc:
        raise ValueError("Database must be stored under %s: %s" % (state, resolved_database)) from exc
    if not relative.parts:
        raise ValueError("Database path must be a file below %s" % state)
    return resolved_database


def load_config(home: Optional[Path] = None) -> AppConfig:
    resolved_home = (home or default_home()).expanduser().resolve(strict=False)
    path = resolved_home / "config.json"
    if not path.is_file():
        raise FileNotFoundError(
            "Memory configuration not found at %s; run `amem --home %s init` first"
            % (path, resolved_home)
        )
    with path.open("r", encoding="utf-8") as handle:
        raw: Dict[str, Any] = json.load(handle)

    if raw.get("version") != 1:
        raise ValueError("Unsupported config version: %r" % raw.get("version"))

    retrieval = raw.get("retrieval", {})
    ingestion = raw.get("ingestion", {})
    operations = raw.get("operations", {})
    storage = raw.get("storage", {})
    feedback = raw.get("feedback", {})
    if not isinstance(feedback, dict):
        raise ValueError("feedback config must be an object")
    source_items: List[SourceConfig] = []
    names = set()
    for item in raw.get("sources", []):
        name = str(item["name"]).strip()
        if not name or name in names:
            raise ValueError("Source names must be non-empty and unique: %r" % name)
        names.add(name)
        extensions = frozenset(
            value.casefold() if str(value).startswith(".") else "." + str(value).casefold()
            for value in item.get("include_extensions", [])
        )
        source_items.append(
            SourceConfig(
                name=name,
                path=_absolute(resolved_home, str(item["path"])),
                description=str(item.get("description", "")).strip(),
                kind=str(item.get("kind", "knowledge")),
                trust=str(item.get("trust", "working")),
                priority=float(item.get("priority", 1.0)),
                enabled=bool(item.get("enabled", True)),
                include_extensions=extensions,
                exclude_dirs=frozenset(
                    value.casefold()
                    for value in (set(DEFAULT_EXCLUDE_DIRS) | set(item.get("exclude_dirs", [])))
                ),
                exclude_globs=tuple(str(value) for value in item.get("exclude_globs", [])),
                max_depth=(int(item["max_depth"]) if item.get("max_depth") is not None else None),
                include_hidden=bool(item.get("include_hidden", False)),
            )
        )

    database = validate_database_path(
        resolved_home,
        _absolute(resolved_home, str(raw.get("database", ".state/index.sqlite3"))),
    )

    sync_max_age_hours = float(operations.get("sync_max_age_hours", 8.0))
    if not 0 < sync_max_age_hours <= 24 * 365:
        raise ValueError("operations.sync_max_age_hours must be within (0, 8760]")

    raw_max_feedback_records = feedback.get("max_records", 1000)
    if isinstance(raw_max_feedback_records, bool) or not isinstance(raw_max_feedback_records, int):
        raise ValueError("feedback.max_records must be an integer")
    max_feedback_records = raw_max_feedback_records
    if not 1 <= max_feedback_records <= 100_000:
        raise ValueError("feedback.max_records must be within [1, 100000]")
    raw_resolved_retention_days = feedback.get("resolved_retention_days", 180)
    if isinstance(raw_resolved_retention_days, bool) or not isinstance(
        raw_resolved_retention_days, int
    ):
        raise ValueError("feedback.resolved_retention_days must be an integer")
    resolved_retention_days = raw_resolved_retention_days
    if not 1 <= resolved_retention_days <= 3650:
        raise ValueError("feedback.resolved_retention_days must be within [1, 3650]")
    auto_record_zero_results = feedback.get("auto_record_zero_results", False)
    if not isinstance(auto_record_zero_results, bool):
        raise ValueError("feedback.auto_record_zero_results must be a boolean")

    python_chunking = ingestion.get("python_chunking", "line")
    if python_chunking not in {"line", "ast"}:
        raise ValueError("ingestion.python_chunking must be line or ast")
    return AppConfig(
        home=resolved_home,
        database=database,
        chunk_chars=int(ingestion.get("chunk_chars", 2400)),
        chunk_overlap_chars=int(ingestion.get("chunk_overlap_chars", 240)),
        max_text_file_bytes=int(ingestion.get("max_text_file_bytes", 4_000_000)),
        max_archive_uncompressed_bytes=int(ingestion.get("max_archive_uncompressed_bytes", 80_000_000)),
        max_pdf_file_bytes=int(ingestion.get("max_pdf_file_bytes", 50_000_000)),
        default_results=int(retrieval.get("default_results", 5)),
        default_snippet_chars=int(retrieval.get("default_snippet_chars", 240)),
        default_context_tokens=int(retrieval.get("default_context_tokens", 1800)),
        sync_max_age_hours=sync_max_age_hours,
        secure_delete=bool(storage.get("secure_delete", True)),
        feedback=FeedbackConfig(
            auto_record_zero_results=auto_record_zero_results,
            max_records=max_feedback_records,
            resolved_retention_days=resolved_retention_days,
        ),
        sources=tuple(source_items),
        python_chunking=python_chunking,
    )


def source_by_name(config: AppConfig, name: str) -> SourceConfig:
    for source in config.sources:
        if source.name == name:
            return source
    raise KeyError("Unknown collection: %s" % name)
