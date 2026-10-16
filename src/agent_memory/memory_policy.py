"""Typed, fail-closed memory-card eligibility, shared by search and fetch."""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from importlib import metadata
from pathlib import Path
from typing import Optional

import yaml


METADATA_VERSION = "3:" + metadata.version("PyYAML")
MAX_FRONTMATTER_BYTES = 16_384
KINDS = {"fact", "preference", "episode", "procedure", "decision", "concept", "entity", "synthesis"}
NAVIGATION_NAMES = {"readme.md", "schema.md", "index.md", "log.md"}
SCOPE_RE = re.compile(r"^(?:global|(?:project|domain):[^\s:]{1,128})$")
# Misspelling one of these must not silently drop a lifecycle rule. supersedes
# is spelling-checked and linted by doctor; retrieval does not enforce it.
LIFECYCLE_KEYS = {"kind", "status", "scope", "sensitivity", "confidence", "source",
    "valid_from", "review_after", "supersedes"}
# Spellings are compared without case, spaces, dots, hyphens or underscores
# ("Review After", "reviewAfter", "review.after"); transposed letters are not caught.
_LIFECYCLE_COMPACT = {key.replace("_", ""): key for key in LIFECYCLE_KEYS}
_LIFECYCLE_COMPACT["supersede"] = "supersedes"


def _compact_key(key: str) -> str:
    return re.sub(r"[\s._-]+", "", key).casefold()


