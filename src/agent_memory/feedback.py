from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import stat
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import AppConfig
from .util import atomic_create_text, utc_now

try:
    import fcntl
except ImportError:  # pragma: no cover - the installed integration targets macOS/POSIX
    fcntl = None  # type: ignore[assignment]


MAX_QUERY_INPUT_CHARS = 2_000
MAX_STORED_QUERY_CHARS = 500
MAX_COLLECTION_CHARS = 240
MAX_EXPECTED_PATH_CHARS = 1_000
MAX_NOTES_CHARS = 2_000
MAX_LIST_LIMIT = 100
MAX_EXPORT_LIMIT = 1_000
FAILURE_REASONS = ("zero_results", "relevance_miss")
FAILURE_STATUSES = ("open", "resolved", "ignored")
FAILURE_ORIGINS = ("cli_search", "mcp_search", "explicit")
EXPORT_SCHEMA = "agent-memory.feedback.v1"
FEEDBACK_SCHEMA_VERSION = "2"
READABLE_FEEDBACK_SCHEMA_VERSIONS = ("1", FEEDBACK_SCHEMA_VERSION)


_CREDENTIAL_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|auth(?:orization)?|"
    r"password|passwd|secret|client[_ -]?secret|private[_ -]?key|cookie|"
    r"session[_ -]?id)\b\s*(?:=|:|\bis\b|\s)\s*\S{4,}"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_PROVIDER_TOKEN_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[A-Z0-9]{16}|"
    r"xox[a-z]-[A-Za-z0-9-]{10,}|AIza[A-Za-z0-9_-]{20,}|glpat-[A-Za-z0-9_-]{12,}|"
    r"npm_[A-Za-z0-9]{20,}|[sr]k_(?:live|test)_[A-Za-z0-9]{16,})\b"
)
_PEM_RE = re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----")
_URI_CREDENTIAL_RE = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s/:]+:[^\s/@]+@", re.IGNORECASE)
_EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![\w.-])")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?82[- ]?)?0?1[016789](?:[- ]?\d){7,8}(?!\d)")
_KOREAN_ID_RE = re.compile(r"(?<!\d)\d{6}[- ]?[1-8]\d{6}(?!\d)")
_PAYMENT_CARD_CANDIDATE_RE = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
_PAYMENT_CARD_CONTEXT_RE = re.compile(
    r"(?i)(?:\b(?:credit|debit|payment)\s*card\b|\bcard\s*(?:number|no\.?|#)?\b|"
    r"\bpan\b|카드\s*(?:번호|넘버)?|결제\s*카드)"
)
_WHITESPACE_RE = re.compile(r"\s+")
_EXPORT_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}\.jsonl")


