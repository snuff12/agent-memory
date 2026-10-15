#!/usr/bin/env python3
"""Opt-in, local semantic/graph source-use experiment; never a core backend."""
from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import statistics
import subprocess
import threading
import time

from agent_memory.bootstrap import initialize
from agent_memory.config import load_config
from agent_memory.database import connect
from agent_memory.indexer import sync_all
from agent_memory.memory_policy import MemoryPolicy, eligible_documents
from agent_memory.search import SearchHit, StaleReferenceError, _validate_document, fetch, format_search_with_visible_hits, search
from agent_memory.semantic import MODEL_REVISION, _model_fingerprint, build, search_semantic
from benchmarks.practical_variants_fixture import MAX_READS, PROTOCOL, READ_BYTES, SEARCH_BYTES, fixture


ROOT = Path(__file__).resolve().parents[1]
METHODS = ("lexical", "lexical_followup", "semantic", "hybrid", "graph", "hybrid_graph")
WORD_RE = re.compile(r"[A-Za-z_][A-Za-z_0-9]{1,79}")
QUOTED_RE = re.compile(r"`([^`\n]{2,80})`|\[([^\]\n]{2,80})\]\([^\)]+\)")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def outside(path):
    path = path.expanduser().resolve()
    if path == ROOT or ROOT in path.parents:
        raise ValueError("Experiment state must remain outside the checkout")
    return path


def entities(text):
    """Literal surface forms only: no aliases, questions, answer labels or LLM."""
    found = []
    for match in WORD_RE.finditer(text):
        value = match.group()
        if value[0].isupper() or "_" in value or re.search(r"[a-z][A-Z]", value):
            found.append((match.start(), value.casefold()))
    for match in QUOTED_RE.finditer(text):
        found.append((match.start(), next(value for value in match.groups() if value is not None).casefold()))
    return list(dict.fromkeys(value for _position, value in sorted(found)))


@dataclass(frozen=True)
class Candidate:
    hit: SearchHit
    span: tuple = ()


def distinct(candidates, limit=20):
    result, seen, per_document = [], set(), defaultdict(int)
    for candidate in candidates:
        if candidate.hit.ref not in seen and per_document[candidate.hit.document_id] < 2:
            result.append(candidate)
            seen.add(candidate.hit.ref)
            per_document[candidate.hit.document_id] += 1
        if len(result) == limit:
            break
    return result


def fuse(first, second):
    scores, candidates = defaultdict(float), {}
    for ranking in (first, second):
        for rank, candidate in enumerate(distinct(ranking), 1):
            ref = candidate.hit.ref
            scores[ref] += 1.0 / (60 + rank)
            # Lexical candidates retain their whole chunk navigation/focus.
            candidates.setdefault(ref, candidate)
    return distinct([candidates[ref] for ref in sorted(scores, key=lambda ref: (-scores[ref], ref))])


def current_rows(config, collection, policy=None):
    connection = connect(config, readonly=True)
    try:
        eligible_documents(connection, policy)
        connection.execute("BEGIN")
        rows = connection.execute("""SELECT c.*, d.id AS doc_id, d.collection_name, d.path, d.relative_path,
            d.real_path, d.title, d.generation, d.extension, d.chunker_version, d.source_sha256,
            d.size_bytes, d.mtime_ns, d.memory_status, d.memory_scope, d.memory_sensitivity,
            d.memory_entry_type, d.memory_kind, d.memory_error, d.memory_valid_from, d.memory_review_after,
            col.kind, col.trust FROM chunks c JOIN eligible_documents d ON d.id=c.document_id
            JOIN collections col ON col.name=d.collection_name
            WHERE d.collection_name=? AND d.status='ok' ORDER BY d.relative_path,c.ordinal""", (collection,)).fetchall()
        cache, valid = {}, {}
        result = []
        for row in rows:
            if row["doc_id"] not in valid:
                valid[row["doc_id"]] = _validate_document(row, config, cache)
            if valid[row["doc_id"]]:
                result.append(dict(row))
        return result
    finally:
        connection.close()


