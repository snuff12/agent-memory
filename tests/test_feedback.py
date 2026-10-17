from __future__ import annotations

import io
import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from agent_memory.cli import _evaluate, main
from agent_memory.config import load_config
from agent_memory.feedback import (
    FEEDBACK_SCHEMA_VERSION,
    export_failures,
    feedback_stats,
    list_failures,
    prune_failures,
    record_failure,
    record_zero_result,
    resolve_failure,
    show_failure,
    triage_failure,
)
from agent_memory.indexer import sync_all
from agent_memory.mcp_server import handle_request


def make_feedback_home(base: Path, *, enabled: bool = True, max_records: int = 1000) -> Path:
    home = base / "memory"
    source = base / "source"
    source.mkdir()
    (source / "known.md").write_text("known retrieval evidence", encoding="utf-8")
    (home / "vault" / "inbox").mkdir(parents=True)
    payload = {
        "version": 1,
        "database": ".state/index.sqlite3",
        "retrieval": {
            "default_results": 5,
            "default_snippet_chars": 120,
            "default_context_tokens": 512,
        },
        "ingestion": {
            "chunk_chars": 300,
            "chunk_overlap_chars": 30,
            "max_text_file_bytes": 1000000,
            "max_pdf_file_bytes": 1000000,
            "max_archive_uncompressed_bytes": 1000000,
        },
        "storage": {"secure_delete": True},
        "feedback": {
            "auto_record_zero_results": enabled,
            "max_records": max_records,
            "resolved_retention_days": 180,
        },
        "sources": [
            {
                "name": "test",
                "path": str(source),
                "description": "test",
                "kind": "research",
                "trust": "canonical",
                "priority": 1,
                "include_extensions": [".md"],
            }
        ],
    }
    (home / "config.json").write_text(json.dumps(payload), encoding="utf-8")
    return home


def run_main(arguments):  # type: ignore[no-untyped-def]
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        result = main(arguments)
    return result, stdout.getvalue(), stderr.getvalue()