def _normalize(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("feedback text must be a string")
    normalized = unicodedata.normalize("NFC", value)
    normalized = "".join(" " if unicodedata.category(char) == "Cc" else char for char in normalized)
    return _WHITESPACE_RE.sub(" ", normalized).strip()


def _bounded(value: Optional[str], label: str, maximum: int) -> Optional[str]:
    if value is None:
        return None
    normalized = _normalize(value)
    if not normalized:
        return None
    if len(normalized) > maximum:
        raise ValueError("%s exceeds the %d-character limit" % (label, maximum))
    categories = _secret_categories(normalized)
    if categories:
        return "[REDACTED_SECRET_LIKE_VALUE categories=%s]" % ",".join(categories)
    return normalized


def _secret_categories(value: str) -> List[str]:
    value = unicodedata.normalize("NFKC", value)
    checks = (
        ("credential", _CREDENTIAL_ASSIGNMENT_RE),
        ("bearer", _BEARER_RE),
        ("jwt", _JWT_RE),
        ("provider_token", _PROVIDER_TOKEN_RE),
        ("private_key", _PEM_RE),
        ("uri_credential", _URI_CREDENTIAL_RE),
        ("email", _EMAIL_RE),
        ("phone", _PHONE_RE),
        ("korean_id", _KOREAN_ID_RE),
    )
    categories = [name for name, pattern in checks if pattern.search(value)]
    if _contains_payment_card(value):
        categories.append("payment_card")
    return categories


def _luhn_valid(digits: str) -> bool:
    if not 13 <= len(digits) <= 19 or not digits.isdigit():
        return False
    total = 0
    parity = len(digits) % 2
    for index, character in enumerate(digits):
        value = int(character)
        if index % 2 == parity:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _contains_payment_card(value: str) -> bool:
    for match in _PAYMENT_CARD_CANDIDATE_RE.finditer(value):
        candidate = match.group(0)
        digits = re.sub(r"[ -]", "", candidate)
        if not _luhn_valid(digits):
            continue
        window = value[max(0, match.start() - 32) : min(len(value), match.end() + 32)]
        # A card label is required for contiguous digits.  Grouped spaces or
        # hyphens are themselves a strong payment-number formatting signal.
        if " " in candidate or "-" in candidate or _PAYMENT_CARD_CONTEXT_RE.search(window):
            return True
    return False


def _query_storage(query: str, *, explicit: bool) -> Tuple[str, Optional[str], Optional[str], bool]:
    normalized = _normalize(query)
    if not normalized:
        raise ValueError("feedback query is empty")
    if len(normalized) > MAX_QUERY_INPUT_CHARS:
        raise ValueError("feedback query exceeds the %d-character limit" % MAX_QUERY_INPUT_CHARS)
    categories = _secret_categories(normalized)
    if categories:
        redacted = "[REDACTED_SECRET_LIKE_QUERY categories=%s length=%d]" % (
            ",".join(categories),
            len(normalized),
        )
        return normalized, None, redacted, True
    if len(normalized) > MAX_STORED_QUERY_CHARS:
        if explicit:
            raise ValueError(
                "explicit feedback query exceeds the %d-character storage limit"
                % MAX_STORED_QUERY_CHARS
            )
        return (
            normalized,
            None,
            "[REDACTED_OVERSIZED_QUERY length=%d]" % len(normalized),
            False,
        )
    return normalized, normalized, None, False


def _feedback_directory(config: AppConfig, *, create: bool) -> Path:
    directory = config.home / "feedback"
    if directory.is_symlink():
        raise ValueError("Feedback directory must not be a symlink")
    if create:
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            if not directory.is_dir():
                raise NotADirectoryError("Feedback path is not a directory: %s" % directory)
    elif not directory.is_dir():
        raise FileNotFoundError("Feedback queue does not exist")
    info = directory.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise PermissionError("Feedback directory must be owner-only: %s" % directory)
    return directory


def _create_private_file(path: Path) -> None:
    _validate_private_file(path, allow_missing=True)
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(str(path), flags, 0o600)
    except FileExistsError:
        _validate_private_file(path, allow_missing=False)
    else:
        os.close(descriptor)


def _validate_private_file(path: Path, *, allow_missing: bool) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        if allow_missing:
            return
        raise
    if path.is_symlink() or not stat.S_ISREG(info.st_mode):
        raise ValueError("Feedback storage must be a regular non-symlink file: %s" % path)
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise PermissionError("Feedback storage file must be owner-only: %s" % path)


def _chmod_feedback_files(database: Path) -> None:
    for path in (
        database,
        Path(str(database) + "-wal"),
        Path(str(database) + "-shm"),
        Path(str(database) + "-journal"),
    ):
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(str(path), flags)
        except FileNotFoundError:
            continue
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise PermissionError("Feedback SQLite file has an unsafe owner or type: %s" % path)
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)