def row_candidate(row):
    return Candidate(SearchHit(
        rank=1, ref="%s@%s#%d" % (row["doc_id"], row["generation"], row["ordinal"]),
        document_id=row["doc_id"], generation=row["generation"], collection=row["collection_name"],
        kind=row["kind"], trust=row["trust"], title=row["title"], path=row["path"],
        relative_path=row["relative_path"], heading=row["heading"], locator=row["locator"],
        start_line=row["start_line"], end_line=row["end_line"], page=row["page"],
        token_estimate=row["token_estimate"], snippet=row["text"][:240].replace("\n", " "), score=0.0,
        **{key: row[key] for key in ("memory_status", "memory_scope", "memory_sensitivity", "memory_entry_type",
                                   "memory_kind", "memory_error", "memory_valid_from", "memory_review_after")},
    ))


class PassageGraph:
    def __init__(self, config, collection="research", policy=None):
        self.collection = collection
        self.passages, self.candidates = {}, {}
        for row in current_rows(config, collection, policy):
            candidate = row_candidate(row)
            self.passages[candidate.hit.ref] = entities(row["text"])
            self.candidates[candidate.hit.ref] = candidate

    def retrieve(self, config, query, seeds, policy=None):
        # Reapply eligibility and current source validation BEFORE graph walking.
        allowed = {row_candidate(row).hit.ref for row in current_rows(config, self.collection, policy)}
        links = defaultdict(set)
        for ref, mentions in self.passages.items():
            if ref not in allowed:
                continue
            for mention in mentions:
                links["p:" + ref].add("e:" + mention)
                links["e:" + mention].add("p:" + ref)
        prior = defaultdict(float)
        for rank, candidate in enumerate(seeds[:3], 1):
            if candidate.hit.ref in allowed:
                prior["p:" + candidate.hit.ref] += 1.0 / rank
        for node in links:
            if node.startswith("e:") and re.search(r"(?<!\w)" + re.escape(node[2:]) + r"(?!\w)", query, re.I):
                prior[node] += 1.0
        if not prior:
            return []
        total = sum(prior.values())
        prior = {node: value / total for node, value in prior.items()}
        rank = dict(prior)
        for _iteration in range(30):
            following = defaultdict(float, {node: 0.15 * value for node, value in prior.items()})
            dangling = 0.0
            for node, value in rank.items():
                neighbors = links.get(node, ())
                if neighbors:
                    for neighbor in neighbors:
                        following[neighbor] += 0.85 * value / len(neighbors)
                else:
                    dangling += value
            for node, value in prior.items():
                following[node] += 0.85 * dangling * value
            rank = following
        refs = sorted(allowed & set(self.candidates), key=lambda ref: (-rank.get("p:" + ref, 0.0), ref))
        return distinct([self.candidates[ref] for ref in refs if rank.get("p:" + ref, 0.0) > 0])

    def serializable(self):
        return {"passages": self.passages, "restart": 0.15, "iterations": 30}


def visible(candidates, budget):
    ranked = [replace(candidate.hit, rank=index) for index, candidate in enumerate(candidates, 1)]
    rendered, hits = format_search_with_visible_hits(ranked, max_units=budget)
    refs = {hit.ref for hit in hits}
    return [candidate for candidate in candidates if candidate.hit.ref in refs], len(rendered.encode("utf-8"))


def read_candidate(config, candidate, query, policy=None):
    if candidate.span:
        return fetch(config, candidate.hit.ref, span=candidate.span, max_tokens=READ_BYTES, policy=policy)
    return fetch(config, candidate.hit.ref, query=query, max_tokens=READ_BYTES, policy=policy)


def evidence_score(case, reads):
    found = [any(path == requirement["path"] and requirement["needle"] in text for path, text in reads)
             for requirement in case["requirements"]]
    return dict(complete=all(found) if found else None, requirements_met=found)


