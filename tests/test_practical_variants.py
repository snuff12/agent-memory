from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from agent_memory.config import load_config
from agent_memory.indexer import sync_all
from agent_memory.memory_policy import MemoryPolicy
from agent_memory.search import fetch, search
from benchmarks.practical_variants import Candidate, PassageGraph, evidence_score, prepare
from tests.test_memory_policy import card
from tests.test_memory_system import make_home


class PracticalVariantIntegrityTests(unittest.TestCase):
    def test_entity_graph_reads_connected_evidence_without_answer_labels(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "owner.txt").write_text("Mina maintains Aurora.")
            (source / "store.txt").write_text("Aurora persists in CockroachDB.")
            (source / "other.txt").write_text("Leon maintains Borealis.")
            config = load_config(make_home(root, source))
            sync_all(config)
            graph = PassageGraph(config, "test")
            hits = graph.retrieve(config, "Mina database", [])
            self.assertEqual({candidate.hit.relative_path for candidate in hits}, {"owner.txt", "store.txt"})
            self.assertTrue(any("CockroachDB" in fetch(config, candidate.hit.ref) for candidate in hits))
            self.assertNotIn("other.txt", {candidate.hit.relative_path for candidate in hits})

    def test_cached_graph_revalidates_source_before_propagation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            owner = source / "owner.txt"
            owner.write_text("Mina maintains Aurora.")
            (source / "store.txt").write_text("Aurora persists in CockroachDB.")
            config = load_config(make_home(root, source))
            sync_all(config)
            graph = PassageGraph(config, "test")
            previous = [Candidate(hit) for hit in search(config, "Mina", collection="test")]
            owner.write_text("Mina no longer owns that service.")
            self.assertEqual(graph.retrieve(config, "Mina", previous), [])

    def test_policy_is_applied_before_walking_shared_entities(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            home = make_home(root, source)
            (home / "vault/alpha.md").write_text(card(scope="project:alpha", sensitivity="shareable") + "PublicOwner maintains SharedBridge.")
            (home / "vault/beta.md").write_text(card(scope="project:beta") + "SecretOwner maintains SharedBridge.")
            (home / "vault/pending.md").write_text(card(scope="project:alpha", status="inbox") + "PendingOwner maintains SharedBridge.")
            config = load_config(home)
            sync_all(config)
            unrestricted = MemoryPolicy(status="all")
            graph = PassageGraph(config, "vault", unrestricted)
            seeds = [Candidate(hit) for hit in search(config, "SecretOwner", collection="vault", policy=unrestricted)]
            self.assertTrue(seeds)
            self.assertFalse(graph.retrieve(config, "SecretOwner", seeds, MemoryPolicy(project="alpha")))
            hits = graph.retrieve(config, "SharedBridge", [], MemoryPolicy(project="alpha", shareable_only=True))
            self.assertEqual({candidate.hit.relative_path for candidate in hits}, {"alpha.md"})

    def test_file_id_or_evidence_in_another_file_is_not_completion(self):
        case = {"requirements": [{"path": "policy.txt", "needle": "37 days"}]}
        self.assertFalse(evidence_score(case, [("policy.txt", "An introduction only.")])["complete"])
        self.assertFalse(evidence_score(case, [("other.txt", "37 days")])["complete"])
        self.assertTrue(evidence_score(case, [("policy.txt", "Retention: 37 days.")])["complete"])
        self.assertIsNone(evidence_score({"requirements": []}, [])["complete"])

    def test_questions_and_requirements_are_not_indexed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, cases, documents, frozen = prepare(root, root / "unused-runtime-home")
            self.assertEqual(len(cases), 42)
            self.assertEqual(frozen["documents"], len(documents))
            indexed_text = "\n".join(path.read_text() for path in (config.home / "knowledge").rglob("*") if path.is_file())
            self.assertNotIn('"requirements"', indexed_text)
            for case in cases:
                self.assertNotIn(case["query"], indexed_text)
            self.assertTrue((root / "frozen.json").is_file())
            self.assertFalse((config.home / "knowledge/frozen.json").exists())


if __name__ == "__main__":
    unittest.main()