def _validate_feedback_schema(
    connection: sqlite3.Connection,
    *,
    require_current: bool = False,
) -> str:
    has_metadata = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata'"
    ).fetchone()
    if not has_metadata:
        raise sqlite3.DatabaseError("Feedback schema metadata is missing")
    row = connection.execute(
        "SELECT value FROM metadata WHERE key='schema_version'"
    ).fetchone()
    if row is None:
        raise sqlite3.DatabaseError("Feedback schema_version metadata is missing")
    version = str(row[0])
    if version not in READABLE_FEEDBACK_SCHEMA_VERSIONS:
        raise sqlite3.DatabaseError(
            "Unsupported feedback schema version %s (readable: %s)"
            % (version, ", ".join(READABLE_FEEDBACK_SCHEMA_VERSIONS))
        )
    if require_current and version != FEEDBACK_SCHEMA_VERSION:
        raise sqlite3.DatabaseError(
            "Feedback schema version %s requires writable migration to %s"
            % (version, FEEDBACK_SCHEMA_VERSION)
        )
    if not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='retrieval_failures'"
    ).fetchone():
        raise sqlite3.DatabaseError("Feedback queue table is missing")
    secure = connection.execute(
        "SELECT value FROM metadata WHERE key='secure_delete'"
    ).fetchone()
    if not secure or str(secure[0]) != "1":
        raise sqlite3.DatabaseError("Feedback secure_delete metadata is missing")
    return version


