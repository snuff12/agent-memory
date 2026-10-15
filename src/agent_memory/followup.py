"""Explicit, source-bound follow-up retrieval; no automatic query expansion."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from typing import List, Optional

from .config import AppConfig
from .database import connect
from .memory_policy import MemoryPolicy, eligible_documents
from .search import REF_RE, SearchDiagnostics, SearchHit, StaleReferenceError, _validate_document, format_search, search
from .util import json_dumps, normalize_text, truncate_to_token_budget


MAX_BRIDGE_CHARS = 120


def bridge_query(query: str) -> str:
    if not isinstance(query, str):
        raise ValueError("Follow-up query must be a literal name or phrase")
    query = normalize_text(query).strip()
    if (not 1 <= len(query) <= MAX_BRIDGE_CHARS or any(ord(char) < 32 or ord(char) == 127 for char in query)
            or not any(char.isalnum() for char in query)):
        raise ValueError("Follow-up query must be one literal name or phrase of 1-120 characters, without control characters")
    return query


@dataclass(frozen=True)
class FollowupAnchor:
    ref: str
    collection: str
    path: str
    locator: str
    start_line: Optional[int]
    end_line: Optional[int]
    page: Optional[int]
    span_start: int
    span_end: int
    matched_text: str
    memory_status: str = ""
    memory_scope: str = ""
    memory_sensitivity: str = ""
    memory_entry_type: str = ""


@dataclass(frozen=True)
class FollowupResult:
    query: str
    anchor: FollowupAnchor
    results: List[SearchHit]


def _anchor(config: AppConfig, query: str, ref: str, policy: Optional[MemoryPolicy]) -> FollowupAnchor:
    match = REF_RE.fullmatch(ref.strip())
    if not match:
        raise ValueError("Invalid document ref; use the opaque ref returned by search")
    connection = connect(config, readonly=True)
    try:
        eligible_documents(connection, policy)
        connection.execute("BEGIN")
        row = connection.execute("""SELECT d.*, c.text, c.locator, c.start_line, c.end_line, c.page
            FROM eligible_documents d JOIN chunks c ON c.document_id=d.id
            WHERE d.id=? AND d.generation=? AND c.ordinal=? AND d.status='ok'""",
            (match.group("doc"), match.group("generation"), int(match.group("ordinal"))),
        ).fetchone()
        if not row or not _validate_document(row, config, {}):
            raise StaleReferenceError("Follow-up source is unavailable or changed; search and read it again")
        # Match indexed source text only, never rendered titles, paths or tool
        # headers. ASCII bounds avoid mistaking AuroraArchive for Aurora, while
        # allowing Korean particles attached to Latin names (Aurora를).
        left = r"(?<![A-Za-z0-9_])" if re.match(r"[A-Za-z0-9_]", query) else ""
        right = r"(?![A-Za-z0-9_])" if re.search(r"[A-Za-z0-9_]$", query) else ""
        literal = re.search(left + re.escape(query) + right, row["text"], re.I)
        if literal is None:
            raise ValueError("Follow-up name or phrase is absent from the referenced source chunk; read the source and choose a literal bridge")
        return FollowupAnchor(ref.strip(), row["collection_name"], row["path"], row["locator"],
                              row["start_line"], row["end_line"], row["page"],
                              literal.start(), literal.end(), literal.group(),
                              memory_status=row["memory_status"], memory_scope=row["memory_scope"],
                              memory_sensitivity=row["memory_sensitivity"], memory_entry_type=row["memory_entry_type"])
    finally:
        connection.close()


def followup_search(
    config: AppConfig,
    query: str,
    ref: str,
    limit: Optional[int] = None,
    collection: Optional[str] = None,
    policy: Optional[MemoryPolicy] = None,
    diagnostics: Optional[SearchDiagnostics] = None,
) -> FollowupResult:
    """One bounded search using a literal bridge from a current eligible ref.

    This returns a separate result set. It never reranks the original query,
    recursively expands names, reads a new result, or saves derived queries.
    """
    query = bridge_query(query)
    anchor = _anchor(config, query, ref, policy)
    if collection is not None and collection != anchor.collection:
        raise ValueError("Follow-up collection must match the referenced source collection")
    wanted = max(1, min(int(limit or config.default_results), 20))
    hits = search(config, query, limit=min(20, wanted + 1), collection=anchor.collection,
                  policy=policy, diagnostics=diagnostics)
    # Reopen eligibility and revalidate the source after search: a concurrent
    # edit or policy/index change must not leave a stale bridge in the output.
    if _anchor(config, query, ref, policy) != anchor:
        raise StaleReferenceError("Follow-up source changed while searching; search and read it again")
    hits = [hit for hit in hits if hit.ref != anchor.ref][:wanted]
    if diagnostics is not None:
        diagnostics.returned = len(hits)
    return FollowupResult(query, anchor, hits)


def format_followup(result: FollowupResult, as_json: bool = False, max_units: int = 4000,
                    diagnostics: Optional[SearchDiagnostics] = None) -> str:
    if as_json:
        payload = json_dumps({"followup": dict(query=result.query, anchor=asdict(result.anchor)),
                              "results": [asdict(hit) for hit in result.results],
                              **({"explanation": asdict(diagnostics)} if diagnostics is not None else {})}, pretty=True)
        return payload
    def field(value: str, cap: int) -> str:
        # Escape controls from source/config metadata before rendering a line.
        return truncate_to_token_budget(json_dumps(value), cap, marker="…")[0]

    header = ("FOLLOW-UP SEARCH (navigation only; keep earlier evidence separately)\n"
              "from-ref: %s\ncollection: %s | via: %s\nsource chunk chars: %d:%d\n" % (
                  result.anchor.ref, field(result.anchor.collection, 120), field(result.query, 520),
                  result.anchor.span_start, result.anchor.span_end))
    if result.anchor.memory_status:
        header += "anchor-memory: %s | scope: %s | sensitivity: %s\n" % (
            field(result.anchor.memory_status, 120), field(result.anchor.memory_scope, 120), field(result.anchor.memory_sensitivity, 120))
    if result.anchor.memory_entry_type == "invalid":
        header += "MEMORY_METADATA_INVALID: review the anchor before using its claims.\n"
    budget = max(0, max_units)
    if len(header.encode("utf-8")) >= budget:
        return truncate_to_token_budget(header, budget, marker="")[0]
    body = format_search(result.results, max_units=budget - len(header.encode("utf-8")), diagnostics=diagnostics)
    return header + body
