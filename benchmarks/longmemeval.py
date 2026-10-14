#!/usr/bin/env python3
"""Evaluate session retrieval on pinned LongMemEval-S, without an LLM."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import sqlite3
import statistics
import sys
import time
from typing import Any, Iterable
import urllib.request

from agent_memory.bootstrap import initialize
from agent_memory.config import load_config
from agent_memory.database import connect
from agent_memory.indexer import sync_all
from agent_memory.search import _fts_query, _fts_terms, format_search_with_visible_hits, search


DATASET_REVISION = "98d7416c24c778c2fee6e6f3006e7a073259d48f"
DATASET_FILENAME = "longmemeval_s_cleaned.json"
DATASET_SHA256 = "d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442"
DATASET_URL = (
    "https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/"
    + DATASET_REVISION + "/" + DATASET_FILENAME
)
PROTOCOL = "longmemeval-s-session-retrieval-v1"
RETRIEVAL_LIMIT = 20
KS = (1, 5, 10)
SEARCH_BUDGET = 4000
SYSTEMS = ("amem_candidates", "amem_cli_4000_bytes", "fts5_bm25")
ROOT = Path(__file__).resolve().parents[1]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def outside_checkout(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(ROOT)
    except ValueError:
        return resolved
    raise ValueError("Keep datasets, run output, and indexes outside the source checkout")


def download(destination: Path) -> Path:
    destination = outside_checkout(destination)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if destination.exists():
        if sha256(destination) != DATASET_SHA256:
            raise ValueError("Existing dataset has a different checksum; it was not replaced")
        return destination
    partial = destination.with_name(destination.name + ".partial")
    created = False
    try:
        # Exclusive creation also refuses an existing partial file or symlink.
        with partial.open("xb") as handle:
            created = True
            os.chmod(partial, 0o600)
            with urllib.request.urlopen(DATASET_URL, timeout=60) as response:
                shutil.copyfileobj(response, handle, length=1024 * 1024)
        if sha256(partial) != DATASET_SHA256:
            raise ValueError("Downloaded dataset failed SHA-256 verification")
        # Do not replace another process's completed download.
        os.link(partial, destination)
    finally:
        if created and partial.exists():
            partial.unlink()
    return destination


def validate_case(case: dict[str, Any]) -> None:
    for key in ("question_id", "question_type", "question"):
        if not isinstance(case.get(key), str) or not case[key].strip():
            raise ValueError("Missing or invalid " + key)
    ids = case.get("haystack_session_ids", [])
    sessions = case.get("haystack_sessions", [])
    dates = case.get("haystack_dates", [])
    if not ids or not len(ids) == len(sessions) == len(dates):
        raise ValueError("Session IDs, histories, and timestamps must align")
    # The official cleaned release repeats some session IDs with different
    # histories. Preserve every entry and score its published ID, as upstream does.
    if not all(isinstance(value, str) and value for value in ids):
        raise ValueError("Session IDs must be nonempty strings")
    if not all(isinstance(date, str) for date in dates):
        raise ValueError("Session dates must be strings")
    for session in sessions:
        if not isinstance(session, list) or not session:
            raise ValueError("Each session must have at least one turn")
        for turn in session:
            if (not isinstance(turn, dict) or turn.get("role") not in {"user", "assistant", "system"}
                    or not isinstance(turn.get("content"), str)):
                raise ValueError("Each turn needs a known role and text content")
    gold = case.get("answer_session_ids")
    if not isinstance(gold, list) or any(value not in ids for value in gold):
        raise ValueError("Evidence sessions must be present in the history")
    if not case["question_id"].endswith("_abs") and not gold:
        raise ValueError("Answerable questions need evidence sessions")


def render_session(turns: list[dict[str, Any]], date: str) -> str:
    """Allowlist history fields: never index a question, answer, or gold label."""
    return "Date: " + date + "\n\n" + "\n\n".join(
        turn["role"] + ": " + turn["content"] for turn in turns
    ) + "\n"


def prepare_case(case: dict[str, Any], home: Path):
    validate_case(case)
    initialize(home)
    config_path = home / "config.json"
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["sources"] = [dict(raw["sources"][0], include_extensions=[".txt"])]
    config_path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    paths = {}
    for index, (session_id, turns, date) in enumerate(zip(
        case["haystack_session_ids"], case["haystack_sessions"], case["haystack_dates"]
    )):
        name = "session-%04d.txt" % index
        target = home / "knowledge" / name
        target.write_text(render_session(turns, date), encoding="utf-8")
        target.chmod(0o600)
        paths[name] = session_id
    return load_config(home), paths


@dataclass(frozen=True)
class Candidate:
    session: str
    ordinal: int


def bm25_candidates(config, query: str, paths: dict[str, str]) -> list[Candidate]:
    """Word-only FTS5 ablation, with the same terms/chunks/document cap."""
    terms = _fts_terms(query)
    if not terms:
        return []
    connection = connect(config, readonly=True)
    try:
        rows = connection.execute(
            "SELECT d.relative_path, c.ordinal, "
            "bm25(chunks_fts, 0.1, 0.4, 0.5, 0.4, 1.0) AS relevance "
            "FROM chunks_fts JOIN chunks c ON c.id=chunks_fts.chunk_id "
            "JOIN documents d ON d.id=c.document_id "
            "WHERE chunks_fts MATCH ? AND d.status='ok' AND d.collection_name='research' "
            "ORDER BY relevance LIMIT ?",
            (_fts_query(terms), max(40, RETRIEVAL_LIMIT * 8)),
        ).fetchall()
        candidates = []
        counts: Counter[str] = Counter()
        for row in rows:
            path = row["relative_path"]
            if counts[path] >= 2:
                continue
            counts[path] += 1
            candidates.append(Candidate(paths[path], row["ordinal"]))
            if len(candidates) == RETRIEVAL_LIMIT:
                break
        return candidates
    finally:
        connection.close()


def session_order(candidates: Iterable[Candidate]) -> list[str]:
    return list(dict.fromkeys(candidate.session for candidate in candidates))


def score_sessions(ranking: list[str], evidence: set[str]) -> dict[str, float]:
    if not evidence:
        raise ValueError("Do not score abstention questions as retrieval successes")
    ranking = list(dict.fromkeys(ranking))
    metrics = {}
    for k in KS:
        found = evidence.intersection(ranking[:k])
        metrics["session_hit@%d" % k] = float(bool(found))
        metrics["session_all@%d" % k] = float(found == evidence)
        metrics["session_recall@%d" % k] = len(found) / len(evidence)
    metrics["session_mrr"] = next(
        (1.0 / rank for rank, session in enumerate(ranking, 1) if session in evidence), 0.0
    )
    return metrics


def run_case(case: dict[str, Any], home: Path) -> dict[str, Any]:
    if home.exists() or home.is_symlink():
        raise FileExistsError("Benchmark home must not already exist")
    started = time.perf_counter()
    try:
        config, paths = prepare_case(case, home)
        index_started = time.perf_counter()
        stats = sync_all(config, collection="research")
        if any(stat.errors for stat in stats) or sum(stat.indexed for stat in stats) != len(paths):
            raise RuntimeError("Incomplete benchmark ingestion")
        index_ms = (time.perf_counter() - index_started) * 1000
        query_started = time.perf_counter()
        hits = search(config, case["question"], limit=RETRIEVAL_LIMIT, collection="research")
        amem_ms = (time.perf_counter() - query_started) * 1000
        rendered, visible = format_search_with_visible_hits(hits, max_units=SEARCH_BUDGET)
        actual_bytes = len(rendered.encode("utf-8"))
        if actual_bytes > SEARCH_BUDGET:
            raise RuntimeError("Search output exceeded the CLI byte budget")
        query_started = time.perf_counter()
        baseline = bm25_candidates(config, case["question"], paths)
        bm25_ms = (time.perf_counter() - query_started) * 1000
        candidates = {
            "amem_candidates": [Candidate(paths[hit.relative_path], int(hit.ref.rsplit("#", 1)[1])) for hit in hits],
            "amem_cli_4000_bytes": [Candidate(paths[hit.relative_path], int(hit.ref.rsplit("#", 1)[1])) for hit in visible],
            "fts5_bm25": baseline,
        }
        abstention = case["question_id"].endswith("_abs")
        evidence = set(case["answer_session_ids"])
        systems = {}
        for name, values in candidates.items():
            ranking = session_order(values)
            systems[name] = {
                "sessions": ranking,
                "chunks": [{"session": value.session, "ordinal": value.ordinal} for value in values],
                "metrics": None if abstention else score_sessions(ranking, evidence),
                "query_ms": round(bm25_ms if name == "fts5_bm25" else amem_ms, 3),
            }
        return {
            "id": case["question_id"], "type": case["question_type"], "abstention": abstention,
            "evidence_sessions": sorted(evidence), "source_sessions": len(paths),
            "duplicate_session_id_entries": len(paths) - len(set(paths.values())),
            "indexed_chunks": sum(stat.chunks for stat in stats),
            "index_ms": round(index_ms, 3), "case_ms": round((time.perf_counter() - started) * 1000, 3),
            "cli_search_bytes": actual_bytes, "systems": systems,
        }
    finally:
        if home.exists():
            shutil.rmtree(home)


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, int(len(ordered) * fraction + 0.999999) - 1))]


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    answerable = [row for row in rows if not row["abstention"]]
    groups = defaultdict(list)
    for row in answerable:
        groups[row["type"]].append(row)

    def average_metrics(entries, name):
        if not entries:
            return {}
        keys = entries[0]["systems"][name]["metrics"]
        return {key: round(statistics.mean(row["systems"][name]["metrics"][key] for row in entries), 6)
                for key in keys}

    systems = {}
    for name in SYSTEMS:
        latencies = [row["systems"][name]["query_ms"] for row in rows]
        systems[name] = {
            "metrics": average_metrics(answerable, name),
            "by_type": {kind: {"cases": len(entries), "metrics": average_metrics(entries, name)}
                        for kind, entries in sorted(groups.items())},
            "query_ms_p50": round(statistics.median(latencies), 3),
            "query_ms_p95": round(percentile(latencies, 0.95), 3),
            "abstention_questions_with_candidates": sum(
                bool(row["systems"][name]["sessions"]) for row in rows if row["abstention"]
            ),
        }
    return {
        "cases": len(rows), "answerable_cases": len(answerable), "abstention_cases": len(rows) - len(answerable),
        "systems": systems, "max_cli_search_bytes": max(row["cli_search_bytes"] for row in rows),
        "mean_cli_search_bytes": round(statistics.mean(row["cli_search_bytes"] for row in rows), 1),
        "index_ms_p50": round(statistics.median(row["index_ms"] for row in rows), 3),
        "indexed_sessions": sum(row["source_sessions"] for row in rows),
        "duplicate_session_id_entries": sum(row["duplicate_session_id_entries"] for row in rows),
        "indexed_chunks": sum(row["indexed_chunks"] for row in rows),
    }


def source_fingerprint() -> str:
    digest = hashlib.sha256()
    for path in sorted((ROOT / "src" / "agent_memory").rglob("*.py")):
        digest.update(path.relative_to(ROOT).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def run(data: Path, output: Path, limit: int | None = None, allow_unpinned: bool = False) -> Path:
    data, output = outside_checkout(data), outside_checkout(output)
    digest = sha256(data)
    if digest != DATASET_SHA256 and not allow_unpinned:
        raise ValueError("Dataset checksum differs from the pinned cleaned release")
    cases = json.loads(data.read_text(encoding="utf-8"))
    if not isinstance(cases, list) or not cases:
        raise ValueError("Expected a nonempty JSON array")
    ids = []
    for case in cases:
        validate_case(case)
        ids.append(case["question_id"])
    if len(set(ids)) != len(ids):
        raise ValueError("Question IDs must be unique")
    if digest == DATASET_SHA256 and (len(cases) != 500 or sum(value.endswith("_abs") for value in ids) != 30):
        raise ValueError("Unexpected counts in pinned data")
    if limit is not None:
        if limit < 1:
            raise ValueError("limit must be positive")
        cases = cases[:limit]
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    started = time.perf_counter()
    metadata = {
        "protocol": PROTOCOL,
        "dataset": {"name": "LongMemEval-S cleaned", "revision": DATASET_REVISION,
                    "sha256": digest, "pinned": digest == DATASET_SHA256, "url": DATASET_URL},
        "full_dataset": digest == DATASET_SHA256 and len(cases) == 500,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "engine_source_sha256": source_fingerprint(), "harness_sha256": sha256(Path(__file__)),
        "environment": {"python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
                        "system": platform.system(), "release": platform.release(), "machine": platform.machine()},
        "settings": {"chunk_chars": 2400, "chunk_overlap_chars": 240, "candidate_chunks": RETRIEVAL_LIMIT,
                     "per_session_chunk_cap": 2, "session_k": list(KS), "cli_search_budget_bytes": SEARCH_BUDGET,
                     "llm": None, "query_expansion": False, "semantic_embeddings": False},
    }
    rows = []
    records = output / "cases.jsonl"
    with records.open("x", encoding="utf-8") as handle:
        records.chmod(0o600)
        for index, case in enumerate(cases, 1):
            row = run_case(case, output / "home")
            rows.append(row)
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()
            if index == 1 or index % 10 == 0 or index == len(cases):
                print("%d/%d questions; %.1fs elapsed" % (index, len(cases), time.perf_counter() - started),
                      file=sys.stderr, flush=True)
    report = {**metadata, "elapsed_seconds": round(time.perf_counter() - started, 3), **aggregate(rows)}
    report["case_records_sha256"] = sha256(records)
    destination = output / "summary.json"
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    destination.chmod(0o600)
    return destination


def main() -> int:
    cache = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "agent-memory-benchmarks" / "longmemeval"
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    fetch_parser = commands.add_parser("download", help="Download the pinned dataset outside the checkout")
    fetch_parser.add_argument("--destination", type=Path, default=cache / DATASET_FILENAME)
    run_parser = commands.add_parser("run", help="Evaluate independent histories; no model/API key needed")
    run_parser.add_argument("--data", type=Path, default=cache / DATASET_FILENAME)
    run_parser.add_argument("--output", type=Path, required=True, help="New directory outside the checkout")
    run_parser.add_argument("--limit", type=int, help="First N cases, marked as a partial run")
    run_parser.add_argument("--allow-unpinned", action="store_true", help="For custom/synthetic data; never a full official run")
    args = parser.parse_args()
    try:
        result = download(args.destination) if args.command == "download" else run(
            args.data, args.output, args.limit, args.allow_unpinned
        )
    except (ValueError, FileExistsError, FileNotFoundError) as exc:
        parser.error(str(exc))
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