def evaluate(config, case, candidates, followup=False, display_limit=20):
    reads, used, searches, followup_query = [], set(), 1, None
    candidates, search_bytes = visible(candidates[:display_limit], SEARCH_BYTES // 2 if followup else SEARCH_BYTES)
    first = candidates[:1] if followup else candidates[:MAX_READS]
    for candidate in first:
        text = read_candidate(config, candidate, case["query"])
        reads.append((candidate.hit.relative_path, text))
        used.add(candidate.hit.ref)
    if followup and reads:
        # Strip engine provenance/continuation metadata before extracting names.
        body = reads[0][1].split("---\n", 1)[-1].split("\nfocus:", 1)[0].split("\npart:", 1)[0]
        names = [name for name in entities(body) if not re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", case["query"], re.I)]
        if names:
            followup_query = " ".join(names[:3])
            next_hits = [Candidate(hit) for hit in search(config, followup_query, limit=20, collection="research")]
            following, size = visible(next_hits[:display_limit], SEARCH_BYTES - search_bytes)
            search_bytes += size
            searches += 1
            candidates = following + candidates[1:]
        else:
            candidates = candidates[1:]
        for candidate in distinct(candidates):
            if candidate.hit.ref in used:
                continue
            text = read_candidate(config, candidate, followup_query or case["query"])
            reads.append((candidate.hit.relative_path, text))
            used.add(candidate.hit.ref)
            if len(reads) == MAX_READS:
                break
    sizes = [len(text.encode("utf-8")) for _path, text in reads]
    assert search_bytes <= SEARCH_BYTES and all(size <= READ_BYTES for size in sizes) and len(reads) <= MAX_READS
    return dict(evidence_score(case, reads), read_paths=[path for path, _text in reads],
                search_calls=searches, search_bytes=search_bytes, source_reads=len(reads),
                read_bytes=sum(sizes), followup_query=followup_query)


class MemorySampler:
    """Sample the runner + descendants, excluding the ps sampling process."""
    def __init__(self):
        self.label, self.peaks, self.stop = "startup", defaultdict(int), threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def sample(self):
        label = self.label
        output = subprocess.check_output(["ps", "-axo", "pid=,ppid=,rss=,comm="], text=True)
        rows = []
        for line in output.splitlines():
            fields = line.split(None, 3)
            if len(fields) == 4 and Path(fields[3]).name != "ps":
                rows.append((int(fields[0]), int(fields[1]), int(fields[2]) * 1024))
        descendants = {os.getpid()}
        for _ in range(8):
            following = descendants | {pid for pid, parent, _rss in rows if parent in descendants}
            if following == descendants:
                break
            descendants = following
        rss = sum(size for pid, _parent, size in rows if pid in descendants)
        # A worker may exit while ps runs. Do not charge a previous worker's
        # memory to the next (fast) lexical/graph phase.
        if label == self.label:
            self.peaks[label] = max(self.peaks[label], rss)

    def run(self):
        while not self.stop.is_set():
            self.sample()
            self.stop.wait(0.1)


def prepare(workspace, semantic_home, display_limit=20):
    initialize(workspace / "memory")
    config_path = workspace / "memory/config.json"
    raw = json.loads(config_path.read_text())
    raw["sources"] = [dict(raw["sources"][0], include_extensions=[".md", ".txt", ".py"]), raw["sources"][-1]]
    raw["feedback"]["auto_record_zero_results"] = False
    config_path.write_text(json.dumps(raw, indent=2) + "\n")
    config = load_config(workspace / "memory")
    documents, cases = fixture()
    for name, text in documents.items():
        target = config.home / "knowledge" / name
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        target.write_text(text)
        target.chmod(0o600)
    runtime = config.home / "runtime"
    runtime.mkdir(mode=0o700)
    (runtime / "semantic").symlink_to(semantic_home / "runtime/semantic", target_is_directory=True)
    (runtime / "models").symlink_to(semantic_home / "runtime/models", target_is_directory=True)
    frozen = dict(protocol=PROTOCOL, source_revision=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                  code_hashes={name: digest((ROOT / name).read_bytes()) for name in (
                      "benchmarks/practical_variants.py", "benchmarks/practical_variants_fixture.py", "benchmarks/PRACTICAL_VARIANTS_PROTOCOL.md",
                      "src/agent_memory/search.py", "src/agent_memory/semantic.py", "src/agent_memory/semantic_worker.py")},
                  corpus_hash=digest(json.dumps(documents, ensure_ascii=False, sort_keys=True).encode()),
                  cases_hash=digest(json.dumps(cases, ensure_ascii=False, sort_keys=True).encode()),
                  cases=cases, documents=len(documents), display_limit=display_limit)
    (workspace / "frozen.json").write_text(json.dumps(frozen, ensure_ascii=False, indent=2) + "\n")
    sync_all(config)
    return config, cases, documents, frozen


def summarize(records):
    result = {}
    for method in METHODS:
        rows = [record for record in records if record["method"] == method]
        groups = {}
        for group in sorted({record["group"] for record in rows}):
            selected = [record for record in rows if record["group"] == group]
            groups[group] = dict(cases=len(selected), complete=sum(record["complete"] is True for record in selected),
                                 source_reads=sum(record["source_reads"] for record in selected))
        times = sorted(record["latency_ms"] for record in rows)
        result[method] = dict(answerable=sum(record["complete"] is not None for record in rows),
                              complete=sum(record["complete"] is True for record in rows), groups=groups,
                              median_ms=statistics.median(times), p95_ms=times[min(len(times) - 1, int(len(times) * .95))],
                              mean_read_bytes=statistics.mean(record["read_bytes"] for record in rows),
                              mean_source_reads=statistics.mean(record["source_reads"] for record in rows))
    return result


def guard_checks(config, graph):
    def card(status="reviewed", scope="project:alpha", sensitivity="private", body="AlphaOwner maintains SharedBridge."):
        fields = dict(kind="fact", status=status, scope=scope, sensitivity=sensitivity, confidence="high", source="synthetic experiment")
        return "---\n" + "\n".join(key + ": " + json.dumps(value) for key, value in fields.items()) + "\n---\n" + body + "\n"

    vault = config.home / "vault"
    (vault / "alpha.md").write_text(card())
    (vault / "beta.md").write_text(card(scope="project:beta", body="SecretOwner maintains SharedBridge."))
    (vault / "pending.md").write_text(card(status="inbox", body="PendingOwner maintains SharedBridge."))
    (vault / "public.md").write_text(card(sensitivity="shareable", body="PublicOwner maintains SharedBridge."))
    sync_all(config, "vault")
    all_policy = MemoryPolicy(status="all")
    snapshot = PassageGraph(config, "vault", all_policy)
    unrestricted = [Candidate(hit) for hit in search(config, "SecretOwner", collection="vault", policy=all_policy)]
    assert unrestricted
    assert not snapshot.retrieve(config, "SecretOwner", unrestricted, MemoryPolicy(project="alpha"))
    shared = snapshot.retrieve(config, "SharedBridge", [], MemoryPolicy(project="alpha", shareable_only=True))
    assert shared and {row.hit.relative_path for row in shared} == {"public.md"}
    try:
        build(config, "vault")
    except ValueError as error:
        assert "not a memory vault" in str(error)
    else:
        raise AssertionError("Semantic pilot accepted curated memory")

    target = config.home / "knowledge/runbooks/INC-7842.md"
    original, stat = target.read_bytes(), target.stat()
    previous = search(config, "INC-7842", collection="research")[0]
    assert previous.relative_path == "runbooks/INC-7842.md"
    try:
        target.write_text("Modified source; previous evidence must not be returned.\n")
        assert all(row.hit.relative_path != previous.relative_path for row in graph.retrieve(config, "INC-7842", [Candidate(previous)]))
        assert all(row["hit"]["relative_path"] != previous.relative_path for row in search_semantic(config, "INC-7842", limit=20))
        try:
            fetch(config, previous.ref)
        except StaleReferenceError:
            pass
        else:
            raise AssertionError("Stale source was fetched")
    finally:
        target.write_bytes(original)
        os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))

    other = config.home / "separate-collection"
    other.mkdir(mode=0o700)
    (other / "foreign.txt").write_text("ForeignProject uses OutsideStore. This document belongs to a separate collection.\n")
    separate = replace(config.sources[0], name="other", path=other)
    expanded = replace(config, sources=tuple(config.sources) + (separate,))
    sync_all(expanded, "other")
    assert search(expanded, "ForeignProject", collection="other")
    assert all(row.hit.collection == "research" for row in graph.retrieve(expanded, "ForeignProject", []))
    assert all(row["hit"]["collection"] == "research" for row in search_semantic(expanded, "ForeignProject", limit=20))
    return dict(graph_rechecks_scope_before_traversal=True, graph_shareable_filter=True,
                semantic_rejects_curated_memory=True, stale_source_rejected=True,
                graph_and_semantic_collection_isolation=True, curated_semantic_policies_supported=False)