class _CardLoader(yaml.SafeLoader):
    def compose_node(self, parent, index):  # type: ignore[no-untyped-def]
        if self.check_event(yaml.AliasEvent):
            raise ValueError("YAML aliases are not allowed in memory metadata")
        return super().compose_node(parent, index)

    def construct_mapping(self, node, deep=False):  # type: ignore[no-untyped-def]
        seen = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in seen:
                raise ValueError("Memory metadata keys must be unique strings")
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def _instant(value: object, *, date_only: bool = False) -> Optional[str]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        if date_only:
            raise ValueError("review_after must be an ISO date")
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime.min.time(), timezone.utc)
    elif isinstance(value, str):
        if date_only and len(value) != 10:
            raise ValueError("review_after must be an ISO date")
        try:
            parsed = (datetime.combine(date.fromisoformat(value), datetime.min.time(), timezone.utc)
                if len(value) == 10 else datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            # fromisoformat echoes the rejected value; stored errors carry no card text.
            raise ValueError("Memory validity must be an ISO date or timestamp") from None
    else:
        raise ValueError("Memory validity must be an ISO date or timestamp")
    if parsed.tzinfo is None:
        raise ValueError("Memory timestamps must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_memory(path: Path, text: str) -> dict:
    result = {"entry_type": "invalid", "memory_kind": "", "memory_status": "", "scope": "", "sensitivity": "",
        "valid_from": None, "review_after": None, "supersedes": None, "error": "", "version": METADATA_VERSION}
    if not text.startswith("---\n"):
        if path.name.casefold() in NAVIGATION_NAMES:
            result["entry_type"] = "navigation"
        else:
            result["error"] = "Memory card has no frontmatter"
        return result
    end = text.find("\n---\n", 4)
    if end < 0 or len(text[:end].encode("utf-8")) > MAX_FRONTMATTER_BYTES:
        result["error"] = "Memory frontmatter is missing its end or exceeds the limit"
        return result
    try:
        raw = yaml.load(text[4:end], Loader=_CardLoader)
        if not isinstance(raw, dict):
            raise ValueError("Memory frontmatter must be a mapping")
        near_misses = [(key, _LIFECYCLE_COMPACT[_compact_key(key)]) for key in raw
                       if key not in LIFECYCLE_KEYS and _compact_key(key) in _LIFECYCLE_COMPACT]
        if near_misses:
            raise ValueError("Misspelled lifecycle key: " + ", ".join("%s (use %s)" % pair for pair in near_misses))
        # Retrieval cannot enforce an expiry, so a card that states one is not served as if it held.
        if any(_compact_key(key) == "validto" for key in raw):
            raise ValueError("valid_to is not supported; mark the card superseded or set review_after")
        for key, output in (("kind", "memory_kind"), ("status", "memory_status"), ("scope", "scope"), ("sensitivity", "sensitivity")):
            value = raw.get(key, "")
            if not isinstance(value, str) or len(value) > 240:
                raise ValueError("%s must be a bounded string" % key)
            result[output] = value
        if result["memory_kind"] not in KINDS:
            raise ValueError("Unknown memory kind")
        if result["memory_status"] not in {"inbox", "reviewed", "superseded"}:
            raise ValueError("Unknown memory status")
        if not SCOPE_RE.fullmatch(result["scope"]):
            raise ValueError("Memory scope must be global, project:<name>, or domain:<name>")
        if result["sensitivity"] not in {"private", "shareable"}:
            raise ValueError("Memory sensitivity must be private or shareable")
        if raw.get("confidence") not in {"high", "medium", "low"}:
            raise ValueError("Memory confidence must be high, medium, or low")
        source = raw.get("source")
        if not isinstance(source, str) or not source.strip():
            raise ValueError("Memory source/provenance is required")
        result["valid_from"] = _instant(raw.get("valid_from"))
        result["review_after"] = _instant(raw.get("review_after"), date_only=True)
        result["supersedes"] = raw.get("supersedes")
        result["entry_type"] = "card"
    except yaml.YAMLError as exc:
        # PyYAML messages quote the offending frontmatter lines; keep only the position.
        mark = getattr(exc, "problem_mark", None)
        result["error"] = "Memory frontmatter is not valid YAML" + (
            " (line %d, column %d)" % (mark.line + 1, mark.column + 1) if mark is not None else "")
    except (ValueError, TypeError, RecursionError) as exc:
        result["error"] = str(exc)[:400]
    return result


def store_memory(connection: sqlite3.Connection, document_id: str, parsed: dict) -> None:
    connection.execute(
        "INSERT OR REPLACE INTO memory_metadata(document_id,entry_type,memory_kind,memory_status,scope,sensitivity,valid_from,review_after,error,version) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (document_id, parsed["entry_type"], parsed["memory_kind"], parsed["memory_status"], parsed["scope"],
            parsed["sensitivity"], parsed["valid_from"], parsed["review_after"], parsed["error"], parsed["version"]),
    )


@dataclass(frozen=True)
class MemoryPolicy:
    status: str = "reviewed"
    project: Optional[str] = None
    domain: Optional[str] = None
    shareable_only: bool = False

    def __post_init__(self) -> None:
        if self.status not in {"reviewed", "inbox", "all"}:
            raise ValueError("Memory status must be reviewed, inbox, or all")
        for name in (self.project, self.domain):
            if name is not None and (not isinstance(name, str) or not re.fullmatch(r"[^\s:]{1,128}", name)):
                raise ValueError("Project/domain must be a non-empty name of at most 128 characters")
        if not isinstance(self.shareable_only, bool):
            raise ValueError("shareable_only must be a boolean")


def eligible_documents(connection: sqlite3.Connection, policy: Optional[MemoryPolicy] = None) -> None:
    """Make every retrieval channel filter the full match set before LIMIT.

    Policy values are parameter-bound in one connection-local row. The view is
    temporary and contains no source bodies or queries; the persisted DB stays read-only.
    """
    policy = policy or MemoryPolicy()
    connection.execute("CREATE TEMP TABLE memory_request(status TEXT,project TEXT,domain TEXT,shareable INTEGER,now TEXT,version TEXT)")
    connection.execute("INSERT INTO memory_request VALUES(?,?,?,?,?,?)", (
        policy.status, "project:" + policy.project if policy.project else None,
        "domain:" + policy.domain if policy.domain else None, int(policy.shareable_only),
        datetime.now(timezone.utc).isoformat(timespec="seconds"), METADATA_VERSION,
    ))
    connection.execute("""CREATE TEMP VIEW eligible_documents AS
        SELECT d.*, COALESCE(m.entry_type,'') AS memory_entry_type,
            COALESCE(m.memory_kind,'') AS memory_kind, COALESCE(m.memory_status,'') AS memory_status,
            COALESCE(m.scope,'') AS memory_scope, COALESCE(m.sensitivity,'') AS memory_sensitivity,
            COALESCE(m.error,'') AS memory_error,
            COALESCE(m.valid_from,'') AS memory_valid_from, COALESCE(m.review_after,'') AS memory_review_after
        FROM main.documents d JOIN main.collections col ON col.name=d.collection_name
        LEFT JOIN main.memory_metadata m ON m.document_id=d.id CROSS JOIN memory_request p
        WHERE col.kind!='curated_memory' OR (
            m.version=p.version AND (
                (m.entry_type='navigation' AND p.shareable=0) OR (
                    (p.status='all' OR m.memory_status=p.status)
                    AND (p.status!='reviewed' OR (m.entry_type='card' AND m.error=''))
                    AND (m.scope='global' OR m.scope=p.project OR m.scope=p.domain
                         OR (p.status!='reviewed' AND p.project IS NULL AND p.domain IS NULL))
                    AND (p.shareable=0 OR m.sensitivity='shareable')
                    AND (p.status!='reviewed' OR m.valid_from IS NULL OR m.valid_from<=p.now)
                    AND (p.status!='reviewed' OR m.review_after IS NULL OR m.review_after>p.now)
                )
            )
        )""")
    connection.commit()