def _create_feedback_queue_v2(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE retrieval_failures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fingerprint TEXT NOT NULL CHECK(length(fingerprint) = 64),
            query_text TEXT CHECK(query_text IS NULL OR length(query_text) <= 500),
            query_redacted TEXT CHECK(query_redacted IS NULL OR length(query_redacted) <= 500),
            secret_like INTEGER NOT NULL CHECK(secret_like IN (0, 1)),
            reason TEXT NOT NULL CHECK(reason IN ('zero_results', 'relevance_miss')),
            collection_name TEXT NOT NULL DEFAULT '' CHECK(length(collection_name) <= 240),
            expected_path TEXT CHECK(expected_path IS NULL OR length(expected_path) <= 1000),
            notes TEXT CHECK(notes IS NULL OR length(notes) <= 2000),
            origin TEXT NOT NULL CHECK(origin IN ('cli_search', 'mcp_search', 'explicit')),
            count INTEGER NOT NULL DEFAULT 1 CHECK(count >= 1),
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open', 'resolved', 'ignored')),
            resolved_at TEXT,
            resolution_notes TEXT CHECK(resolution_notes IS NULL OR length(resolution_notes) <= 2000),
            CHECK((query_text IS NULL) != (query_redacted IS NULL)),
            CHECK(
                (status='open' AND resolved_at IS NULL)
                OR (status IN ('resolved', 'ignored') AND resolved_at IS NOT NULL)
            ),
            UNIQUE(fingerprint, collection_name, reason)
        )"""
    )
    connection.execute(
        "CREATE INDEX retrieval_failures_review_idx "
        "ON retrieval_failures(status, last_seen DESC, id DESC)"
    )
    connection.execute(
        "CREATE INDEX retrieval_failures_retention_idx "
        "ON retrieval_failures(status, resolved_at, id)"
    )


def _migrate_feedback_v1_to_v2(connection: sqlite3.Connection) -> None:
    columns = (
        "id, fingerprint, query_text, query_redacted, secret_like, reason, "
        "collection_name, expected_path, notes, origin, count, first_seen, "
        "last_seen, status, resolved_at, resolution_notes"
    )
    connection.execute("BEGIN IMMEDIATE")
    try:
        original_count = int(
            connection.execute("SELECT COUNT(*) FROM retrieval_failures").fetchone()[0]
        )
        connection.execute("DROP INDEX IF EXISTS retrieval_failures_review_idx")
        connection.execute("DROP INDEX IF EXISTS retrieval_failures_retention_idx")
        connection.execute("ALTER TABLE retrieval_failures RENAME TO retrieval_failures_v1")
        _create_feedback_queue_v2(connection)
        connection.execute(
            """INSERT INTO retrieval_failures(%s)
               SELECT id, fingerprint, query_text, query_redacted, secret_like, reason,
                      collection_name, expected_path, notes, origin, count, first_seen,
                      last_seen, status,
                      CASE WHEN status='open' THEN NULL ELSE COALESCE(resolved_at, last_seen) END,
                      resolution_notes
               FROM retrieval_failures_v1""" % columns
        )
        migrated_count = int(
            connection.execute("SELECT COUNT(*) FROM retrieval_failures").fetchone()[0]
        )
        if migrated_count != original_count:
            raise sqlite3.DatabaseError(
                "Feedback migration row-count mismatch: %d != %d"
                % (migrated_count, original_count)
            )
        connection.execute("DROP TABLE retrieval_failures_v1")
        connection.execute(
            "UPDATE metadata SET value=? WHERE key='schema_version'",
            (FEEDBACK_SCHEMA_VERSION,),
        )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


@contextmanager
def _feedback_initialization_lock(directory: Path):  # type: ignore[no-untyped-def]
    if fcntl is None:
        raise RuntimeError("Feedback storage requires POSIX file locking")
    lock_path = directory / "init.lock"
    _create_private_file(lock_path)
    flags = os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(str(lock_path), flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise PermissionError("Feedback initialization lock must be an owner-only regular file")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _connect_feedback(config: AppConfig, *, readonly: bool = False) -> sqlite3.Connection:
    directory = _feedback_directory(config, create=not readonly)
    database = directory / "query_failures.sqlite3"
    if readonly:
        _validate_private_file(database, allow_missing=False)
        for sidecar in (
            Path(str(database) + "-wal"),
            Path(str(database) + "-shm"),
            Path(str(database) + "-journal"),
        ):
            _validate_private_file(sidecar, allow_missing=True)
        connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=8.0)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=8000")
            connection.execute("PRAGMA query_only=ON")
            _validate_feedback_schema(connection)
            return connection
        except BaseException:
            connection.close()
            raise

    with _feedback_initialization_lock(directory):
        return _connect_feedback_writable(database)


def _connect_feedback_writable(database: Path) -> sqlite3.Connection:
    _create_private_file(database)
    for sidecar in (
        Path(str(database) + "-wal"),
        Path(str(database) + "-shm"),
        Path(str(database) + "-journal"),
    ):
        _validate_private_file(sidecar, allow_missing=True)
    connection = sqlite3.connect(str(database), timeout=30.0)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=8000")
        connection.execute("PRAGMA secure_delete=ON")
        current_journal = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).casefold()
        if current_journal != "delete":
            selected = str(connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0]).casefold()
            if selected != "delete":
                raise sqlite3.DatabaseError("Feedback database could not enable DELETE journal mode")
        connection.execute("PRAGMA synchronous=FULL")
        has_tables = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' LIMIT 1"
        ).fetchone()
        if not has_tables:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                _create_feedback_queue_v2(connection)
                connection.execute(
                    "INSERT INTO metadata(key, value) VALUES('schema_version', ?)",
                    (FEEDBACK_SCHEMA_VERSION,),
                )
                connection.execute(
                    "INSERT INTO metadata(key, value) VALUES('secure_delete', '1')"
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        else:
            version = _validate_feedback_schema(connection)
            if version == "1":
                _migrate_feedback_v1_to_v2(connection)
            _validate_feedback_schema(connection, require_current=True)
        _chmod_feedback_files(database)
        return connection
    except BaseException:
        connection.close()
        raise


@contextmanager
def _writer_transaction(connection: sqlite3.Connection):  # type: ignore[no-untyped-def]
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def _fingerprint_key(config: AppConfig) -> bytes:
    directory = _feedback_directory(config, create=True)
    key_path = directory / "fingerprint.key"
    if key_path.is_symlink():
        raise ValueError("Feedback fingerprint key must not be a symlink")

    temporary = directory / (".fingerprint.%s.tmp" % uuid.uuid4().hex)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(str(temporary), flags, 0o600)
    try:
        material = secrets.token_bytes(32)
        os.write(descriptor, material)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        try:
            os.link(str(temporary), str(key_path))
        except FileExistsError:
            pass
    finally:
        temporary.unlink()

    info = key_path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise PermissionError("Feedback fingerprint key must be an owner-only regular file")
    read_flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        read_flags |= os.O_NOFOLLOW
    descriptor = os.open(str(key_path), read_flags)
    try:
        key = os.read(descriptor, 33)
    finally:
        os.close(descriptor)
    if len(key) != 32:
        raise ValueError("Feedback fingerprint key has an invalid length")
    return key


def _fingerprint(config: AppConfig, normalized_query: str) -> str:
    key = _fingerprint_key(config)
    material = normalized_query.casefold().encode("utf-8", "surrogatepass")
    return hmac.new(key, material, hashlib.sha256).hexdigest()


def _validate_enum(value: str, label: str, allowed: Tuple[str, ...]) -> str:
    if value not in allowed:
        raise ValueError("%s must be one of: %s" % (label, ", ".join(allowed)))
    return value


def _prune_candidate_ids(connection, config: AppConfig, now: datetime) -> List[int]:  # type: ignore[no-untyped-def]
    cutoff = (now - timedelta(days=config.feedback.resolved_retention_days)).replace(
        microsecond=0
    ).isoformat().replace("+00:00", "Z")
    expired = [
        int(row[0])
        for row in connection.execute(
            "SELECT id FROM retrieval_failures WHERE status != 'open' "
            "AND COALESCE(resolved_at, last_seen) < ?",
            (cutoff,),
        ).fetchall()
    ]
    expired_set = set(expired)
    remaining = connection.execute(
        "SELECT id, status FROM retrieval_failures "
        "ORDER BY CASE WHEN status='open' THEN 1 ELSE 0 END, last_seen, id"
    ).fetchall()
    remaining = [row for row in remaining if int(row["id"]) not in expired_set]
    excess = len(remaining) - config.feedback.max_records
    if excess > 0:
        expired.extend(int(row["id"]) for row in remaining[:excess])
    return expired


def _prune(connection, config: AppConfig, now: datetime) -> int:  # type: ignore[no-untyped-def]
    candidates = _prune_candidate_ids(connection, config, now)
    if candidates:
        placeholders = ",".join("?" for _ in candidates)
        connection.execute(
            "DELETE FROM retrieval_failures WHERE id IN (%s)" % placeholders,
            tuple(candidates),
        )
    return len(candidates)


def prune_failures(config: AppConfig, *, dry_run: bool = False) -> Dict[str, Any]:
    empty_report = {
        "dry_run": dry_run,
        "would_remove" if dry_run else "removed": 0,
        "remaining": 0,
        "resolved_retention_days": config.feedback.resolved_retention_days,
        "max_records": config.feedback.max_records,
    }
    try:
        if dry_run:
            connection = _connect_feedback(config, readonly=True)
        else:
            probe = _connect_feedback(config, readonly=True)
            probe.close()
            connection = _connect_feedback(config)
    except FileNotFoundError:
        return empty_report
    try:
        now = datetime.now(timezone.utc)
        if dry_run:
            candidates = _prune_candidate_ids(connection, config, now)
        else:
            with _writer_transaction(connection):
                candidates = _prune_candidate_ids(connection, config, now)
                if candidates:
                    placeholders = ",".join("?" for _ in candidates)
                    connection.execute(
                        "DELETE FROM retrieval_failures WHERE id IN (%s)" % placeholders,
                        tuple(candidates),
                    )
        remaining = int(
            connection.execute("SELECT COUNT(*) FROM retrieval_failures").fetchone()[0]
        )
        return {
            "dry_run": dry_run,
            "would_remove" if dry_run else "removed": len(candidates),
            "remaining": remaining if not dry_run else max(0, remaining - len(candidates)),
            "resolved_retention_days": config.feedback.resolved_retention_days,
            "max_records": config.feedback.max_records,
        }
    finally:
        connection.close()


def record_failure(
    config: AppConfig,
    query: str,
    *,
    reason: str,
    collection: Optional[str],
    origin: str,
    expected_path: Optional[str] = None,
    notes: Optional[str] = None,
) -> int:
    reason = _validate_enum(reason, "feedback reason", FAILURE_REASONS)
    origin = _validate_enum(origin, "feedback origin", FAILURE_ORIGINS)
    normalized, query_text, query_redacted, secret_like = _query_storage(
        query,
        explicit=origin == "explicit",
    )
    collection_name = _bounded(collection, "collection", MAX_COLLECTION_CHARS) or ""
    safe_expected_path = _bounded(expected_path, "expected path", MAX_EXPECTED_PATH_CHARS)
    safe_notes = _bounded(notes, "notes", MAX_NOTES_CHARS)

    connection = _connect_feedback(config)
    try:
        fingerprint = _fingerprint(config, normalized)
        now_dt = datetime.now(timezone.utc)
        now = now_dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        with _writer_transaction(connection):
            connection.execute(
                """INSERT INTO retrieval_failures(
                       fingerprint, query_text, query_redacted, secret_like, reason,
                       collection_name, expected_path, notes, origin, count,
                       first_seen, last_seen, status
                   ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, 'open')
                   ON CONFLICT(fingerprint, collection_name, reason) DO UPDATE SET
                       count=retrieval_failures.count + 1,
                       last_seen=excluded.last_seen,
                       origin=excluded.origin,
                       expected_path=COALESCE(excluded.expected_path, retrieval_failures.expected_path),
                       notes=COALESCE(excluded.notes, retrieval_failures.notes),
                       status='open', resolved_at=NULL, resolution_notes=NULL""",
                (
                    fingerprint,
                    query_text,
                    query_redacted,
                    int(secret_like),
                    reason,
                    collection_name,
                    safe_expected_path,
                    safe_notes,
                    origin,
                    now,
                    now,
                ),
            )
            _prune(connection, config, now_dt)
        row = connection.execute(
            "SELECT id FROM retrieval_failures WHERE fingerprint=? AND collection_name=? AND reason=?",
            (fingerprint, collection_name, reason),
        ).fetchone()
        if row is None:
            raise RuntimeError("feedback record was pruned before it could be reviewed")
        return int(row[0])
    finally:
        connection.close()


def record_zero_result(
    config: AppConfig,
    query: str,
    *,
    collection: Optional[str],
    origin: str,
) -> Optional[int]:
    feedback = getattr(config, "feedback", None)
    if feedback is None or not feedback.auto_record_zero_results:
        return None
    return record_failure(
        config,
        query,
        reason="zero_results",
        collection=collection,
        origin=origin,
    )


def _row_dict(row, *, summary: bool = False) -> Dict[str, Any]:  # type: ignore[no-untyped-def]
    display = row["query_text"] if row["query_text"] is not None else row["query_redacted"]
    result: Dict[str, Any] = {
        "id": int(row["id"]),
        "query": display,
        "secret_like": bool(row["secret_like"]),
        "reason": row["reason"],
        "collection": row["collection_name"] or None,
        "count": int(row["count"]),
        "first_seen": row["first_seen"],
        "last_seen": row["last_seen"],
        "status": row["status"],
        "origin": row["origin"],
    }
    if summary:
        result["fingerprint"] = str(row["fingerprint"])[:12]
    else:
        result.update(
            {
                "fingerprint": row["fingerprint"],
                "expected_path": row["expected_path"],
                "notes": row["notes"],
                "resolved_at": row["resolved_at"],
                "resolution_notes": row["resolution_notes"],
            }
        )
    return result


def list_failures(
    config: AppConfig,
    *,
    status: str = "open",
    limit: int = 20,
) -> List[Dict[str, Any]]:
    if status != "all":
        _validate_enum(status, "feedback status", FAILURE_STATUSES)
    if not 1 <= limit <= MAX_LIST_LIMIT:
        raise ValueError("feedback list limit must be within [1, %d]" % MAX_LIST_LIMIT)
    try:
        connection = _connect_feedback(config, readonly=True)
    except FileNotFoundError:
        return []
    try:
        if status == "all":
            rows = connection.execute(
                "SELECT * FROM retrieval_failures ORDER BY last_seen DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM retrieval_failures WHERE status=? "
                "ORDER BY last_seen DESC, id DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        return [_row_dict(row, summary=True) for row in rows]
    finally:
        connection.close()


def show_failure(config: AppConfig, failure_id: int) -> Dict[str, Any]:
    if failure_id < 1:
        raise ValueError("feedback id must be positive")
    try:
        connection = _connect_feedback(config, readonly=True)
    except FileNotFoundError as exc:
        raise KeyError("Unknown feedback id: %d" % failure_id) from exc
    try:
        row = connection.execute(
            "SELECT * FROM retrieval_failures WHERE id=?", (failure_id,)
        ).fetchone()
        if row is None:
            raise KeyError("Unknown feedback id: %d" % failure_id)
        return _row_dict(row)
    finally:
        connection.close()


def resolve_failure(
    config: AppConfig,
    failure_id: int,
    *,
    status: str,
    notes: Optional[str],
) -> Dict[str, Any]:
    if failure_id < 1:
        raise ValueError("feedback id must be positive")
    if status not in ("resolved", "ignored"):
        raise ValueError("resolution status must be resolved or ignored")
    safe_notes = _bounded(notes, "resolution notes", MAX_NOTES_CHARS)
    try:
        probe = _connect_feedback(config, readonly=True)
        probe.close()
    except FileNotFoundError as exc:
        raise KeyError("Unknown feedback id: %d" % failure_id) from exc
    connection = _connect_feedback(config)
    try:
        now = utc_now()
        with _writer_transaction(connection):
            cursor = connection.execute(
                "UPDATE retrieval_failures SET status=?, resolved_at=?, resolution_notes=? WHERE id=?",
                (status, now, safe_notes, failure_id),
            )
            if cursor.rowcount != 1:
                raise KeyError("Unknown feedback id: %d" % failure_id)
        row = connection.execute(
            "SELECT * FROM retrieval_failures WHERE id=?", (failure_id,)
        ).fetchone()
        return _row_dict(row)
    finally:
        connection.close()


def export_failures(
    config: AppConfig,
    *,
    export_format: str,
    status: str,
    limit: int,
) -> str:
    if export_format not in ("review", "gold"):
        raise ValueError("feedback export format must be review or gold")
    if status != "all":
        _validate_enum(status, "feedback status", FAILURE_STATUSES)
    if not 1 <= limit <= MAX_EXPORT_LIMIT:
        raise ValueError("feedback export limit must be within [1, %d]" % MAX_EXPORT_LIMIT)
    try:
        connection = _connect_feedback(config, readonly=True)
    except FileNotFoundError:
        return ""
    try:
        status_clause = "" if status == "all" else "WHERE status=?"
        params: List[Any] = [] if status == "all" else [status]
        if export_format == "gold":
            status_clause += (" AND " if status_clause else "WHERE ")
            status_clause += (
                "reason='relevance_miss' AND query_text IS NOT NULL "
                "AND expected_path IS NOT NULL"
            )
        params.append(limit)
        rows = connection.execute(
            "SELECT * FROM retrieval_failures %s ORDER BY last_seen DESC, id DESC LIMIT ?"
            % status_clause,
            tuple(params),
        ).fetchall()
    finally:
        connection.close()

    items: List[Dict[str, Any]] = []
    for row in rows:
        if export_format == "review":
            item = _row_dict(row)
            item["schema"] = EXPORT_SCHEMA
        else:
            item = {
                "id": "observed-failure-%d" % int(row["id"]),
                "category": "observed-relevance-miss",
                "query": row["query_text"],
                "k": 5,
                "relevant_paths": [row["expected_path"]],
                "enabled": False,
                "_candidate": {
                    "schema": EXPORT_SCHEMA,
                    "feedback_id": int(row["id"]),
                    "observed_count": int(row["count"]),
                    "last_seen": row["last_seen"],
                },
            }
            if row["collection_name"]:
                item["collection"] = row["collection_name"]
        items.append(item)
    return "".join(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n" for item in items)


def write_feedback_export(config: AppConfig, name: str, content: str) -> Path:
    if not _EXPORT_NAME_RE.fullmatch(name) or name in (".", ".."):
        raise ValueError("feedback export name must be a simple .jsonl filename")
    directory = _feedback_directory(config, create=True)
    destination = directory / name
    atomic_create_text(destination, content, mode=0o600)
    return destination
