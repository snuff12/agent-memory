"""Additional public-source tasks; expected evidence is never indexed."""
from pathlib import Path

from benchmarks.practical_variants_fixture import fixture


def comparison_fixture():
    documents, cases = fixture()
    root = Path(__file__).resolve().parents[1]
    files = {"followup": "src/agent_memory/followup.py",
             "chunking": "src/agent_memory/python_chunking.py",
             "policy": "src/agent_memory/memory_policy.py",
             "contract": "docs/agent-contract.md"}
    paths = {}
    for name, relative in files.items():
        path = "public-source/" + relative
        documents[path] = (root / relative).read_text(encoding="utf-8")
        paths[name] = path
    new = [
        ("bridge-body", "Does a name appearing only in a filename authorize follow-up search? Find the implementation check.", "followup", ["row[\"text\"]", "if literal is None:"]),
        ("bridge-revalidation", "후속검색 도중 원문이나 검토 상태가 달라졌을 때 기존 연결어를 계속 써도 되는지 구현을 확인해줘.", "followup", ["if _anchor(config, query, ref, policy) != anchor:", "Follow-up source changed while searching"]),
        ("bridge-complement", "Does followup_search remove every result from the original file, or only the anchor chunk?", "followup", ["hit.ref != anchor.ref"]),
        ("bridge-scope", "원문에서 가져온 이름으로 후속 검색할 때 다른 컬렉션을 지정해도 되는가?", "followup", ["collection != anchor.collection", "Follow-up collection must match"]),
        ("syntax-fallback", "If Python parsing fails while chunking, can ordinary text still be indexed? Find the fallback.", "chunking", ["except (SyntaxError, ValueError, RecursionError):", "return _bounded(chunk_text"]),
        ("decorator-boundary", "Python 함수를 나눌 때 데코레이터가 함수 본문과 분리되는지 확인해줘.", "chunking", ["node.decorator_list"]),
        ("metadata-aliases", "Can reviewed memory metadata use YAML aliases or duplicate keys? Show both checks.", "policy", ["YAML aliases are not allowed", "Memory metadata keys must be unique strings"]),
        ("cross-call-budget", "Does the CLI enforce the total number of source reads across multiple calls, or must the agent track that?", "contract", ["stateless CLI does not enforce a cross-call budget"]),
    ]
    for identifier, query, name, needles in new:
        cases.append(dict(id="new-public-" + identifier, group="new_public_source", query=query,
                          requirements=[dict(path=paths[name], needle=needle) for needle in needles]))
    return documents, cases
