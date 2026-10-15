"""Measure actual SCIP definition/reference coverage, not natural-language QA."""
import json
from pathlib import Path
import re
import sys
from collections import defaultdict


class ScipNavigation:
    """Exact function names only; native SCIP locations precede lexical fallback.

    This is an integration prototype, not the Sourcegraph/Cody product. Neither
    expected paths nor question categories are available to the retriever.
    """
    def __init__(self,sources):
        self.sources=sources
        self.index=json.loads((sources.workspace/"runs/scip-index.json").read_text())

    def search(self,query):
        from agent_memory.search import QUERY_TERM_RE
        # A filename/path is one navigation atom. Do not turn example.py into
        # a request for a function named example. Sentence punctuation is not
        # part of a function identifier.
        atoms={match.group(0).strip(".") for match in QUERY_TERM_RE.finditer(query)}
        locations=[]
        for document in self.index["documents"]:
            for occurrence in document["occurrences"]:
                match=re.search(r"/([^/()]+)\(\)\.$",occurrence["symbol"])
                if match and match.group(1) in atoms:
                    locations.append((not bool(occurrence["symbol_roles"]&1),document["relative_path"],occurrence["range"][0]+1))
        candidates=[]
        for _reference,path,line in sorted(locations):
            matches=[(key,row) for key,row in zip(self.sources.by_id,self.sources.rows)
                     if row["relative_path"]==path and row["start_line"]<=line<=row["end_line"]]
            if matches:
                key,_row=min(matches,key=lambda item:abs((item[1]["start_line"]+item[1]["end_line"])/2-line))
                candidates.append(self.sources.by_id[key])
        from benchmarks.practical_variants import distinct
        return distinct(candidates+self.sources.lexical(query))


def main():
    workspace=Path(sys.argv[1]).resolve()
    index=json.loads((workspace/"runs/scip-index.json").read_text())
    symbols=defaultdict(lambda:dict(definitions=[],references=[]))
    for document in index["documents"]:
        for occurrence in document["occurrences"]:
            symbol=occurrence["symbol"]
            # Ignore parameter/local-variable symbols; retain actual function IDs.
            if not symbol.endswith("()."):
                continue
            target=symbols[symbol]
            location=dict(path=document["relative_path"],line=occurrence["range"][0]+1)
            target["definitions" if occurrence["symbol_roles"]&1 else "references"].append(location)
    local={symbol:value for symbol,value in symbols.items() if value["definitions"]}
    report=dict(method="actual Sourcegraph SCIP Python indexer",documents=len(index["documents"]),
        defined_functions=len(local),functions_with_references=sum(bool(value["references"]) for value in local.values()),
        natural_language_qa_score=None,
        limitation="Structural coverage only; no claim of passing the source-use QA benchmark.",
        functions=local)
    (workspace/"runs/scip-coverage.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n")
    print(json.dumps({key:value for key,value in report.items() if key!="functions"},indent=2))


if __name__=="__main__":
    main()