def run(workspace, semantic_home, display_limit=20):
    workspace = outside(workspace)
    workspace.mkdir(mode=0o700, parents=True, exist_ok=False)
    os.umask(0o077)
    config, cases, documents, frozen = prepare(workspace, semantic_home, display_limit)
    sampler = MemorySampler()
    sampler.thread.start()
    costs = {}
    try:
        sampler.label = "graph_build"
        started = time.perf_counter()
        graph = PassageGraph(config)
        costs["graph_build_ms"] = (time.perf_counter() - started) * 1000
        graph_data = json.dumps(graph.serializable(), ensure_ascii=False).encode()
        (workspace / "graph.json").write_bytes(graph_data)
        costs["graph_state_bytes"] = len(graph_data)
        sampler.label = "semantic_build"
        started = time.perf_counter()
        built = build(config, "research")
        costs["semantic_build_ms"] = (time.perf_counter() - started) * 1000
        costs["semantic_state_bytes"] = sum(path.stat().st_size for path in (config.database.parent / "semantic").iterdir())
        print("Built %d graph passages and %d embedding windows" % (len(graph.passages), built["windows"]), flush=True)
        records = []
        for index, case in enumerate(cases, 1):
            sampler.label = "lexical"
            started = time.perf_counter()
            lexical = [Candidate(hit) for hit in search(config, case["query"], limit=20, collection="research")]
            lexical_ms = (time.perf_counter() - started) * 1000
            sampler.label = "semantic"
            started = time.perf_counter()
            semantic = [Candidate(SearchHit(**row["hit"]), (row["span_start"], row["span_end"]))
                        for row in search_semantic(config, case["query"], limit=20)]
            semantic_ms = (time.perf_counter() - started) * 1000
            hybrid = fuse(lexical, semantic)
            for method in METHODS:
                sampler.label = method
                sampler.sample()
                started = time.perf_counter()
                candidates = lexical
                inherited = lexical_ms
                if method == "semantic":
                    candidates, inherited = semantic, semantic_ms
                elif method in {"hybrid", "hybrid_graph"}:
                    candidates, inherited = hybrid, lexical_ms + semantic_ms
                if method in {"graph", "hybrid_graph"}:
                    candidates = graph.retrieve(config, case["query"], candidates)
                result = evaluate(config, case, candidates, followup=method == "lexical_followup", display_limit=display_limit)
                elapsed = (time.perf_counter() - started) * 1000 + inherited
                records.append(dict(id=case["id"], group=case["group"], method=method, latency_ms=round(elapsed, 3), **result))
            print("%d/%d %s" % (index, len(cases), case["id"]), flush=True)
            (workspace / "partial.json").write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n")
        manifest = json.loads((config.database.parent / "semantic/manifest.json").read_text())
        sampler.label = "guard_checks"
        guards = guard_checks(config, graph)
        report = dict(protocol=PROTOCOL, frozen={key: value for key, value in frozen.items() if key != "cases"},
                      model=dict(name=manifest["model"], revision=MODEL_REVISION, fingerprint=_model_fingerprint(config),
                                 runtime_versions=manifest["runtime_versions"]),
                      environment=dict(system=platform.system(), machine=platform.machine(), python=platform.python_version(), cpu_count=os.cpu_count()),
                      costs=costs, sampled_process_tree_peak_rss_bytes=dict(sampler.peaks),
                      guard_checks=guards, summary=summarize(records), records=records)
        peaks = report["sampled_process_tree_peak_rss_bytes"]
        report["effective_method_peak_rss_bytes"] = {method: max(peaks.get(method, 0), peaks.get("semantic", 0)
            if method in {"semantic", "hybrid", "hybrid_graph"} else 0) for method in METHODS}
        assert all((config.home / "knowledge" / name).read_text() == text for name, text in documents.items())
        (workspace / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        return report
    finally:
        sampler.stop.set()
        sampler.thread.join(timeout=2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True, help="New private directory outside the checkout")
    parser.add_argument("--semantic-home", type=Path, required=True, help="Existing private home containing the pinned optional runtime/model")
    parser.add_argument("--display-limit", type=int, choices=(3, 20), default=20, help="3 for recommended agent use; 20 for the initial broad-display probe")
    args = parser.parse_args()
    report = run(args.workspace, args.semantic_home.expanduser().resolve(), args.display_limit)
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
