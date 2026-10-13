from __future__ import annotations

import argparse
import json
import os
import sqlite3
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

from . import __version__
from .config import load_config, source_by_name
from .database import MINIMUM_FTS_SECURE_DELETE_VERSION, connect, trigram_available
from .feedback import (
    MAX_EXPORT_LIMIT,
    MAX_LIST_LIMIT,
    export_failures,
    list_failures,
    prune_failures,
    record_failure,
    record_zero_result,
    resolve_failure,
    show_failure,
    write_feedback_export,
)
from .indexer import ensure_fresh, stats_to_dict, sync_all, sync_lock, sync_source
from .mcp_server import run_server
from .memory_policy import MemoryPolicy, parse_memory
from .migration import migrate_projections
from .search import (
    StaleReferenceError,
    catalog,
    context,
    fetch,
    format_search,
    format_search_with_visible_hits,
    search,
)
from .util import atomic_create_text, estimate_tokens, json_dumps, slugify, utc_now


CAPTURE_KINDS = ("decision", "fact", "preference", "episode", "procedure")
SEARCH_OUTPUT_MAX_UNITS = 4000


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="amem",
        description="Portable local memory: index canonical files, search briefly, fetch selectively.",
    )
    parser.add_argument("--home", type=Path, help="Private memory home (AMEM_HOME or ~/.local/share/agent-memory).")
    parser.add_argument("--version", action="version", version="%(prog)s " + __version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Create an empty private memory home; never overwrite existing data.")
    init_parser.add_argument("--demo", action="store_true", help="Include synthetic documents and a reproducible evaluation set.")

    sync_parser = subparsers.add_parser("sync", help="Incrementally reconcile configured source roots.")
    sync_parser.add_argument("--collection")
    sync_parser.add_argument("--dry-run", action="store_true")
    sync_parser.add_argument("--verbose", action="store_true")
    sync_parser.add_argument("--json", action="store_true")

    ensure_parser = subparsers.add_parser(
        "ensure-fresh",
        help="Reconcile one collection only when its last successful sync exceeds an SLA.",
    )
    ensure_parser.add_argument("--collection", required=True)
    ensure_parser.add_argument("--max-age-minutes", type=float)
    ensure_parser.add_argument("--verbose", action="store_true")
    ensure_parser.add_argument("--json", action="store_true")

    search_parser = subparsers.add_parser("search", help="Return small candidate snippets and opaque refs.")
    search_parser.add_argument("query")
    search_parser.add_argument("--collection")
    search_parser.add_argument("--limit", type=int)
    search_parser.add_argument("--json", action="store_true")

    read_parser = subparsers.add_parser("read", help="Fetch one searched section under a hard budget.")
    read_parser.add_argument("ref")
    read_parser.add_argument("--max-tokens", type=int, default=1200)
    read_parser.add_argument("--around", type=int, default=0)
    read_parser.add_argument("--part", type=int, default=1)

    context_parser = subparsers.add_parser("context", help="Assemble diverse excerpts under one hard budget.")
    context_parser.add_argument("query")
    context_parser.add_argument("--collection")
    context_parser.add_argument("--max-tokens", type=int)

    catalog_parser = subparsers.add_parser("catalog", help="List collections without reading content.")
    catalog_parser.add_argument("--json", action="store_true")

    status_parser = subparsers.add_parser("status", help="Show index and extraction health.")
    status_parser.add_argument("--json", action="store_true")
    status_parser.add_argument("--max-age-hours", type=float)
    status_parser.add_argument("--require-fresh", action="store_true")

    doctor_parser = subparsers.add_parser("doctor", help="Check local prerequisites and safety settings.")
    doctor_parser.add_argument("--json", action="store_true")

    capture_parser = subparsers.add_parser("capture", help="Explicitly add a candidate memory to the inbox.")
    capture_parser.add_argument("--kind", choices=CAPTURE_KINDS, required=True)
    capture_parser.add_argument("--title", required=True)
    capture_parser.add_argument("--body", help="Body text; stdin is used when omitted.")
    capture_parser.add_argument("--source", default="user")
    capture_parser.add_argument("--confidence", choices=("high", "medium", "low"), default="medium")
    capture_parser.add_argument("--no-sync", action="store_true")
    capture_parser.add_argument("--scope", default="global")
    capture_parser.add_argument("--sensitivity", choices=("private", "shareable"), default="private")
    capture_parser.add_argument("--valid-from")
    capture_parser.add_argument("--review-after")

    for scoped in (search_parser, read_parser, context_parser):
        scoped.add_argument("--project")
        scoped.add_argument("--domain")
        scoped.add_argument("--memory-status", choices=("reviewed", "inbox", "all"), default="reviewed")
        scoped.add_argument("--shareable-only", action="store_true")

    migration = subparsers.add_parser("migrate-projections", help="Adopt verified byte-identical legacy text projections before sync.")
    migration.add_argument("--previous-pdfium", required=True)
    migration.add_argument("--previous-pypdf", required=True)

    feedback_parser = subparsers.add_parser(
        "feedback",
        help="Review explicit relevance misses and opt-in zero-result observations.",
    )
    feedback_commands = feedback_parser.add_subparsers(dest="feedback_command", required=True)
    feedback_list = feedback_commands.add_parser("list", help="List bounded failure summaries.")
    feedback_list.add_argument(
        "--status", choices=("open", "resolved", "ignored", "all"), default="open"
    )
    feedback_list.add_argument("--limit", type=int, default=20)
    feedback_list.add_argument("--json", action="store_true")

    feedback_show = feedback_commands.add_parser("show", help="Show one failure record.")
    feedback_show.add_argument("id", type=int)
    feedback_show.add_argument("--json", action="store_true")

    feedback_report = feedback_commands.add_parser(
        "report", help="Explicitly report a result set that was not relevant."
    )
    feedback_report.add_argument("query")
    feedback_report.add_argument("--collection")
    feedback_report.add_argument("--expected-path")
    feedback_report.add_argument("--notes")
    feedback_report.add_argument("--json", action="store_true")

    feedback_resolve = feedback_commands.add_parser("resolve", help="Resolve or ignore one record.")
    feedback_resolve.add_argument("id", type=int)
    feedback_resolve.add_argument("--status", choices=("resolved", "ignored"), default="resolved")
    feedback_resolve.add_argument("--notes")
    feedback_resolve.add_argument("--json", action="store_true")

    feedback_export = feedback_commands.add_parser(
        "export", help="Explicitly export a durable review queue or disabled gold candidates."
    )
    feedback_export.add_argument("--format", choices=("review", "gold"), default="review")
    feedback_export.add_argument(
        "--status", choices=("open", "resolved", "ignored", "all"), default="open"
    )
    feedback_export.add_argument("--limit", type=int, default=100)
    feedback_export.add_argument(
        "--output", help="Simple .jsonl filename written owner-only below feedback/."
    )

    feedback_prune = feedback_commands.add_parser(
        "prune", help="Apply bounded-record and resolved-retention policy."
    )
    feedback_prune.add_argument("--dry-run", action="store_true")
    feedback_prune.add_argument("--json", action="store_true")

    eval_parser = subparsers.add_parser("eval", help="Run file-level retrieval gold queries.")
    eval_parser.add_argument("--file", type=Path)
    eval_parser.add_argument("--fail-under", type=float, default=0.8)
    eval_parser.add_argument("--fail-under-mrr", type=float, default=0.5)
    eval_parser.add_argument("--max-contamination-rate", type=float, default=0.0)
    eval_parser.add_argument("--max-average-units", type=float, default=3500.0)
    eval_parser.add_argument("--allow-budget-violations", type=int, default=0)
    eval_parser.add_argument("--json", action="store_true")

    compact_parser = subparsers.add_parser(
        "compact",
        help="Optimize, integrity-check, and scrub deleted content from the disposable index.",
    )
    compact_parser.add_argument("--json", action="store_true")

    semantic_parser = subparsers.add_parser("semantic", help="Explicit local research pilot; does not change default search.")
    semantic_commands = semantic_parser.add_subparsers(dest="semantic_command", required=True)
    semantic_build = semantic_commands.add_parser("build")
    semantic_build.add_argument("--collection", default="research")
    semantic_build.add_argument("--path-prefix", action="append", default=[], help="Select relative directories; repeat for multiple prefixes.")
    semantic_search = semantic_commands.add_parser("search")
    semantic_search.add_argument("query")
    semantic_search.add_argument("--limit", type=int, default=5)
    semantic_search.add_argument("--json", action="store_true")
    semantic_read = semantic_commands.add_parser("read")
    semantic_read.add_argument("ref")
    semantic_read.add_argument("--span-start", type=int, required=True)
    semantic_read.add_argument("--span-end", type=int, required=True)
    semantic_read.add_argument("--max-tokens", type=int, default=1200)
    semantic_read.add_argument("--part", type=int, default=1)
    for experimental in (semantic_build, semantic_search, semantic_read):
        experimental.add_argument("--experimental", action="store_true", required=True)

    subparsers.add_parser("mcp", help="Run the optional stdio MCP adapter.")
    return parser


def _parse_timestamp(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _status(config, as_json: bool, max_age_hours: Optional[float] = None) -> tuple[str, bool]:  # type: ignore[no-untyped-def]
    age_limit = float(max_age_hours if max_age_hours is not None else config.sync_max_age_hours)
    if not 0 < age_limit <= 24 * 365:
        raise ValueError("max age hours must be within (0, 8760]")
    enabled_names = [source.name for source in config.sources if source.enabled]
    missing_sources = [
        source.name for source in config.sources if source.enabled and not source.path.exists()
    ]
    if not config.database.exists():
        payload = {
            "ready": False,
            "fresh": False,
            "database": str(config.database),
            "sync_max_age_hours": age_limit,
            "sync_overdue": enabled_names,
            "sync_failed": [],
            "source_missing": missing_sources,
            "freshness_basis": "last_successful_sync_age_only",
            "message": "run amem sync",
        }
    else:
        connection = connect(config, readonly=True)
        try:
            rows = connection.execute(
                "SELECT name, last_sync_at, last_sync_ok FROM collections WHERE enabled=1"
            ).fetchall()
            states = {str(row["name"]): row for row in rows}
            now = datetime.now(timezone.utc)
            overdue = []
            failed = []
            timestamps = []
            for name in enabled_names:
                row = states.get(name)
                synced_at = _parse_timestamp(str(row["last_sync_at"])) if row and row["last_sync_at"] else None
                age_seconds = (
                    (now - synced_at.astimezone(timezone.utc)).total_seconds()
                    if synced_at is not None
                    else None
                )
                if age_seconds is None or not 0 <= age_seconds <= age_limit * 3600:
                    overdue.append(name)
                else:
                    timestamps.append(synced_at)
                if row and row["last_sync_ok"] != 1:
                    failed.append(name)
            payload = {
                "ready": True,
                "fresh": not overdue and not failed and not missing_sources,
                "database": str(config.database),
                "database_bytes": config.database.stat().st_size,
                "documents": connection.execute("SELECT COUNT(*) FROM documents WHERE status='ok'").fetchone()[0],
                "chunks": connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0],
                "errors": connection.execute("SELECT COUNT(*) FROM documents WHERE status='error'").fetchone()[0],
                "trigram": trigram_available(connection),
                "collections": connection.execute("SELECT COUNT(*) FROM collections WHERE enabled=1").fetchone()[0],
                "sync_max_age_hours": age_limit,
                "oldest_current_sync_at": min(timestamps).isoformat().replace("+00:00", "Z") if timestamps else None,
                "sync_overdue": overdue,
                "sync_failed": failed,
                "source_missing": missing_sources,
                "freshness_basis": "last_successful_sync_age_only",
            }
        finally:
            connection.close()
    if as_json:
        return json_dumps(payload, pretty=True), bool(payload["fresh"])
    return "\n".join("%s: %s" % item for item in payload.items()), bool(payload["fresh"])


def _permission_check(name: str, path: Path, kind: str, allow_missing: bool = False) -> dict:
    if path.is_symlink():
        return {"name": name, "ok": False, "detail": "%s is a symlink" % path}
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return {
            "name": name,
            "ok": allow_missing,
            "detail": "%s does not exist%s" % (path, " (will be created privately)" if allow_missing else ""),
        }

    permissions = stat.S_IMODE(mode)
    if kind == "directory":
        right_kind = stat.S_ISDIR(mode)
        owner_access = permissions & 0o700 == 0o700
        expectation = "private owner-accessible directory"
    else:
        right_kind = stat.S_ISREG(mode)
        owner_access = permissions & 0o400 == 0o400
        expectation = "private owner-readable file"
    private = permissions & 0o077 == 0
    return {
        "name": name,
        "ok": right_kind and owner_access and private,
        "detail": "%s mode=%04o (%s)" % (path, permissions, expectation),
    }


def _doctor(config, as_json: bool) -> tuple[str, bool]:  # type: ignore[no-untyped-def]
    checks = []
    config_path = config.home / "config.json"
    checks.append({"name": "config", "ok": config_path.is_file() and not config_path.is_symlink(), "detail": str(config_path)})
    checks.append(_permission_check("home_permissions", config.home, "directory"))
    checks.append(_permission_check("vault_permissions", config.home / "vault", "directory"))
    checks.append(_permission_check("config_permissions", config_path, "file"))
    checks.append(_permission_check("state_permissions", config.home / ".state", "directory", allow_missing=True))
    checks.append(
        _permission_check(
            "feedback_permissions", config.home / "feedback", "directory", allow_missing=True
        )
    )
    state_root = config.home / ".state"
    orphan_indexes = []
    insecure_state_files = []
    if state_root.is_dir() and not state_root.is_symlink():
        for state_path in state_root.rglob("*"):
            if state_path.is_symlink():
                insecure_state_files.append("%s (symlink)" % state_path)
                continue
            if not state_path.is_file():
                continue
            if state_path.name.endswith(".sqlite3") and state_path.resolve() != config.database:
                orphan_indexes.append(str(state_path))
            if stat.S_IMODE(state_path.stat().st_mode) & 0o077:
                insecure_state_files.append(
                    "%s (mode=%04o)" % (state_path, stat.S_IMODE(state_path.stat().st_mode))
                )
    checks.append(
        {
            "name": "orphan_indexes",
            "ok": not orphan_indexes,
            "detail": "none" if not orphan_indexes else ", ".join(orphan_indexes),
        }
    )
    feedback_root = config.home / "feedback"
    insecure_feedback_files = []
    if feedback_root.is_dir() and not feedback_root.is_symlink():
        for feedback_path in feedback_root.rglob("*"):
            if feedback_path.is_symlink():
                insecure_feedback_files.append("%s (symlink)" % feedback_path)
                continue
            if feedback_path.is_file() and stat.S_IMODE(feedback_path.stat().st_mode) & 0o077:
                insecure_feedback_files.append(
                    "%s (mode=%04o)"
                    % (feedback_path, stat.S_IMODE(feedback_path.stat().st_mode))
                )
    checks.append(
        {
            "name": "feedback_file_permissions",
            "ok": not insecure_feedback_files,
            "detail": "owner-only" if not insecure_feedback_files else ", ".join(insecure_feedback_files),
        }
    )
    checks.append(
        {
            "name": "state_file_permissions",
            "ok": not insecure_state_files,
            "detail": "owner-only" if not insecure_state_files else ", ".join(insecure_state_files),
        }
    )
    secure_delete_supported = sqlite3.sqlite_version_info >= MINIMUM_FTS_SECURE_DELETE_VERSION
    active_secure_delete: Optional[bool] = None
    if config.database.exists():
        try:
            secure_connection = connect(config, readonly=True)
            try:
                secure_row = secure_connection.execute(
                    "SELECT value FROM metadata WHERE key='secure_delete'"
                ).fetchone()
                active_secure_delete = bool(secure_row and secure_row[0] == "1")
            finally:
                secure_connection.close()
        except (sqlite3.Error, OSError, ValueError):
            active_secure_delete = False
    checks.append(
        {
            "name": "secure_delete",
            "ok": config.secure_delete
            and secure_delete_supported
            and active_secure_delete is not False,
            "detail": "configured=%s active_index=%s sqlite=%s (FTS requires >=%s)"
            % (
                config.secure_delete,
                "pending-first-sync" if active_secure_delete is None else active_secure_delete,
                sqlite3.sqlite_version,
                ".".join(str(value) for value in MINIMUM_FTS_SECURE_DELETE_VERSION),
            ),
        }
    )
    try:
        import pypdfium2  # type: ignore[import-not-found]  # noqa: F401
        import pypdf  # type: ignore[import-not-found]  # noqa: F401

        pdf_ok = True
    except ImportError:
        pdf_ok = False
    checks.append({"name": "pdf_parser", "ok": pdf_ok, "detail": "PDFium primary + pypdf fallback"})
    for source in config.sources:
        checks.append({"name": "source:%s" % source.name, "ok": (not source.enabled or source.path.exists()), "detail": str(source.path)})
    ok = all(check["ok"] for check in checks)
    if as_json:
        return json_dumps({"ok": ok, "checks": checks}, pretty=True), ok
    return "\n".join("[%s] %s — %s" % ("ok" if item["ok"] else "fail", item["name"], item["detail"]) for item in checks), ok


def _capture(config, args: argparse.Namespace) -> Path:  # type: ignore[no-untyped-def]
    body = args.body if args.body is not None else sys.stdin.read()
    if not body.strip():
        raise ValueError("capture body is empty")
    created = utc_now()
    date = created[:10]
    slug = slugify(args.title)
    safe_title = args.title.replace("\r", " ").replace("\n", " ").strip()
    safe_source = args.source.replace("\r", " ").replace("\n", " ").strip()
    content = (
        "---\n"
        "title: %s\nkind: %s\nstatus: inbox\nconfidence: %s\nsource: %s\ncreated: %s\n"
        "scope: %s\nsensitivity: %s\nvalid_from: %s\nreview_after: %s\n---\n\n%s\n" % (
            json.dumps(safe_title, ensure_ascii=False),
            args.kind,
            args.confidence,
            json.dumps(safe_source, ensure_ascii=False),
            created,
            json.dumps(args.scope, ensure_ascii=False), args.sensitivity,
            json.dumps(args.valid_from) if args.valid_from else "null",
            json.dumps(args.review_after) if args.review_after else "null",
            body.strip(),
        )
    )
    parsed = parse_memory(Path("candidate.md"), content)
    if parsed["error"]:
        raise ValueError(parsed["error"])
    with sync_lock(config):
        inbox = config.home / "vault" / "inbox"
        if (config.home / "vault").is_symlink() or inbox.is_symlink():
            raise ValueError("Memory vault and inbox must not be symlinks")
        candidate = inbox / (date + "-" + slug + ".md")
        counter = 2
        while True:
            try:
                atomic_create_text(candidate, content, mode=0o600)
                break
            except FileExistsError:
                candidate = inbox / (date + "-" + slug + "-%d.md" % counter)
                counter += 1
        if not args.no_sync:
            vault_source = source_by_name(config, "vault")
            sync_source(config, vault_source)
    return candidate


def _try_record_zero_result(config, query: str, collection: Optional[str], origin: str) -> None:  # type: ignore[no-untyped-def]
    """Best-effort telemetry: retrieval must not fail because feedback storage did."""
    try:
        record_zero_result(config, query, collection=collection, origin=origin)
    except (ValueError, OSError, sqlite3.Error, RuntimeError):
        print(
            "amem: warning: zero-result feedback could not be recorded",
            file=sys.stderr,
        )


def _render_feedback_summary(item: dict) -> str:
    query = str(item.get("query") or "")
    if len(query) > 160:
        query = query[:157] + "..."
    return "#%d %s %s count=%d collection=%s query=%s" % (
        item["id"],
        item["status"],
        item["reason"],
        item["count"],
        item.get("collection") or "all",
        query,
    )


def _compact(config) -> dict:  # type: ignore[no-untyped-def]
    if not config.secure_delete:
        raise ValueError("compact requires storage.secure_delete=true")
    with sync_lock(config):
        before_bytes = config.database.stat().st_size
        connection = connect(config)
        try:
            checkpoint_before = list(connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone())
            if checkpoint_before[0] != 0 or checkpoint_before[1] != 0:
                raise sqlite3.OperationalError(
                    "cannot scrub index while a reader holds the WAL; close active memory readers and retry"
                )
            integrity_before = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity_before != "ok":
                raise sqlite3.DatabaseError("integrity_check failed before compact: %s" % integrity_before)
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise sqlite3.IntegrityError("foreign_key_check failed before compact")
            connection.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('integrity-check')")
            connection.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('optimize')")
            if trigram_available(connection):
                connection.execute("INSERT INTO chunks_trigram(chunks_trigram) VALUES('integrity-check')")
                connection.execute("INSERT INTO chunks_trigram(chunks_trigram) VALUES('optimize')")
            connection.commit()
            connection.execute("VACUUM")
            checkpoint_after = list(connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone())
            if checkpoint_after[0] != 0 or checkpoint_after[1] != 0:
                raise sqlite3.OperationalError(
                    "index rewrite completed but WAL scrub is blocked by an active reader; close readers and retry"
                )
            integrity_after = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity_after != "ok":
                raise sqlite3.DatabaseError("integrity_check failed after compact: %s" % integrity_after)
            chunk_count = connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            word_count = connection.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0]
            trigram_count = (
                connection.execute("SELECT COUNT(*) FROM chunks_trigram").fetchone()[0]
                if trigram_available(connection)
                else None
            )
            if word_count != chunk_count or (trigram_count is not None and trigram_count != chunk_count):
                raise sqlite3.DatabaseError("retrieval projection row-count mismatch after compact")
        finally:
            connection.close()
        after_bytes = config.database.stat().st_size
        return {
            "database": str(config.database),
            "before_bytes": before_bytes,
            "after_bytes": after_bytes,
            "reclaimed_bytes": max(0, before_bytes - after_bytes),
            "chunks": chunk_count,
            "word_projection_rows": word_count,
            "trigram_projection_rows": trigram_count,
            "wal_checkpoint_before": checkpoint_before,
            "wal_checkpoint_after": checkpoint_after,
            "integrity": integrity_after,
            "secure_delete": True,
        }