class FeedbackTests(unittest.TestCase):
    def test_review_and_dry_run_paths_are_readonly_and_missing_queue_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = make_feedback_home(Path(temporary))
            config = load_config(home)
            self.assertEqual(list_failures(config), [])
            self.assertEqual(
                export_failures(config, export_format="review", status="open", limit=10),
                "",
            )
            preview = prune_failures(config, dry_run=True)
            self.assertEqual(preview["would_remove"], 0)
            with self.assertRaises(KeyError):
                show_failure(config, 1)
            self.assertFalse((home / "feedback").exists())

            failure_id = record_failure(
                config,
                "readonly review query",
                reason="relevance_miss",
                collection="test",
                origin="explicit",
                expected_path="known.md",
            )
            feedback = home / "feedback"

            def snapshot():  # type: ignore[no-untyped-def]
                return {
                    path.name: (path.stat().st_size, path.stat().st_mtime_ns, path.read_bytes())
                    for path in feedback.iterdir()
                    if path.is_file()
                }

            before = snapshot()
            self.assertEqual(list_failures(config)[0]["id"], failure_id)
            self.assertEqual(show_failure(config, failure_id)["id"], failure_id)
            self.assertIn(
                "readonly review query",
                export_failures(config, export_format="review", status="open", limit=10),
            )
            self.assertEqual(prune_failures(config, dry_run=True)["would_remove"], 0)
            self.assertEqual(snapshot(), before)

    def test_cli_records_only_opted_in_zero_results_and_deduplicates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = make_feedback_home(root)
            config = load_config(home)
            sync_all(config)

            success = run_main(["--home", str(home), "search", "known retrieval"])
            self.assertEqual(success[0], 0)
            self.assertIn("known.md", success[1])
            self.assertFalse((home / "feedback").exists())

            for _ in range(2):
                missing = run_main(
                    ["--home", str(home), "search", "nonce-no-result-884422"]
                )
                self.assertEqual(missing[0], 0)
                self.assertEqual(missing[1], "No current, readable source matched.\n")
                self.assertEqual(missing[2], "")
            rows = list_failures(config)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["count"], 2)
            self.assertEqual(rows[0]["reason"], "zero_results")

            disabled_root = root / "disabled"
            disabled_root.mkdir()
            disabled_home = make_feedback_home(disabled_root, enabled=False)
            disabled_config = load_config(disabled_home)
            sync_all(disabled_config)
            result = run_main(
                ["--home", str(disabled_home), "search", "another-absent-nonce"]
            )
            self.assertEqual(result[0], 0)
            self.assertFalse((disabled_home / "feedback").exists())

    def test_secret_like_queries_are_hash_only_and_never_reach_sqlite_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = make_feedback_home(Path(temporary))
            config = load_config(home)
            secret = "sk-supersecretcredential123456789"
            failure_id = record_zero_result(
                config,
                "find account api_key=%s" % secret,
                collection="test",
                origin="cli_search",
            )
            self.assertIsNotNone(failure_id)
            item = show_failure(config, int(failure_id))
            self.assertTrue(item["secret_like"])
            self.assertIn("REDACTED_SECRET_LIKE_QUERY", item["query"])
            self.assertNotIn(secret, json.dumps(item))

            feedback = home / "feedback"
            for path in feedback.iterdir():
                if path.is_file():
                    self.assertFalse(secret.encode() in path.read_bytes(), path.name)
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode) & 0o077, 0)
            self.assertEqual(stat.S_IMODE(feedback.stat().st_mode), 0o700)

            second_id = record_failure(
                config,
                "ordinary relevance query",
                reason="relevance_miss",
                collection=None,
                origin="explicit",
                expected_path="api_key=%s" % secret,
                notes="contact owner@example.com",
            )
            second = show_failure(config, second_id)
            self.assertIn("REDACTED_SECRET_LIKE_VALUE", second["expected_path"])
            self.assertIn("REDACTED_SECRET_LIKE_VALUE", second["notes"])
            combined = b"".join(path.read_bytes() for path in feedback.iterdir() if path.is_file())
            self.assertNotIn(secret.encode(), combined)
            self.assertNotIn(b"owner@example.com", combined)

    def test_common_provider_tokens_and_korean_ids_are_conservatively_redacted(self) -> None:
        samples = [
            "900101-1234567",
            "xoxb-1234567890-abcdefghijkl",
            "AIza1234567890abcdefghijklmnopqrstuv",
            "glpat-abcdefghijklmnopqrst",
            "npm_abcdefghijklmnopqrstuvwxyz123456",
            # Construct the synthetic Stripe-shaped fixture at runtime so
            # provider push protection does not treat the source as a real key.
            "_".join(("sk", "live", "abcdefghijklmnopqrstuvwxyz")),
            "cookie=session-secret-value",
            "session_id: abcdefghijklmnop",
            "4111 1111 1111 1111",
            "카드번호 4111-1111-1111-1111",
        ]
        with tempfile.TemporaryDirectory() as temporary:
            home = make_feedback_home(Path(temporary))
            config = load_config(home)
            for sample in samples:
                failure_id = record_zero_result(
                    config,
                    "lookup %s" % sample,
                    collection=None,
                    origin="mcp_search",
                )
                item = show_failure(config, int(failure_id))
                self.assertTrue(item["secret_like"], sample)
                self.assertNotIn(sample, json.dumps(item), sample)
            combined = b"".join(
                path.read_bytes() for path in (home / "feedback").iterdir() if path.is_file()
            )
            for sample in samples:
                self.assertNotIn(sample.encode(), combined)

    def test_payment_card_redaction_uses_luhn_and_context_without_numeric_false_positives(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = make_feedback_home(Path(temporary))
            config = load_config(home)
            positive_id = record_failure(
                config,
                "billing lookup",
                reason="relevance_miss",
                collection=None,
                origin="explicit",
                expected_path="payment card 4111111111111111",
                notes="카드번호 4111 1111 1111 1111",
            )
            positive = show_failure(config, positive_id)
            self.assertIn("payment_card", positive["expected_path"])
            self.assertIn("payment_card", positive["notes"])

            safe_cases = [
                "paper identifier 4111111111111111",
                "카드번호 4111111111111112",
                "experiment 1234-5678-9012-3456",
            ]
            for query in safe_cases:
                failure_id = record_failure(
                    config,
                    query,
                    reason="relevance_miss",
                    collection=None,
                    origin="explicit",
                    notes="numeric false-positive check",
                )
                item = show_failure(config, failure_id)
                self.assertFalse(item["secret_like"], query)
                self.assertEqual(item["query"], query)

            combined = b"".join(
                path.read_bytes() for path in (home / "feedback").iterdir() if path.is_file()
            )
            self.assertNotIn(b"4111 1111 1111 1111", combined)

    def test_explicit_relevance_miss_exports_disabled_gold_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = make_feedback_home(Path(temporary))
            eval_path = home / "eval" / "gold_queries.jsonl"
            eval_path.parent.mkdir()
            eval_path.write_text("{\"id\":\"existing\"}\n", encoding="utf-8")
            before = eval_path.read_bytes()

            missing_context = run_main(
                ["--home", str(home), "feedback", "report", "pricing architecture"]
            )
            self.assertEqual(missing_context[0], 2)

            reported = run_main(
                [
                    "--home",
                    str(home),
                    "feedback",
                    "report",
                    "pricing architecture",
                    "--collection",
                    "test",
                    "--expected-path",
                    "known.md",
                    "--notes",
                    "expected this source near rank one",
                    "--json",
                ]
            )
            self.assertEqual(reported[0], 0, reported[2])
            item = json.loads(reported[1])
            self.assertEqual(item["reason"], "relevance_miss")

            config = load_config(home)
            gold = export_failures(config, export_format="gold", status="open", limit=10)
            candidate = json.loads(gold)
            self.assertFalse(candidate["enabled"])
            self.assertEqual(candidate["query"], "pricing architecture")
            self.assertEqual(candidate["relevant_paths"], ["known.md"])
            self.assertEqual(eval_path.read_bytes(), before)

            exported = run_main(
                [
                    "--home",
                    str(home),
                    "feedback",
                    "export",
                    "--format",
                    "gold",
                    "--output",
                    "observed-candidates.jsonl",
                ]
            )
            self.assertEqual(exported[0], 0, exported[2])
            output_path = Path(exported[1].strip())
            self.assertTrue(output_path.is_file())
            self.assertEqual(stat.S_IMODE(output_path.stat().st_mode), 0o600)
            self.assertEqual(eval_path.read_bytes(), before)

    def test_queue_is_independent_of_disposable_retrieval_index_and_eval_is_not_instrumented(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = make_feedback_home(Path(temporary))
            config = load_config(home)
            failure_id = record_failure(
                config,
                "missing relevant result",
                reason="relevance_miss",
                collection="test",
                origin="explicit",
                expected_path="known.md",
            )
            self.assertFalse((home / ".state").exists())

            sync_all(config)
            gold = home / "negative.jsonl"
            gold.write_text(
                json.dumps(
                    {
                        "id": "negative",
                        "query": "eval-negative-nonce-928371",
                        "expect_no_results": True,
                        "collection": "test",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            report = _evaluate(config, gold)
            self.assertEqual(report["negative_pass_rate"], 1.0)
            self.assertEqual(len(list_failures(config)), 1)

            for suffix in ("", "-wal", "-shm"):
                path = Path(str(config.database) + suffix)
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            self.assertEqual(show_failure(config, failure_id)["query"], "missing relevant result")

    def test_concurrent_duplicates_are_counted_and_queue_remains_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = make_feedback_home(Path(temporary), max_records=3)
            config = load_config(home)
            errors = []
            barrier = threading.Barrier(8)

            def worker() -> None:
                try:
                    barrier.wait(5)
                    record_zero_result(
                        config,
                        "concurrent missing query",
                        collection="test",
                        origin="mcp_search",
                    )
                except BaseException as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=worker) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)
            self.assertFalse(errors)
            concurrent = [
                item for item in list_failures(config, status="all", limit=10)
                if item["query"] == "concurrent missing query"
            ]
            self.assertEqual(concurrent[0]["count"], 8)

            for ordinal in range(5):
                record_failure(
                    config,
                    "bounded query %d" % ordinal,
                    reason="relevance_miss",
                    collection=None,
                    origin="explicit",
                    notes="bounded",
                )
            self.assertEqual(len(list_failures(config, status="all", limit=10)), 3)

    def test_cold_start_is_serialized_across_cli_processes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = make_feedback_home(Path(temporary))
            config = load_config(home)
            sync_all(config)
            processes = [
                subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "agent_memory.cli",
                        "--home",
                        str(home),
                        "search",
                        "cross-process-cold-start-nonce",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for _ in range(8)
            ]
            outcomes = [process.communicate(timeout=15) for process in processes]
            self.assertTrue(all(process.returncode == 0 for process in processes), outcomes)
            self.assertTrue(all(not stderr for _stdout, stderr in outcomes), outcomes)
            records = list_failures(config)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["count"], 8)

    def test_resolve_and_explicit_prune_apply_retention_without_new_search(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = make_feedback_home(Path(temporary))
            config = load_config(home)
            failure_id = record_failure(
                config,
                "old resolved feedback",
                reason="relevance_miss",
                collection=None,
                origin="explicit",
                notes="resolved",
            )
            resolve_failure(config, failure_id, status="resolved", notes="fixed")
            database = home / "feedback" / "query_failures.sqlite3"
            raw = sqlite3.connect(str(database))
            raw.execute(
                "UPDATE retrieval_failures SET last_seen='2000-01-01T00:00:00Z' WHERE id=?",
                (failure_id,),
            )
            raw.commit()
            raw.close()

            preview = prune_failures(config, dry_run=True)
            self.assertEqual(preview["would_remove"], 0)
            current = show_failure(config, failure_id)
            self.assertNotEqual(current["resolved_at"], "2000-01-01T00:00:00Z")

            raw = sqlite3.connect(str(database))
            raw.execute(
                "UPDATE retrieval_failures SET resolved_at='2000-01-01T00:00:00Z' WHERE id=?",
                (failure_id,),
            )
            raw.commit()
            with self.assertRaises(sqlite3.IntegrityError):
                raw.execute(
                    "UPDATE retrieval_failures SET resolved_at=NULL WHERE id=?",
                    (failure_id,),
                )
            raw.rollback()
            raw.close()

            preview = prune_failures(config, dry_run=True)
            self.assertEqual(preview["would_remove"], 1)
            self.assertIsNotNone(show_failure(config, failure_id))
            applied = prune_failures(config)
            self.assertEqual(applied["removed"], 1)
            with self.assertRaises(KeyError):
                show_failure(config, failure_id)

    def test_feedback_failure_is_best_effort_at_cli_and_mcp_user_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = make_feedback_home(Path(temporary))
            config = load_config(home)
            sync_all(config)
            with patch(
                "agent_memory.cli.record_zero_result",
                side_effect=PermissionError("feedback unavailable"),
            ):
                code, output, warning = run_main(
                    ["--home", str(home), "search", "missing-boundary-query"]
                )
            self.assertEqual(code, 0)
            self.assertEqual(output, "No current, readable source matched.\n")
            self.assertIn("could not be recorded", warning)

            request = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "memory_search",
                    "arguments": {"query": "another-missing-boundary-query"},
                },
            }
            with patch(
                "agent_memory.mcp_server.record_zero_result",
                side_effect=PermissionError("feedback unavailable"),
            ), redirect_stderr(io.StringIO()):
                response = handle_request(config, request)
            self.assertIn("result", response)
            self.assertEqual(
                response["result"]["content"][0]["text"],
                "No current, readable source matched.",
            )

    def test_feedback_schema_and_storage_boundaries_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = make_feedback_home(root)
            config = load_config(home)
            feedback = home / "feedback"
            feedback.mkdir(mode=0o700)
            database = feedback / "query_failures.sqlite3"
            raw = sqlite3.connect(str(database))
            raw.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            raw.execute("INSERT INTO metadata VALUES('schema_version', '999')")
            raw.commit()
            raw.close()
            os.chmod(database, 0o600)
            with self.assertRaises(sqlite3.DatabaseError):
                list_failures(config)
            raw = sqlite3.connect(str(database))
            self.assertEqual(
                raw.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0],
                "999",
            )
            raw.close()

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = make_feedback_home(root)
            config = load_config(home)
            feedback = home / "feedback"
            feedback.mkdir(mode=0o700)
            outside = root / "outside.sqlite3"
            outside.write_bytes(b"must remain untouched")
            (feedback / "query_failures.sqlite3").symlink_to(outside)
            with self.assertRaises(ValueError):
                list_failures(config)
            self.assertEqual(outside.read_bytes(), b"must remain untouched")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = make_feedback_home(root)
            config = load_config(home)
            feedback = home / "feedback"
            feedback.mkdir(mode=0o700)
            database = feedback / "query_failures.sqlite3"
            database.write_bytes(b"")
            os.chmod(database, 0o644)
            with self.assertRaises(PermissionError):
                list_failures(config)
            self.assertEqual(stat.S_IMODE(database.stat().st_mode), 0o644)

    def test_feedback_v1_migration_backfills_resolution_invariant(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = make_feedback_home(Path(temporary))
            config = load_config(home)
            legacy_id = record_failure(
                config,
                "legacy resolution row",
                reason="relevance_miss",
                collection=None,
                origin="explicit",
                notes="legacy",
            )
            observed_at = show_failure(config, legacy_id)["last_seen"]
            database = home / "feedback" / "query_failures.sqlite3"
            raw = sqlite3.connect(str(database))
            raw.execute("PRAGMA ignore_check_constraints=ON")
            raw.execute(
                "UPDATE retrieval_failures SET status='resolved', resolved_at=NULL, last_seen=? "
                "WHERE id=?",
                (observed_at, legacy_id),
            )
            raw.execute("UPDATE metadata SET value='1' WHERE key='schema_version'")
            raw.commit()
            raw.close()

            legacy = show_failure(config, legacy_id)
            self.assertIsNone(legacy["resolved_at"])
            record_failure(
                config,
                "migration trigger",
                reason="relevance_miss",
                collection=None,
                origin="explicit",
                notes="trigger",
            )
            migrated = show_failure(config, legacy_id)
            self.assertEqual(migrated["resolved_at"], observed_at)

            raw = sqlite3.connect(str(database))
            self.assertEqual(
                raw.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0],
                FEEDBACK_SCHEMA_VERSION,
            )
            schema_sql = raw.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='retrieval_failures'"
            ).fetchone()[0]
            self.assertIn("status IN ('resolved', 'ignored') AND resolved_at IS NOT NULL", schema_sql)
            with self.assertRaises(sqlite3.IntegrityError):
                raw.execute(
                    "UPDATE retrieval_failures SET resolved_at=NULL WHERE id=?",
                    (legacy_id,),
                )
            raw.close()

    def test_stats_and_triage_expose_repeat_without_implicitly_resolving(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = make_feedback_home(Path(temporary))
            config = load_config(home)
            self.assertEqual(feedback_stats(config)["records"], 0)
            self.assertFalse((home / "feedback").exists())
            failure_id = record_zero_result(config, "missing evidence", collection="test", origin="cli_search")
            reviewed = triage_failure(config, failure_id, category="corpus_gap", notes="Expected source is outside the configured root.")
            self.assertEqual(reviewed["status"], "open")
            self.assertFalse(reviewed["needs_triage"])
            self.assertEqual(feedback_stats(config)["open_pending_triage"], 0)
            resolve_failure(config, failure_id, status="ignored", notes="No indexed evidence was expected.")
            record_zero_result(config, "missing evidence", collection="test", origin="cli_search")
            repeated = show_failure(config, failure_id)
            self.assertEqual(repeated["status"], "open")
            self.assertEqual(repeated["category"], "corpus_gap")
            self.assertEqual(repeated["triage_notes"], reviewed["triage_notes"])
            self.assertTrue(repeated["needs_triage"])
            self.assertEqual(repeated["triaged_count"], 1)
            report = feedback_stats(config)
            self.assertEqual((report["records"], report["occurrences"], report["open_pending_triage"]), (1, 2, 1))
            self.assertNotIn("failure_rate", report)
            self.assertNotIn("missing evidence", json.dumps(report))
            self.assertEqual(len(list_failures(config, category="corpus_gap")), 1)
            self.assertEqual(list_failures(config, category="retrieval_miss"), [])

    def test_triage_rejects_empty_evidence_and_redacts_sensitive_notes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = make_feedback_home(Path(temporary))
            config = load_config(home)
            with self.assertRaises(KeyError):
                triage_failure(config, 99, category="unclassified", notes="Needs source evidence.")
            self.assertFalse((home / "feedback").exists())
            failure_id = record_zero_result(config, "unknown source", collection="test", origin="cli_search")
            for category, notes in (("other", "evidence"), ("corpus_gap", "  ")):
                with self.assertRaises(ValueError):
                    triage_failure(config, failure_id, category=category, notes=notes)
            secret = "owner@example.com"
            item = triage_failure(config, failure_id, category="unclassified", notes="Ask " + secret)
            self.assertIn("REDACTED", item["triage_notes"])
            self.assertNotIn(secret.encode(), (home / "feedback/query_failures.sqlite3").read_bytes())

    def test_recurrence_between_observation_and_triage_write_still_needs_review(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = make_feedback_home(Path(temporary))
            config = load_config(home)
            failure_id = record_zero_result(config, "recurring evidence gap", collection="test", origin="cli_search")

            def observe_then_recur(*args, **kwargs):
                observed = show_failure(*args, **kwargs)
                record_zero_result(config, "recurring evidence gap", collection="test", origin="cli_search")
                return observed

            with patch("agent_memory.feedback.show_failure", side_effect=observe_then_recur):
                item = triage_failure(config, failure_id, category="corpus_gap", notes="Reviewed the first observation.")
            self.assertEqual(item["count"], 2)
            self.assertEqual(item["triaged_count"], 1)
            self.assertTrue(item["needs_triage"])
            self.assertEqual(feedback_stats(config)["open_pending_triage"], 1)

    def test_v2_review_is_readonly_and_write_migrates_without_losing_observations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = make_feedback_home(Path(temporary))
            config = load_config(home)
            failure_id = record_failure(config, "legacy observation", reason="relevance_miss", collection="test", origin="explicit", expected_path="known.md")
            database = home / "feedback/query_failures.sqlite3"
            raw = sqlite3.connect(str(database))
            for column in ("triaged_count", "triaged_at", "triage_notes", "category"):
                raw.execute("ALTER TABLE retrieval_failures DROP COLUMN " + column)
            raw.execute("UPDATE metadata SET value='2' WHERE key='schema_version'")
            raw.commit()
            raw.close()
            before = database.read_bytes(), database.stat().st_mtime_ns
            legacy = show_failure(config, failure_id)
            self.assertTrue(legacy["needs_triage"])
            self.assertEqual(feedback_stats(config)["open_pending_triage"], 1)
            self.assertEqual(feedback_stats(config)["open_unclassified"], 1)
            self.assertEqual(len(list_failures(config, category="unclassified")), 1)
            self.assertEqual(list_failures(config, category="retrieval_miss"), [])
            self.assertIn("unclassified", export_failures(config, export_format="review", status="open", limit=10))
            self.assertEqual((database.read_bytes(), database.stat().st_mtime_ns), before)

            from agent_memory.feedback import _add_triage_columns
            def interrupted_migration(connection):  # type: ignore[no-untyped-def]
                _add_triage_columns(connection)
                raise RuntimeError("synthetic interrupted migration")
            with patch("agent_memory.feedback._add_triage_columns", interrupted_migration):
                with self.assertRaises(RuntimeError):
                    triage_failure(config, failure_id, category="retrieval_miss", notes="Expected source is indexed.")
            self.assertEqual(show_failure(config, failure_id)["fingerprint"], legacy["fingerprint"])
            self.assertEqual(feedback_stats(config)["open_pending_triage"], 1)

            triaged = triage_failure(config, failure_id, category="retrieval_miss", notes="Expected source is indexed.")
            for key in ("id", "fingerprint", "query", "expected_path", "first_seen", "last_seen", "count", "status"):
                self.assertEqual(triaged[key], legacy[key])
            raw = sqlite3.connect(str(database))
            self.assertEqual(raw.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0], FEEDBACK_SCHEMA_VERSION)
            raw.close()
            snapshot = {p.name: (p.stat().st_mtime_ns, p.read_bytes()) for p in database.parent.iterdir()}
            feedback_stats(config)
            list_failures(config, category="retrieval_miss")
            self.assertEqual(snapshot, {p.name: (p.stat().st_mtime_ns, p.read_bytes()) for p in database.parent.iterdir()})

    def test_cli_feedback_review_workflow_and_export_keep_classification(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = make_feedback_home(Path(temporary))
            prefix = ["--home", str(home), "feedback"]
            reported = run_main(prefix + ["report", "unhelpful results", "--expected-path", "known.md", "--json"])
            failure_id = json.loads(reported[1])["id"]
            triaged = run_main(prefix + ["triage", str(failure_id), "--category", "retrieval_miss", "--notes", "Known source absent after a focused retry.", "--json"])
            self.assertEqual(triaged[0], 0, triaged[2])
            self.assertFalse(json.loads(triaged[1])["needs_triage"])
            listed = run_main(prefix + ["list", "--category", "retrieval_miss", "--json"])
            self.assertEqual(json.loads(listed[1])[0]["id"], failure_id)
            stats = run_main(prefix + ["stats", "--json"])
            self.assertEqual(json.loads(stats[1])["by_category"][0]["value"], "retrieval_miss")
            plain = run_main(prefix + ["stats"])
            self.assertIn("successful searches", plain[1])
            exported = run_main(prefix + ["export", "--format", "review"])
            self.assertEqual(json.loads(exported[1])["category"], "retrieval_miss")

    def test_feedback_config_requires_strict_types_and_bounds(self) -> None:
        invalid_feedback = [
            [],
            {"auto_record_zero_results": "true"},
            {"max_records": True},
            {"max_records": 0},
            {"resolved_retention_days": "180"},
            {"resolved_retention_days": 3651},
        ]
        for ordinal, feedback in enumerate(invalid_feedback):
            with self.subTest(feedback=feedback), tempfile.TemporaryDirectory() as temporary:
                home = make_feedback_home(Path(temporary))
                payload = json.loads((home / "config.json").read_text(encoding="utf-8"))
                payload["feedback"] = feedback
                (home / "config.json").write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaises(ValueError, msg="case %d" % ordinal):
                    load_config(home)


if __name__ == "__main__":
    unittest.main()
