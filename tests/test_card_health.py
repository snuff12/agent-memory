"""Read-only card-health reporting and fail-closed lifecycle key spelling."""
from __future__ import annotations

import hashlib
import io
import json
import pathlib
import tempfile
import unittest
from contextlib import redirect_stdout
from importlib import metadata
from pathlib import Path
from unittest import mock

from agent_memory.cli import _doctor, main
from agent_memory.config import load_config
from agent_memory.database import connect
from agent_memory.indexer import sync_all
from agent_memory.memory_policy import METADATA_VERSION, parse_memory
from agent_memory.search import search
from tests.test_memory_policy import card
from tests.test_memory_system import make_home


MARKER = "cardtextmarker"


class CardHealthTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        source = root / "source"
        source.mkdir()
        self.home = make_home(root, source)
        self.vault = self.home / "vault"
        self.config = load_config(self.home)

    def write(self, relative, text):
        path = self.vault / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def cli(self, *arguments):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = main(["--home", str(self.home), *arguments])
        return code, stdout.getvalue()

    def test_near_miss_lifecycle_keys_fail_closed_and_other_keys_stay_accepted(self):
        for key, canonical in (("review-after", "review_after"), ("reviewAfter", "review_after"),
                               ("valid-from", "valid_from"), ("validFrom", "valid_from"), ("Valid_From", "valid_from"),
                               ("STATUS", "status"), ("supersede", "supersedes"), ("Supersedes", "supersedes"),
                               ("review after", "review_after"), ("Review After", "review_after"),
                               ("reviewafter", "review_after"), ("review.after", "review_after"),
                               ("VALIDFROM", "valid_from"), ("valid from", "valid_from")):
            with self.subTest(key=key):
                parsed = parse_memory(Path("card.md"), card(**{key: "2000-01-01"}))
                self.assertEqual(parsed["entry_type"], "invalid")
                self.assertIn("Misspelled lifecycle key: %s (use %s)" % (key, canonical), parsed["error"])
        # No spelling of an expiry is served as if retrieval enforced it.
        for key in ("valid_to", "valid-to", "validTo"):
            with self.subTest(key=key):
                parsed = parse_memory(Path("card.md"), card(**{key: "2000-01-01"}))
                self.assertEqual((parsed["entry_type"], parsed["error"]),
                                 ("invalid", "valid_to is not supported; mark the card superseded or set review_after"))
        for fields in ({"owner": "team"}, {"title": "Title", "created": "2026-10-01T09:00:00"},
                       {"supersedes": "decisions/old.md"}, {"review_note": "x"}, {"sources": "x", "tags": ["a"]}):
            with self.subTest(fields=fields):
                parsed = parse_memory(Path("card.md"), card(**fields))
                self.assertEqual((parsed["entry_type"], parsed["error"]), ("card", ""))
        for key in ("review-after", "review after"):
            self.write("semantic/typo.md", card(**{key: "2000-01-01"}))
            sync_all(self.config, "vault")
            self.assertFalse(search(self.config, "policyneedle", collection="vault"))
        self.write("semantic/typo.md", card(review_after="2999-01-01"))
        sync_all(self.config, "vault")
        self.assertTrue(search(self.config, "policyneedle", collection="vault"))

    def test_stored_parse_errors_carry_no_card_text(self):
        for text in (card().replace('source: "fixture"', "source: %s: broken" % MARKER),
                     card(valid_from=MARKER + "-not-a-date"), card(review_after=MARKER[:10])):
            with self.subTest(text=text):
                error = parse_memory(Path("card.md"), text)["error"]
                self.assertTrue(error)
                self.assertNotIn(MARKER[:10], error)
        self.assertIn("(line 6, column", parse_memory(Path("card.md"), card().replace(
            'source: "fixture"', "source: %s: broken" % MARKER))["error"])

    def test_sync_status_and_doctor_report_invalid_cards_without_failing_doctor(self):
        rendered, _ = _doctor(self.config, True)
        checks = {item["name"]: item for item in json.loads(rendered)["checks"]}
        self.assertEqual(checks["memory_cards"], {"name": "memory_cards", "ok": True, "warning": False,
                                                  "detail": "pending-first-sync"})
        self.write("semantic/good.md", card())
        self.write("semantic/naive.md", card(title=MARKER, valid_from="2026-10-01T09:00:00") + MARKER)
        self.write("semantic/typo.md", card(title=MARKER, **{"review-after": "2000-01-01"}) + MARKER)
        code, output = self.cli("sync")
        self.assertEqual(code, 0)
        self.assertIn("vault: scanned=3 indexed=3 unchanged=0 removed=0 errors=0", output)
        self.assertIn("\nvault: 2 invalid memory cards excluded; run amem doctor", output)
        self.assertNotIn("test: 0 invalid", output)
        code, output = self.cli("sync", "--json")
        payload = {item["collection"]: item for item in json.loads(output)}
        self.assertEqual((code, payload["vault"]["invalid_memory_cards"], payload["vault"]["errors"]), (0, 2, 0))
        self.assertNotIn("invalid_memory_cards", payload["test"])
        status = json.loads(self.cli("status", "--json")[1])
        self.assertEqual((status["errors"], status["invalid_memory_cards"]), (0, 2))
        self.assertIn("invalid_memory_cards: 2", self.cli("status")[1])

        rendered, ok = _doctor(self.config, True)
        report = json.loads(rendered)
        checks = {item["name"]: item for item in report["checks"]}
        self.assertTrue(checks["memory_cards"]["ok"] and checks["memory_cards"]["warning"])
        self.assertEqual(ok, all(item["ok"] for item in report["checks"] if item["name"] != "memory_cards"))
        self.assertEqual(report["warnings"], [
            {"name": "invalid_memory_card", "path": "vault/semantic/naive.md",
             "detail": "Memory timestamps must include a timezone"},
            {"name": "invalid_memory_card", "path": "vault/semantic/typo.md",
             "detail": "Misspelled lifecycle key: review-after (use review_after)"},
        ])
        code, text = self.cli("doctor")
        self.assertEqual(code, 0 if ok else 1)
        self.assertIn("[warn] memory_cards — cards=3 invalid=2 supersedes_issues=0 reviewed_past_review_after=0 (", text)
        self.assertIn("[warn] invalid_memory_card — vault/semantic/naive.md: Memory timestamps must include a timezone", text)
        for output in (rendered, text, self.cli("status")[1], self.cli("sync")[1]):
            self.assertNotIn(MARKER, output)

    def test_doctor_lints_supersedes_and_counts_overdue_reviews_read_only(self):
        self.write("decisions/old.md", card())
        self.write("decisions/new.md", card(supersedes="decisions/old.md"))
        self.write("decisions/dangling.md", card(supersedes="decisions/%s.md" % MARKER))
        shape = self.write("decisions/shape.md", card(supersedes={"path": "decisions/old.md"}))
        self.write("inbox/candidate.md", card(status="inbox", supersedes="decisions/absent.md"))
        # A superseded card is linted too.
        self.write("decisions/retired.md", card(status="superseded", supersedes="decisions/retired-%s.md" % MARKER))
        self.write("semantic/overdue.md", card(review_after="2000-01-01"))
        sync_all(self.config)
        before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in self.vault.rglob("*.md")}
        report = json.loads(_doctor(self.config, True)[0])
        self.assertEqual(report["warnings"], [
            {"name": "supersedes", "path": "vault/decisions/dangling.md", "detail": "target is missing from the vault index"},
            {"name": "supersedes", "path": "vault/decisions/new.md",
             "detail": "target vault/decisions/old.md is not marked status: superseded"},
            {"name": "supersedes", "path": "vault/decisions/retired.md", "detail": "target is missing from the vault index"},
            {"name": "supersedes", "path": "vault/decisions/shape.md",
             "detail": "must be one vault-relative path or a list of at most 8"},
        ])
        detail = {item["name"]: item for item in report["checks"]}["memory_cards"]["detail"]
        self.assertIn("cards=7 invalid=0 supersedes_issues=4 reviewed_past_review_after=1 (", detail)
        self.assertNotIn(MARKER, json.dumps(report))
        self.assertEqual(before, {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in self.vault.rglob("*.md")})

        self.write("decisions/old.md", card(status="superseded"))
        sync_all(self.config, "vault")
        report = json.loads(_doctor(self.config, True)[0])
        self.assertEqual([item["path"] for item in report["warnings"]],
                         ["vault/decisions/dangling.md", "vault/decisions/retired.md", "vault/decisions/shape.md"])
        shape.write_text(shape.read_text() + "edited after sync\n")
        report = json.loads(_doctor(self.config, True)[0])
        self.assertEqual([item["path"] for item in report["warnings"]],
                         ["vault/decisions/dangling.md", "vault/decisions/retired.md"])
        self.assertIn(" changed_since_sync_not_linted=1 ", {item["name"]: item for item in report["checks"]}["memory_cards"]["detail"])

    def test_doctor_reads_cards_only_through_the_read_gate(self):
        self.write("decisions/new.md", card(supersedes="decisions/missing.md"))
        sync_all(self.config)
        self.assertEqual(len(json.loads(_doctor(self.config, True)[0])["warnings"]), 1)
        # A source disabled in config is not read, even before the next sync.
        settings = json.loads((self.home / "config.json").read_text())
        for source in settings["sources"]:
            if source["name"] == "vault":
                source["enabled"] = False
        (self.home / "config.json").write_text(json.dumps(settings))
        report = json.loads(_doctor(load_config(self.home), True)[0])
        self.assertEqual(report["warnings"], [])
        self.assertRegex({item["name"]: item for item in report["checks"]}["memory_cards"]["detail"],
                         r" changed_since_sync_not_linted=1\b")

    def test_symlink_loop_while_reading_a_card_does_not_abort_doctor(self):
        path = self.write("decisions/a.md", card(supersedes="decisions/missing.md"))
        sync_all(self.config)
        code, _ = self.cli("doctor", "--json")
        real_resolve = pathlib.Path.resolve

        def resolve(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            # Python 3.9-3.12 raise RuntimeError, not OSError, for a symlink loop.
            if self.name == path.name:
                raise RuntimeError("Symlink loop from %r" % str(self))
            return real_resolve(self, *args, **kwargs)

        with mock.patch.object(pathlib.Path, "resolve", resolve):
            looped, output = self.cli("doctor", "--json")
        self.assertEqual(looped, code)
        report = json.loads(output)
        self.assertEqual(report["warnings"], [])
        self.assertRegex({item["name"]: item for item in report["checks"]}["memory_cards"]["detail"],
                         r" changed_since_sync_not_linted=1\b")

    def test_doctor_text_escapes_control_characters_in_paths(self):
        self.write("semantic/bad\n[ok] config — fake.md", card(**{"review-after": "2000-01-01"}))
        self.write("semantic/old\n[ok] secure_delete — forged.md", card())
        self.write("semantic/new.md", card(supersedes="semantic/old\n[ok] secure_delete — forged.md"))
        sync_all(self.config)
        report = json.loads(_doctor(self.config, True)[0])
        self.assertIn("vault/semantic/bad\n[ok] config — fake.md", [item["path"] for item in report["warnings"]])
        code, text = self.cli("doctor")
        lines = text.rstrip("\n").split("\n")
        self.assertEqual(len(lines), len(report["checks"]) + len(report["warnings"]))
        self.assertEqual(text.rstrip("\n").splitlines(), lines)
        self.assertFalse([line for line in lines if line.startswith(("[ok] config — fake", "[ok] secure_delete — forged"))])
        self.assertIn("[warn] invalid_memory_card — vault/semantic/bad\\u000a[ok] config — fake.md: ", text)

    def test_cards_indexed_by_the_previous_metadata_version_are_revalidated(self):
        self.write("semantic/typo.md", card(**{"review-after": "2000-01-01"}))
        sync_all(self.config, "vault")
        connection = connect(self.config)
        # The previous parser ignored near-miss keys and stored this row as a current reviewed card.
        connection.execute("UPDATE memory_metadata SET entry_type='card', error='', memory_status='reviewed', version=?",
                           ("1:" + metadata.version("PyYAML"),))
        connection.commit()
        connection.close()
        self.assertFalse(search(self.config, "policyneedle", collection="vault"))
        detail = {item["name"]: item for item in json.loads(_doctor(self.config, True)[0])["checks"]}["memory_cards"]["detail"]
        self.assertIn(" awaiting_validation_by_sync=1 ", detail)
        self.assertEqual(sync_all(self.config, "vault")[0].unchanged, 1)
        connection = connect(self.config, readonly=True)
        row = connection.execute("SELECT entry_type, error, version FROM memory_metadata").fetchone()
        connection.close()
        self.assertEqual(tuple(row), ("invalid", "Misspelled lifecycle key: review-after (use review_after)", METADATA_VERSION))
        self.assertFalse(search(self.config, "policyneedle", collection="vault"))


if __name__ == "__main__":
    unittest.main()
