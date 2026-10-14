from __future__ import annotations

import io
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
import unicodedata
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import agent_memory.search as search_module
import agent_memory.indexer as indexer_module
from agent_memory.cli import _compact, _evaluate, _status, main
from agent_memory.config import load_config, source_by_name
from agent_memory.database import connect, delete_document, writer_transaction
from agent_memory.indexer import SyncStats, ensure_fresh, sync_all
from agent_memory.mcp_server import TOOLS, handle_request
from agent_memory.search import (
    SearchHit,
    StaleReferenceError,
    fetch,
    format_search_with_visible_hits,
    search,
)
from agent_memory.util import estimate_tokens, utc_now
from agent_memory.memory_policy import MemoryPolicy


EXTENSIONS = [".md", ".txt", ".py", ".json", ".hwpx", ".xlsx", ".pdf"]


def make_home(base: Path, source: Path, *, max_archive: int = 1_000_000) -> Path:
    home = base / "memory"
    (home / "vault" / "inbox").mkdir(parents=True)
    config = {
        "version": 1,
        "database": ".state/index.sqlite3",
        "ingestion": {
            "chunk_chars": 300,
            "chunk_overlap_chars": 30,
            "max_text_file_bytes": 1_000_000,
            "max_pdf_file_bytes": 1_000_000,
            "max_archive_uncompressed_bytes": max_archive,
        },
        "retrieval": {
            "default_results": 5,
            "default_snippet_chars": 120,
            "default_context_tokens": 512,
        },
        "sources": [
            {
                "name": "test",
                "path": str(source),
                "description": "test corpus",
                "kind": "research",
                "trust": "canonical",
                "priority": 1.0,
                "include_extensions": EXTENSIONS,
            },
            {
                "name": "vault",
                "path": "vault",
                "description": "memory vault",
                "kind": "curated_memory",
                "trust": "mixed",
                "priority": 1.2,
                "include_extensions": [".md"],
            },
        ],
    }
    (home / "config.json").write_text(json.dumps(config), encoding="utf-8")
    return home


