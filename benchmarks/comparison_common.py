"""Source mapping and frozen data shared by external-system diagnostics."""
from collections import Counter
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess

from agent_memory.bootstrap import initialize
from agent_memory.config import load_config
from agent_memory.indexer import sync_all
from agent_memory.search import fetch, search
from benchmarks.practical_variants import current_rows, row_candidate, visible
from benchmarks.system_comparison_fixture import comparison_fixture

ROOT = Path(__file__).resolve().parents[1]


def sha(value):
    return hashlib.sha256(value).hexdigest()


def prepare(workspace):
    workspace = workspace.expanduser().resolve()
    if workspace == ROOT or ROOT in workspace.parents:
        raise ValueError("Run state must be outside the checkout")
    if (workspace / "frozen.json").exists():
        raise ValueError("Refusing to overwrite a frozen run")
    for name in ("logs", "inference", "runs", "empty"):
        (workspace/name).mkdir(parents=True,exist_ok=True)
    initialize(workspace / "memory")
    config_path = workspace / "memory/config.json"
    raw = json.loads(config_path.read_text())
    raw["sources"] = [dict(raw["sources"][0], include_extensions=[".md", ".txt", ".py"])]
    raw["feedback"]["auto_record_zero_results"] = False
    config_path.write_text(json.dumps(raw, indent=2) + "\n")
    config = load_config(workspace / "memory")
    documents, cases = comparison_fixture()
    for name, body in documents.items():
        target = config.home / "knowledge" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    for case in cases:
        for requirement in case["requirements"]:
            if requirement["needle"] not in documents[requirement["path"]]:
                raise ValueError("Invalid evidence requirement: " + case["id"])
    sync_all(config)
    rows = current_rows(config, "research")
    corpus = [dict(id="p%04d" % i, path=row["relative_path"], text=row["text"],
                   ref=row_candidate(row).hit.ref, ordinal=row["ordinal"], title=row["title"])
              for i, row in enumerate(rows)]
    count, selected = Counter(), []
    for case in cases[:42]:
        if case["group"] == "absent" or count[case["group"]] < 2:
            selected.append(case["id"])
        count[case["group"]] += 1
    controller_cases = selected + [case["id"] for case in cases[42:]]
    frozen = dict(protocol="external-system-comparison-v1", model="gpt-6.1-sol", reasoning="low",
        source_revision=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        documents=len(documents), chunks=len(corpus), cases=len(cases), controller_cases=controller_cases,
        corpus_hash=sha(json.dumps(documents, sort_keys=True, ensure_ascii=False).encode()),
        cases_hash=sha(json.dumps(cases, sort_keys=True, ensure_ascii=False).encode()),
        code_hashes={str(path.relative_to(ROOT)): sha(path.read_bytes()) for path in sorted((ROOT/"benchmarks").glob("*comparison*.py"))})
    (workspace / "corpus.json").write_text(json.dumps(corpus, ensure_ascii=False), encoding="utf-8")
    (workspace / "cases.json").write_text(json.dumps(cases, ensure_ascii=False, indent=2), encoding="utf-8")
    (workspace / "queries.json").write_text(json.dumps([
        {key:case[key] for key in ("id","query")} for case in cases],ensure_ascii=False),encoding="utf-8")
    (workspace / "frozen.json").write_text(json.dumps(frozen, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    return frozen


class Sources:
    def __init__(self, workspace):
        self.workspace = Path(workspace).resolve()
        self.config = load_config(self.workspace / "memory")
        self.rows = current_rows(self.config, "research")
        self.by_id = {"p%04d" % i: row_candidate(row) for i, row in enumerate(self.rows)}
        self.by_ref = {candidate.hit.ref: candidate for candidate in self.by_id.values()}

    def candidates(self, native):
        result, seen = [], set()
        for record in native:
            source = record.get("source")
            if source not in self.by_id or source in seen:
                continue
            seen.add(source)
            candidate = self.by_id[source]
            hit = replace(candidate.hit, score=float(record.get("score", 0)),
                          snippet=record.get("excerpt", candidate.hit.snippet)[:240].replace("\n", " "))
            result.append(replace(candidate, hit=hit))
            if len(result) == 20:
                break
        return result

    def lexical(self, query):
        return [self.by_ref[hit.ref] for hit in search(self.config, query, collection="research", limit=20)]

    def read(self, ref, query=""):
        if ref not in self.by_ref:
            raise ValueError("Unknown ref")
        return fetch(self.config, ref, query=query or None, max_tokens=1200)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.workspace), indent=2))