def _evaluate(config, path: Path) -> dict:  # type: ignore[no-untyped-def]
    total = positive_cases = negative_cases = hits_count = negative_passes = 0
    passed_cases = contamination_cases = contaminated_cases = budget_violations = 0
    reciprocal_sum = 0.0
    total_search_units = 0
    max_search_units = 0
    details = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            case = json.loads(stripped)
            if case.get("enabled", True) is False:
                continue
            total += 1
            query = str(case["query"])
            k = int(case.get("k", 5))
            if not 1 <= k <= 20:
                raise ValueError("eval line %d: k must be within [1, 20]" % line_number)
            relevant = [str(value) for value in case.get("relevant_paths", [])]
            expect_no_results = bool(case.get("expect_no_results", False))
            if expect_no_results and relevant:
                raise ValueError("eval line %d: expect_no_results cannot have relevant_paths" % line_number)
            if not expect_no_results and not relevant:
                raise ValueError("eval line %d: relevant_paths is required" % line_number)
            forbidden_paths = [str(value) for value in case.get("forbidden_paths", [])]
            forbidden_collections = {str(value) for value in case.get("forbidden_collections", [])}
            results = search(config, query, limit=k, collection=case.get("collection"))
            rendered, visible_results = format_search_with_visible_hits(
                results,
                max_units=SEARCH_OUTPUT_MAX_UNITS,
            )
            search_units = estimate_tokens(rendered)
            total_search_units += search_units
            max_search_units = max(max_search_units, search_units)
            case_budget = case.get("max_search_units")
            budget_violation = case_budget is not None and search_units > int(case_budget)
            if budget_violation:
                budget_violations += 1
            matched_rank: Optional[int] = None
            matched_path: Optional[str] = None
            for result in visible_results:
                if any(value in result.path or value in result.relative_path for value in relevant):
                    matched_rank = result.rank
                    matched_path = result.path
                    break
            contamination = [
                result.path
                for result in visible_results
                if result.collection in forbidden_collections
                or any(value in result.path or value in result.relative_path for value in forbidden_paths)
            ]
            if forbidden_paths or forbidden_collections:
                contamination_cases += 1
                if contamination:
                    contaminated_cases += 1
            if expect_no_results:
                negative_cases += 1
                success = not visible_results
                if success:
                    negative_passes += 1
            else:
                positive_cases += 1
                success = matched_rank is not None
                if success:
                    hits_count += 1
                    reciprocal_sum += 1.0 / int(matched_rank)
            if success and not contamination and not budget_violation:
                passed_cases += 1
            details.append(
                {
                    "line": line_number,
                    "id": case.get("id"),
                    "category": case.get("category"),
                    "query": query,
                    "success": success,
                    "hit": matched_rank is not None,
                    "rank": matched_rank,
                    "matched_path": matched_path,
                    "result_count": len(visible_results),
                    "search_units": search_units,
                    "budget_violation": budget_violation,
                    "contamination": contamination,
                }
            )
    hit_rate = hits_count / positive_cases if positive_cases else 0.0
    return {
        "cases": total,
        "positive_cases": positive_cases,
        "negative_cases": negative_cases,
        "passed_cases": passed_cases,
        "hits": hits_count,
        "hit_rate_at_k": round(hit_rate, 4),
        "mrr": round(reciprocal_sum / positive_cases if positive_cases else 0.0, 4),
        "negative_pass_rate": round(negative_passes / negative_cases if negative_cases else 1.0, 4),
        "contamination_cases": contamination_cases,
        "contaminated_cases": contaminated_cases,
        "contamination_rate": round(
            contaminated_cases / contamination_cases if contamination_cases else 0.0,
            4,
        ),
        "average_search_units": round(total_search_units / total if total else 0.0, 1),
        "max_search_units": max_search_units,
        "budget_violations": budget_violations,
        "details": details,
    }