class MemorySystemTests(unittest.TestCase):
    def test_exact_document_name_outranks_repeated_content_terms(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "ARCHITECTURE.md").write_text(
                "# 시스템 구조\n\nAuthority 경계와 immutable frontier를 설명한다.\n",
                encoding="utf-8",
            )
            for ordinal in range(8):
                (source / ("architecture_helper_%d.py" % ordinal)).write_text(
                    ("authority frontier architecture retrieval receipt\n" * 30),
                    encoding="utf-8",
                )

            home = make_home(root, source)
            config = load_config(home)
            sync_all(config)

            results = search(config, "ARCHITECTURE.md authority frontier", collection="test")
            self.assertTrue(results)
            self.assertEqual(results[0].relative_path, "ARCHITECTURE.md")

    def test_sync_is_read_only_and_korean_search_handles_spacing_nfd_and_two_chars(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            korean = source / "learning.md"
            korean.write_text(
                "# 학습 설계\n\n행동 심리학과 학습 지속성을 연결한다. 수능특강 목차를 첫 파티션으로 보존한다.\n",
                encoding="utf-8",
            )
            code = source / "worker.py"
            code.write_text("def subject_loop_heartbeat():\n    return 'geometry chemistry1'\n", encoding="utf-8")
            (source / ".env").write_text("SECRET=must-not-index", encoding="utf-8")
            outside = root / "outside.txt"
            outside.write_text("outside private payload", encoding="utf-8")
            try:
                (source / "escape.txt").symlink_to(outside)
            except OSError:
                pass

            before = {
                path.name: (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns)
                for path in (korean, code, source / ".env")
            }
            names_before = sorted(path.name for path in source.iterdir())
            home = make_home(root, source)
            config = load_config(home)
            results = sync_all(config)
            self.assertEqual(sum(item.errors for item in results), 0)

            names_after = sorted(path.name for path in source.iterdir())
            self.assertEqual(names_before, names_after)
            after = {
                path.name: (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns)
                for path in (korean, code, source / ".env")
            }
            self.assertEqual(before, after)

            self.assertTrue(search(config, "행동심리학", collection="test"))
            self.assertTrue(search(config, "심리", collection="test"))
            nfd = unicodedata.normalize("NFD", "학습 지속성")
            self.assertTrue(search(config, nfd, collection="test"))
            self.assertTrue(search(config, "subject_loop_heartbeat", collection="test"))
            self.assertFalse(search(config, "must-not-index", collection="test"))
            self.assertFalse(search(config, "outside private payload", collection="test"))

    def test_generation_ref_fails_closed_after_same_size_same_mtime_change_and_delete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            document = source / "decision.md"
            document.write_text("# Decision\n\nalpha architecture", encoding="utf-8")
            home = make_home(root, source)
            config = load_config(home)
            sync_all(config)
            first = search(config, "alpha architecture", collection="test")[0]
            self.assertIn("alpha architecture", fetch(config, first.ref))

            original_stat = document.stat()
            document.write_text("# Decision\n\nbravo architecture", encoding="utf-8")
            self.assertEqual(document.stat().st_size, original_stat.st_size)
            os.utime(document, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
            with self.assertRaises(StaleReferenceError):
                fetch(config, first.ref)
            self.assertFalse(search(config, "alpha architecture", collection="test"))

            sync_all(config)
            second = search(config, "bravo architecture", collection="test")[0]
            self.assertNotEqual(first.ref, second.ref)
            with self.assertRaises(StaleReferenceError):
                fetch(config, first.ref)

            document.unlink()
            with self.assertRaises(StaleReferenceError):
                fetch(config, second.ref)
            self.assertFalse(search(config, "bravo architecture", collection="test"))
            sync_all(config)
            connection = connect(config, readonly=True)
            try:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0], 0)
            finally:
                connection.close()

    def test_projection_or_source_policy_change_invalidates_refs_before_resync(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "guide.md").write_text("projection fingerprint sentinel", encoding="utf-8")
            home = make_home(root, source)
            config = load_config(home)
            sync_all(config)
            original = search(config, "projection fingerprint", collection="test")[0]

            config_path = home / "config.json"
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            payload["ingestion"]["chunk_chars"] = 180
            config_path.write_text(json.dumps(payload), encoding="utf-8")
            changed = load_config(home)
            with self.assertRaises(StaleReferenceError):
                fetch(changed, original.ref)
            self.assertFalse(search(changed, "projection fingerprint", collection="test"))

            sync_all(changed)
            replacement = search(changed, "projection fingerprint", collection="test")[0]
            self.assertNotEqual(original.ref, replacement.ref)

            payload["sources"][0]["exclude_globs"] = ["*.md"]
            config_path.write_text(json.dumps(payload), encoding="utf-8")
            excluded = load_config(home)
            with self.assertRaises(StaleReferenceError):
                fetch(excluded, replacement.ref)
            self.assertFalse(search(excluded, "projection fingerprint", collection="test"))

    def test_fetch_fails_closed_when_sync_commits_during_read_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            document = source / "concurrent.md"
            document.write_text("old generation concurrent evidence", encoding="utf-8")
            home = make_home(root, source)
            config = load_config(home)
            sync_all(config)
            old_ref = search(config, "concurrent evidence", collection="test")[0].ref

            validated = threading.Event()
            resume = threading.Event()
            outcome = {}
            original_validate = search_module._validate_document
            first_call = {"pending": True}

            def gated_validate(row, current_config, hash_cache):  # type: ignore[no-untyped-def]
                result = original_validate(row, current_config, hash_cache)
                if threading.current_thread().name == "fetch-reader" and first_call["pending"]:
                    first_call["pending"] = False
                    validated.set()
                    self.assertTrue(resume.wait(5))
                return result

            def reader() -> None:
                try:
                    outcome["value"] = fetch(config, old_ref)
                except BaseException as exc:  # capture the thread outcome for the main assertion
                    outcome["error"] = exc

            with patch.object(search_module, "_validate_document", side_effect=gated_validate):
                thread = threading.Thread(target=reader, name="fetch-reader")
                thread.start()
                self.assertTrue(validated.wait(5))
                document.write_text("new generation concurrent evidence", encoding="utf-8")
                sync_all(config)
                resume.set()
                thread.join(5)

            self.assertFalse(thread.is_alive())
            self.assertNotIn("value", outcome)
            self.assertIsInstance(outcome.get("error"), StaleReferenceError)

    def test_incremental_sync_removes_renames_without_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            original = source / "old.md"
            original.write_text("rename sentinel knowledge", encoding="utf-8")
            home = make_home(root, source)
            config = load_config(home)
            first = sync_all(config)
            self.assertEqual(first[0].indexed, 1)
            second = sync_all(config)
            self.assertEqual(second[0].unchanged, 1)
            renamed = source / "new.md"
            original.rename(renamed)
            third = sync_all(config)
            self.assertEqual(third[0].removed, 1)
            connection = connect(config, readonly=True)
            try:
                paths = [row[0] for row in connection.execute("SELECT path FROM documents WHERE collection_name='test' AND status='ok'")]
                self.assertEqual(paths, [str(renamed.resolve())])
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM chunks WHERE document_id IN (SELECT id FROM documents WHERE collection_name='test')").fetchone()[0], 1)
            finally:
                connection.close()

    def test_file_disappearing_after_enumeration_removes_the_stale_manifest_row(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            document = source / "vanishing.md"
            document.write_text("vanishing stale evidence", encoding="utf-8")
            home = make_home(root, source)
            config = load_config(home)
            sync_all(config)

            original_index_one = indexer_module._index_one

            def disappear(connection, current_config, current_source, path, source_root):  # type: ignore[no-untyped-def]
                if path.resolve() == document.resolve():
                    path.unlink()
                    raise FileNotFoundError(str(document))
                return original_index_one(connection, current_config, current_source, path, source_root)

            with patch.object(indexer_module, "_index_one", side_effect=disappear):
                result = sync_all(config)[0]

            self.assertEqual(result.errors, 1)
            self.assertEqual(result.removed, 1)
            connection = connect(config, readonly=True)
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM documents WHERE collection_name='test'"
                    ).fetchone()[0],
                    0,
                )
            finally:
                connection.close()

    def test_fts_rowids_are_shared_and_legacy_mismatch_deletes_safely(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "target.md").write_text("target projection evidence", encoding="utf-8")
            (source / "keep.md").write_text("unrelated projection evidence", encoding="utf-8")
            (source / "aligned.md").write_text("aligned projection evidence", encoding="utf-8")
            home = make_home(root, source)
            config = load_config(home)
            sync_all(config)

            connection = connect(config)
            try:
                aligned = connection.execute(
                    "SELECT d.id AS document_id FROM documents d "
                    "WHERE d.relative_path='aligned.md'"
                ).fetchone()
                trace = []
                connection.set_trace_callback(trace.append)
                with writer_transaction(connection):
                    delete_document(connection, aligned["document_id"])
                connection.set_trace_callback(None)
                self.assertFalse(
                    [
                        statement
                        for statement in trace
                        if statement.startswith("DELETE FROM chunks_fts WHERE chunk_id=")
                        or statement.startswith("DELETE FROM chunks_trigram WHERE chunk_id=")
                    ],
                    trace,
                )

                target = connection.execute(
                    "SELECT d.id AS document_id, c.rowid AS storage_rowid, c.id AS chunk_id "
                    "FROM documents d JOIN chunks c ON c.document_id=d.id "
                    "WHERE d.relative_path='target.md'"
                ).fetchone()
                self.assertIsNotNone(target)
                self.assertEqual(
                    connection.execute(
                        "SELECT chunk_id FROM chunks_fts WHERE rowid=?",
                        (target["storage_rowid"],),
                    ).fetchone()[0],
                    target["chunk_id"],
                )

                fts_row = connection.execute(
                    "SELECT title, heading, path, body FROM chunks_fts WHERE rowid=?",
                    (target["storage_rowid"],),
                ).fetchone()
                connection.execute("DELETE FROM chunks_fts WHERE rowid=?", (target["storage_rowid"],))
                connection.execute(
                    "INSERT INTO chunks_fts(rowid, chunk_id, title, heading, path, body) "
                    "VALUES(?, ?, ?, ?, ?, ?)",
                    (
                        int(target["storage_rowid"]) + 10_000,
                        target["chunk_id"],
                        fts_row["title"],
                        fts_row["heading"],
                        fts_row["path"],
                        fts_row["body"],
                    ),
                )
                connection.commit()
                mismatch_trace = []
                connection.set_trace_callback(mismatch_trace.append)
                with writer_transaction(connection):
                    delete_document(connection, target["document_id"])
                connection.set_trace_callback(None)
                self.assertEqual(
                    len(
                        [
                            statement
                            for statement in mismatch_trace
                            if statement.startswith("DELETE FROM chunks_fts WHERE chunk_id=")
                        ]
                    ),
                    1,
                    mismatch_trace,
                )
                self.assertFalse(
                    [
                        statement
                        for statement in mismatch_trace
                        if statement.startswith("DELETE FROM chunks_trigram WHERE chunk_id=")
                    ],
                    mismatch_trace,
                )

                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM chunks_fts WHERE chunk_id=?",
                        (target["chunk_id"],),
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM chunks_fts WHERE body MATCH 'unrelated'"
                    ).fetchone()[0],
                    1,
                )
            finally:
                connection.close()

    def test_collection_filter_is_applied_before_candidate_cutoff(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            for index in range(60):
                (source / ("distractor-%02d.md" % index)).write_text(
                    "shared retrieval phrase distractor %d" % index,
                    encoding="utf-8",
                )
            home = make_home(root, source)
            target = home / "vault" / "target.md"
            target.write_text("---\nkind: fact\nstatus: reviewed\nscope: global\nsensitivity: private\nconfidence: high\nsource: test\n---\nshared retrieval phrase curated target", encoding="utf-8")
            config = load_config(home)
            sync_all(config)

            hits = search(config, "shared retrieval phrase", collection="vault", limit=1)
            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0].collection, "vault")
            self.assertEqual(Path(hits[0].path), target.resolve())

    def test_fetch_budget_counts_headers_and_supports_continuation_parts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "long.md").write_text("# 긴 문서\n\n" + "한국어근거문장 " * 900, encoding="utf-8")
            home = make_home(root, source)
            config = load_config(home)
            sync_all(config)
            ref = search(config, "한국어근거문장", collection="test", limit=1)[0].ref
            first = fetch(config, ref, max_tokens=256, part=1)
            self.assertLessEqual(estimate_tokens(first), 256)
            self.assertIn("continuation:", first)
            second = fetch(config, ref, max_tokens=256, part=2)
            self.assertLessEqual(estimate_tokens(second), 256)
            self.assertNotEqual(first, second)

    def test_hwpx_extraction_and_zip_bomb_limit_do_not_block_other_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "good.md").write_text("independent healthy document", encoding="utf-8")
            with zipfile.ZipFile(source / "lesson.hwpx", "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr(
                    "Contents/section0.xml",
                    '<hs:section xmlns:hs="urn:test" xmlns:hp="urn:p"><hp:p><hp:t>행동심리학 수업 설계</hp:t></hp:p></hs:section>',
                )
            with zipfile.ZipFile(source / "oversized.hwpx", "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("Contents/section0.xml", "x" * 5000)
            home = make_home(root, source, max_archive=1000)
            config = load_config(home)
            results = sync_all(config)
            test_stats = results[0]
            self.assertGreaterEqual(test_stats.errors, 1)
            self.assertTrue(search(config, "independent healthy", collection="test"))
            self.assertTrue(search(config, "행동심리학", collection="test"))

    def test_xlsx_shared_inline_and_numeric_cells_are_searchable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            with zipfile.ZipFile(source / "research.xlsx", "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr(
                    "xl/sharedStrings.xml",
                    '<sst xmlns="urn:x"><si><t>행동심리학</t></si><si><r><t>학습 </t></r><r><t>지속성</t></r></si></sst>',
                )
                archive.writestr(
                    "xl/worksheets/sheet1.xml",
                    '<worksheet xmlns="urn:x"><sheetData><row>'
                    '<c t="s"><v>0</v></c><c t="s"><v>1</v></c><c><v>2027</v></c>'
                    '<c t="inlineStr"><is><t>UX 실험</t></is></c>'
                    '</row></sheetData></worksheet>',
                )
            home = make_home(root, source)
            config = load_config(home)
            results = sync_all(config)
            self.assertEqual(results[0].errors, 0)
            self.assertTrue(search(config, "행동심리학 학습 지속성", collection="test"))
            self.assertTrue(search(config, "UX 실험 2027", collection="test"))

    def test_mcp_supports_current_discovery_legacy_initialize_and_three_tools(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "paper.md").write_text("retrieval protocol evidence", encoding="utf-8")
            home = make_home(root, source)
            config = load_config(home)
            sync_all(config)

            discover = handle_request(config, {"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {}})
            self.assertIn("2026-07-28", discover["result"]["supportedVersions"])  # type: ignore[index]
            initialized = handle_request(
                config,
                {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
            )
            self.assertEqual(initialized["result"]["protocolVersion"], "2025-11-25")  # type: ignore[index]
            self.assertEqual([item["name"] for item in TOOLS], ["memory_catalog", "memory_search", "memory_fetch"])
            result = handle_request(
                config,
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "memory_search", "arguments": {"query": "retrieval protocol"}},
                },
            )
            self.assertIn("paper", result["result"]["content"][0]["text"])  # type: ignore[index]

    def test_capture_requires_explicit_command_and_indexes_inbox(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            home = make_home(root, source)
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "--home", str(home), "capture", "--kind", "decision",
                        "--title", "한국어 평가 기준", "--body", "gold query 개선이 있을 때만 벡터를 채택한다.",
                    ]
                )
            self.assertEqual(exit_code, 0)
            captured = Path(stdout.getvalue().strip())
            self.assertTrue(captured.is_file())
            config = load_config(home)
            self.assertFalse(search(config, "gold query 벡터", collection="vault"))
            self.assertTrue(search(config, "gold query 벡터", collection="vault", policy=MemoryPolicy(status="inbox")))

    def test_status_distinguishes_readable_index_from_age_based_freshness(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "current.md").write_text("current index evidence", encoding="utf-8")
            home = make_home(root, source)
            config = load_config(home)
            sync_all(config)

            rendered, fresh = _status(config, True, max_age_hours=8)
            self.assertTrue(fresh)
            self.assertTrue(json.loads(rendered)["ready"])

            connection = connect(config)
            connection.execute(
                "UPDATE collections SET last_sync_at='2000-01-01T00:00:00Z', last_sync_ok=1"
            )
            connection.commit()
            connection.close()

            rendered, fresh = _status(config, True, max_age_hours=8)
            payload = json.loads(rendered)
            self.assertFalse(fresh)
            self.assertTrue(payload["ready"])
            self.assertEqual(set(payload["sync_overdue"]), {"test", "vault"})
            with redirect_stdout(io.StringIO()):
                exit_code = main(
                    ["--home", str(home), "status", "--require-fresh", "--max-age-hours", "8"]
                )
            self.assertEqual(exit_code, 1)

    def test_missing_source_root_and_future_timestamp_fail_freshness_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "current.md").write_text("source availability evidence", encoding="utf-8")
            home = make_home(root, source)
            config = load_config(home)
            sync_all(config)

            connection = connect(config)
            connection.execute(
                "UPDATE collections SET last_sync_at='2099-01-01T00:00:00Z', last_sync_ok=1 "
                "WHERE name='test'"
            )
            connection.commit()
            connection.close()
            rendered, fresh = _status(config, True, max_age_hours=8)
            self.assertFalse(fresh)
            self.assertIn("test", json.loads(rendered)["sync_overdue"])

            source.rename(root / "source-unmounted")
            connection = connect(config, readonly=True)
            before_dry_run = tuple(
                connection.execute(
                    "SELECT last_sync_at, last_sync_ok, last_sync_message "
                    "FROM collections WHERE name='test'"
                ).fetchone()
            )
            connection.close()
            dry_run = sync_all(config, collection="test", dry_run=True)[0]
            self.assertEqual(dry_run.errors, 1)
            connection = connect(config, readonly=True)
            after_dry_run = tuple(
                connection.execute(
                    "SELECT last_sync_at, last_sync_ok, last_sync_message "
                    "FROM collections WHERE name='test'"
                ).fetchone()
            )
            connection.close()
            self.assertEqual(before_dry_run, after_dry_run)

            result = ensure_fresh(
                config,
                source_by_name(config, "test"),
                max_age_seconds=3600,
            )
            self.assertEqual(result.errors, 1)
            rendered, fresh = _status(config, True, max_age_hours=8)
            payload = json.loads(rendered)
            self.assertFalse(fresh)
            self.assertIn("test", payload["source_missing"])
            self.assertIn("test", payload["sync_failed"])

    def test_dry_run_is_scan_only_and_never_creates_or_mutates_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "current.md").write_text("dry run evidence", encoding="utf-8")
            home = make_home(root, source)
            config = load_config(home)

            result = sync_all(config, collection="test", dry_run=True)[0]
            self.assertEqual(result.scanned, 1)
            self.assertEqual(result.message, "dry-run scan only")
            self.assertFalse((home / ".state").exists())

            sync_all(config)
            config_payload = json.loads((home / "config.json").read_text(encoding="utf-8"))
            config_payload["sources"][0]["priority"] = 9.0
            (home / "config.json").write_text(json.dumps(config_payload), encoding="utf-8")
            changed_config = load_config(home)
            state = home / ".state"
            before = {
                item.relative_to(state).as_posix(): item.read_bytes()
                for item in state.iterdir()
                if item.is_file()
            }

            result = sync_all(changed_config, collection="test", dry_run=True)[0]
            after = {
                item.relative_to(state).as_posix(): item.read_bytes()
                for item in state.iterdir()
                if item.is_file()
            }
            self.assertEqual(result.scanned, 1)
            self.assertEqual(before, after)
            connection = connect(config, readonly=True)
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT priority FROM collections WHERE name='test'"
                    ).fetchone()[0],
                    1.0,
                )
            finally:
                connection.close()

    def test_unreadable_subtree_preserves_rows_and_fails_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            nested = source / "sub"
            nested.mkdir(parents=True)
            (nested / "only.md").write_text("preserve on traversal failure", encoding="utf-8")
            home = make_home(root, source)
            config = load_config(home)
            sync_all(config)

            def failing_walk(root_path, *, topdown, followlinks, onerror):
                yield str(root_path), ["sub"], []
                onerror(PermissionError(13, "Permission denied", str(nested)))

            with patch.object(indexer_module.os, "walk", side_effect=failing_walk):
                failed = sync_all(config, collection="test")[0]
            self.assertEqual(failed.errors, 1)
            self.assertEqual(failed.removed, 0)
            self.assertIn("incomplete traversal", failed.message)

            connection = connect(config, readonly=True)
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM documents WHERE collection_name='test'"
                    ).fetchone()[0],
                    1,
                )
                sync_row = connection.execute(
                    "SELECT last_sync_ok, last_sync_message FROM collections WHERE name='test'"
                ).fetchone()
                self.assertEqual(sync_row[0], 0)
                self.assertIn("incomplete traversal", sync_row[1])
            finally:
                connection.close()

            state = home / ".state"
            before = {
                item.relative_to(state).as_posix(): item.read_bytes()
                for item in state.iterdir()
                if item.is_file()
            }
            with patch.object(indexer_module.os, "walk", side_effect=failing_walk):
                dry_run = sync_all(config, collection="test", dry_run=True)[0]
            after = {
                item.relative_to(state).as_posix(): item.read_bytes()
                for item in state.iterdir()
                if item.is_file()
            }
            self.assertEqual(dry_run.errors, 1)
            self.assertIn("dry-run traversal failed", dry_run.message)
            self.assertEqual(before, after)

    def test_eval_tracks_negative_queries_contamination_and_output_budgets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "target.md").write_text("pricing evidence sentinel", encoding="utf-8")
            home = make_home(root, source)
            config = load_config(home)
            sync_all(config)
            gold = home / "eval.jsonl"
            cases = [
                {
                    "id": "positive",
                    "query": "pricing evidence sentinel",
                    "relevant_paths": ["target.md"],
                    "forbidden_paths": ["target.md"],
                    "max_search_units": 1,
                    "collection": "test",
                },
                {
                    "id": "negative",
                    "query": "credential-that-is-not-indexed-7f3d",
                    "expect_no_results": True,
                    "collection": "test",
                },
            ]
            gold.write_text(
                "\n".join(json.dumps(case) for case in cases) + "\n",
                encoding="utf-8",
            )

            report = _evaluate(config, gold)
            self.assertEqual(report["positive_cases"], 1)
            self.assertEqual(report["negative_cases"], 1)
            self.assertEqual(report["hit_rate_at_k"], 1.0)
            self.assertEqual(report["negative_pass_rate"], 1.0)
            self.assertEqual(report["contamination_rate"], 1.0)
            self.assertEqual(report["budget_violations"], 1)
            self.assertGreater(report["average_search_units"], 0)
            with redirect_stdout(io.StringIO()):
                exit_code = main(["--home", str(home), "eval", "--file", str(gold)])
            self.assertEqual(exit_code, 1)

    def test_eval_does_not_credit_hits_truncated_from_the_serialized_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            home = make_home(root, source)
            config = load_config(home)
            sync_all(config)
            gold = home / "eval.jsonl"
            gold.write_text(
                json.dumps(
                    {
                        "query": "bounded output",
                        "relevant_paths": ["target.md"],
                        "forbidden_paths": ["target.md"],
                        "k": 7,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            hits = []
            for rank in range(1, 8):
                relative = "target.md" if rank == 7 else "distractor-%d.md" % rank
                hits.append(
                    SearchHit(
                        rank=rank,
                        ref=("a" * 16) + "@" + ("b" * 16) + ".p3c1f" + ("c" * 16) + "#0",
                        document_id="doc-%d" % rank,
                        generation="generation",
                        collection="test",
                        kind="research",
                        trust="canonical",
                        title="Long distractor %d" % rank,
                        path=str(source / relative),
                        relative_path=relative,
                        heading="heading",
                        locator="lines 1-2",
                        start_line=1,
                        end_line=2,
                        page=None,
                        token_estimate=2000,
                        snippet=("long navigation text " * 160),
                        score=1.0 / rank,
                    )
                )

            # Keep this a truncation test even when the ordinary renderer fits
            # more candidate refs by shortening snippets and sharing paths.
            with patch("agent_memory.cli.search", return_value=hits), patch(
                "agent_memory.cli.SEARCH_OUTPUT_MAX_UNITS", 512
            ):
                report = _evaluate(config, gold)
            self.assertEqual(report["hit_rate_at_k"], 0.0)
            self.assertEqual(report["contamination_rate"], 0.0)
            self.assertLess(report["details"][0]["result_count"], 7)

    def test_search_renderer_holds_exact_budget_and_skips_oversized_metadata(self) -> None:
        def hit(rank: int, *, locator: str = "line 1", path: str = "/source.md") -> SearchHit:
            return SearchHit(
                rank=rank,
                ref=("a" * 16) + "@" + ("b" * 16) + ".p3c1f" + ("c" * 16) + "#0",
                document_id="doc-%d" % rank,
                generation="generation",
                collection="research",
                kind="research",
                trust="canonical",
                title="title",
                path=path,
                relative_path=Path(path).name,
                heading="heading",
                locator=locator,
                start_line=1,
                end_line=1,
                page=None,
                token_estimate=100,
                snippet="useful evidence " * 80,
                score=1.0 / rank,
            )

        rendered, visible = format_search_with_visible_hits(
            [hit(1, locator="x" * 5000, path="/" + "p" * 5000), hit(2)],
            max_units=4000,
        )
        self.assertLessEqual(estimate_tokens(rendered), 4000)
        self.assertTrue(visible)
        self.assertIn("doc-2", {item.document_id for item in visible})

        rendered, _ = format_search_with_visible_hits(
            [hit(1, path="/" + "q" * 3817)],
            max_units=4000,
        )
        self.assertLessEqual(estimate_tokens(rendered), 4000)

    def test_concurrent_ensure_fresh_calls_coalesce_under_the_sync_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "current.md").write_text("freshness coalescing evidence", encoding="utf-8")
            home = make_home(root, source)
            config = load_config(home)
            sync_all(config)
            connection = connect(config)
            connection.execute(
                "UPDATE collections SET last_sync_at='2000-01-01T00:00:00Z', last_sync_ok=1 "
                "WHERE name='test'"
            )
            connection.commit()
            connection.close()

            calls = []
            outcomes = []
            failures = []
            start = threading.Barrier(3)

            def fake_sync(current_config, current_source, dry_run=False, verbose=False):  # type: ignore[no-untyped-def]
                calls.append(current_source.name)
                time.sleep(0.05)
                writable = connect(current_config)
                writable.execute(
                    "UPDATE collections SET last_sync_at=?, last_sync_ok=1 WHERE name=?",
                    (utc_now(), current_source.name),
                )
                writable.commit()
                writable.close()
                result = SyncStats(collection=current_source.name)
                result.message = "reconciled"
                return result

            def worker() -> None:
                try:
                    start.wait(2)
                    outcomes.append(
                        ensure_fresh(
                            config,
                            source_by_name(config, "test"),
                            max_age_seconds=3600,
                        )
                    )
                except BaseException as exc:
                    failures.append(exc)

            with patch.object(indexer_module, "sync_source", side_effect=fake_sync):
                threads = [threading.Thread(target=worker) for _ in range(2)]
                for thread in threads:
                    thread.start()
                start.wait(2)
                for thread in threads:
                    thread.join(3)

            self.assertFalse(failures)
            self.assertEqual(calls, ["test"])
            self.assertEqual(len(outcomes), 2)
            self.assertEqual(
                sorted(result.message.startswith("fresh") for result in outcomes),
                [False, True],
            )

    def test_compact_scrubs_deleted_plaintext_and_preserves_projection_invariants(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            sentinel = "forensic plaintext sentinel 91d0f7be"
            (source / "ordinary.md").write_text("ordinary retained content", encoding="utf-8")
            home = make_home(root, source)
            config_path = home / "config.json"
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            payload["storage"] = {"secure_delete": False}
            config_path.write_text(json.dumps(payload), encoding="utf-8")
            legacy_config = load_config(home)
            sync_all(legacy_config)

            # Manufacture a deterministic pre-secure-delete free-page residue.
            # FTS encodings and WAL checkpoints can make a naturally deleted
            # document's exact byte sequence nondeterministic, so use an
            # ordinary legacy table and force its large payload into the main
            # database before deleting it with secure_delete disabled.
            raw = sqlite3.connect(legacy_config.database)
            try:
                raw.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                raw.execute("PRAGMA journal_mode=DELETE")
                raw.execute("PRAGMA secure_delete=OFF")
                raw.execute("CREATE TABLE legacy_residue(payload TEXT NOT NULL)")
                raw.execute(
                    "INSERT INTO legacy_residue(payload) VALUES (?)",
                    ((sentinel + "|") * 10_000,),
                )
                raw.commit()
                raw.execute("DELETE FROM legacy_residue")
                raw.commit()
            finally:
                raw.close()
            legacy_bytes = legacy_config.database.read_bytes()
            self.assertTrue(
                sentinel.encode("utf-8") in legacy_bytes,
                "legacy fixture did not retain deleted plaintext",
            )

            payload["storage"] = {"secure_delete": True}
            config_path.write_text(json.dumps(payload), encoding="utf-8")
            config = load_config(home)
            report = _compact(config)
            self.assertEqual(report["integrity"], "ok")
            self.assertEqual(report["chunks"], report["word_projection_rows"])
            self.assertEqual(report["chunks"], report["trigram_projection_rows"])
            writable = connect(config)
            try:
                self.assertEqual(writable.execute("PRAGMA secure_delete").fetchone()[0], 1)
                for table in ("chunks_fts", "chunks_trigram"):
                    self.assertEqual(
                        writable.execute(
                            "SELECT v FROM %s_config WHERE k='secure-delete'" % table
                        ).fetchone()[0],
                        1,
                    )
            finally:
                writable.close()
            remaining = config.database.read_bytes()
            wal = Path(str(config.database) + "-wal")
            if wal.exists():
                remaining += wal.read_bytes()
            self.assertNotIn(sentinel.encode("utf-8"), remaining)

    def test_compact_fails_closed_while_a_reader_retains_wal_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            sentinel = "reader retained plaintext sentinel 087ee2a4"
            document = source / "sensitive.md"
            document.write_text(sentinel, encoding="utf-8")
            home = make_home(root, source)
            config = load_config(home)
            sync_all(config)

            reader = connect(config, readonly=True)
            reader.execute("BEGIN")
            reader.execute("SELECT COUNT(*) FROM chunks").fetchone()
            document.unlink()
            sync_all(config)
            with self.assertRaisesRegex(sqlite3.OperationalError, "reader holds the WAL"):
                _compact(config)
            wal = Path(str(config.database) + "-wal")
            self.assertTrue(wal.exists())

            reader.close()
            report = _compact(config)
            self.assertEqual(report["wal_checkpoint_before"], [0, 0, 0])
            self.assertEqual(report["wal_checkpoint_after"], [0, 0, 0])
            remaining = config.database.read_bytes()
            if wal.exists():
                remaining += wal.read_bytes()
            self.assertNotIn(sentinel.encode("utf-8"), remaining)


class BootstrapContractTests(unittest.TestCase):
    def test_agent_bootstraps_are_identical_and_small(self) -> None:
        root = Path(__file__).resolve().parents[1]
        agents = (root / "AGENTS.md").read_bytes()
        claude = (root / "CLAUDE.md").read_bytes()
        self.assertEqual(agents, claude)
        self.assertLess(len(agents), 2000)


if __name__ == "__main__":
    unittest.main()