def _main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "init":
            from .bootstrap import initialize
            print(initialize(args.home, demo=args.demo))
            return 0
        config = load_config(args.home)
        if args.command == "sync":
            results = sync_all(config, args.collection, dry_run=args.dry_run, verbose=args.verbose)
            payload = [stats_to_dict(item) for item in results]
            print(json_dumps(payload, pretty=True) if args.json else "\n".join(
                "%s: scanned=%d indexed=%d unchanged=%d removed=%d errors=%d chunks=%d (%s)"
                % (item.collection, item.scanned, item.indexed, item.unchanged, item.removed, item.errors, item.chunks, item.message)
                for item in results
            ))
            return 1 if any(item.errors for item in results) else 0
        if args.command == "ensure-fresh":
            max_age_minutes = (
                args.max_age_minutes
                if args.max_age_minutes is not None
                else config.sync_max_age_hours * 60
            )
            if not 0 < max_age_minutes <= 365 * 24 * 60:
                raise ValueError("max age minutes must be within (0, 525600]")
            result = ensure_fresh(
                config,
                source_by_name(config, args.collection),
                max_age_minutes * 60,
                verbose=args.verbose,
            )
            print(
                json_dumps(stats_to_dict(result), pretty=True)
                if args.json
                else "%s: scanned=%d indexed=%d unchanged=%d removed=%d errors=%d chunks=%d (%s)"
                % (
                    result.collection,
                    result.scanned,
                    result.indexed,
                    result.unchanged,
                    result.removed,
                    result.errors,
                    result.chunks,
                    result.message,
                )
            )
            return 1 if result.errors else 0
        if args.command == "semantic":
            from .semantic import WARNING, build, search_semantic
            from .search import SearchHit
            from dataclasses import replace
            if args.semantic_command == "build":
                print(json_dumps(build(config, args.collection, args.path_prefix), pretty=True))
            elif args.semantic_command == "read":
                print(fetch(config, args.ref, max_tokens=args.max_tokens, part=args.part, span=(args.span_start, args.span_end)))
            else:
                results = search_semantic(config, args.query, args.limit)
                if args.json:
                    print(json_dumps({"warning": WARNING, "results": results}, pretty=True))
                else:
                    hits = [replace(SearchHit(**result["hit"]), locator=result["hit"]["locator"] + " · chunk chars %d:%d" % (result["span_start"], result["span_end"])) for result in results]
                    print(WARNING + "\n" + format_search(hits, max_units=SEARCH_OUTPUT_MAX_UNITS - estimate_tokens(WARNING + "\n")))
            return 0
        if args.command == "migrate-projections":
            print(json_dumps(migrate_projections(config, args.previous_pdfium, args.previous_pypdf), pretty=True))
            return 0
        if args.command in {"search", "read", "context"}:
            policy = MemoryPolicy(args.memory_status, args.project, args.domain, args.shareable_only)
        if args.command == "search":
            hits = search(config, args.query, args.limit, args.collection, policy=policy)
            if not hits and policy == MemoryPolicy():
                _try_record_zero_result(config, args.query, args.collection, "cli_search")
            print(format_search(
                hits,
                args.json,
                max_units=None if args.json else SEARCH_OUTPUT_MAX_UNITS,
            ))
            return 0
        if args.command == "read":
            print(fetch(config, args.ref, args.max_tokens, args.around, args.part, policy=policy))
            return 0
        if args.command == "context":
            print(context(config, args.query, args.max_tokens, args.collection, policy=policy))
            return 0
        if args.command == "catalog":
            print(catalog(config, args.json))
            return 0
        if args.command == "status":
            rendered, fresh = _status(config, args.json, args.max_age_hours)
            print(rendered)
            return 0 if fresh or not args.require_fresh else 1
        if args.command == "doctor":
            rendered, ok = _doctor(config, args.json)
            print(rendered)
            return 0 if ok else 1
        if args.command == "capture":
            print(_capture(config, args))
            return 0
        if args.command == "feedback":
            if args.feedback_command == "list":
                if not 1 <= args.limit <= MAX_LIST_LIMIT:
                    raise ValueError(
                        "feedback list limit must be within [1, %d]" % MAX_LIST_LIMIT
                    )
                items = list_failures(config, status=args.status, limit=args.limit)
                print(
                    json_dumps(items, pretty=True)
                    if args.json
                    else "\n".join(_render_feedback_summary(item) for item in items)
                )
                return 0
            if args.feedback_command == "show":
                item = show_failure(config, args.id)
                print(
                    json_dumps(item, pretty=True)
                    if args.json
                    else "\n".join("%s: %s" % pair for pair in item.items())
                )
                return 0
            if args.feedback_command == "report":
                if not args.expected_path and not args.notes:
                    raise ValueError("feedback report requires --expected-path or --notes")
                if args.collection:
                    source_by_name(config, args.collection)
                failure_id = record_failure(
                    config,
                    args.query,
                    reason="relevance_miss",
                    collection=args.collection,
                    origin="explicit",
                    expected_path=args.expected_path,
                    notes=args.notes,
                )
                item = show_failure(config, failure_id)
                print(json_dumps(item, pretty=True) if args.json else _render_feedback_summary(item))
                return 0
            if args.feedback_command == "resolve":
                item = resolve_failure(
                    config,
                    args.id,
                    status=args.status,
                    notes=args.notes,
                )
                print(json_dumps(item, pretty=True) if args.json else _render_feedback_summary(item))
                return 0
            if args.feedback_command == "export":
                if not 1 <= args.limit <= MAX_EXPORT_LIMIT:
                    raise ValueError(
                        "feedback export limit must be within [1, %d]" % MAX_EXPORT_LIMIT
                    )
                content = export_failures(
                    config,
                    export_format=args.format,
                    status=args.status,
                    limit=args.limit,
                )
                if args.output:
                    print(write_feedback_export(config, args.output, content))
                else:
                    print(content, end="")
                return 0
            if args.feedback_command == "prune":
                report = prune_failures(config, dry_run=args.dry_run)
                print(
                    json_dumps(report, pretty=True)
                    if args.json
                    else " ".join("%s=%s" % pair for pair in report.items())
                )
                return 0
            raise AssertionError("unhandled feedback command")
        if args.command == "eval":
            eval_path = args.file or (config.home / "eval" / "gold_queries.jsonl")
            report = _evaluate(config, eval_path)
            passed = (
                report["hit_rate_at_k"] >= args.fail_under
                and report["mrr"] >= args.fail_under_mrr
                and report["negative_pass_rate"] >= 1.0
                and report["contamination_rate"] <= args.max_contamination_rate
                and report["average_search_units"] <= args.max_average_units
                and report["budget_violations"] <= args.allow_budget_violations
            )
            report["passed"] = passed
            report["thresholds"] = {
                "hit_rate_at_k": args.fail_under,
                "mrr": args.fail_under_mrr,
                "max_contamination_rate": args.max_contamination_rate,
                "max_average_search_units": args.max_average_units,
                "allowed_budget_violations": args.allow_budget_violations,
            }
            print(json_dumps(report, pretty=True) if args.json else (
                "cases=%d passed=%d hits=%d hit_rate@k=%.4f mrr=%.4f contamination=%.4f avg_units=%.1f budget_violations=%d gate=%s" % (
                    report["cases"], report["passed_cases"], report["hits"],
                    report["hit_rate_at_k"], report["mrr"], report["contamination_rate"],
                    report["average_search_units"], report["budget_violations"],
                    "pass" if passed else "fail",
                )
            ))
            return 0 if passed else 1
        if args.command == "compact":
            report = _compact(config)
            print(
                json_dumps(report, pretty=True)
                if args.json
                else "integrity=%s chunks=%d reclaimed_bytes=%d secure_delete=%s"
                % (
                    report["integrity"],
                    report["chunks"],
                    report["reclaimed_bytes"],
                    report["secure_delete"],
                )
            )
            return 0
        if args.command == "mcp":
            return run_server(config)
        raise AssertionError("unhandled command")
    except (ValueError, KeyError, FileNotFoundError, StaleReferenceError, sqlite3.Error, OSError, RuntimeError) as exc:
        print("amem: %s" % exc, file=sys.stderr)
        return 2


def main(argv: Optional[Sequence[str]] = None) -> int:
    # amem is commonly launched as a long-running MCP process.  Keep every
    # file created by this invocation owner-only, while restoring the caller's
    # mask when main() is embedded in a test or another Python application.
    previous_umask = os.umask(0o077)
    try:
        return _main(argv)
    finally:
        os.umask(previous_umask)


if __name__ == "__main__":
    raise SystemExit(main())
