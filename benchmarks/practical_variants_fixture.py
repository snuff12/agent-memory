"""Synthetic task fixtures fixed before running semantic/graph experiments.

Answers and requirements stay outside the indexed corpus. This is a diagnostic
set, not a representative sample or a leaderboard benchmark.
"""
from pathlib import Path


PROTOCOL = "practical-retrieval-variants-v1"
SEARCH_BYTES = 4000
READ_BYTES = 1200
MAX_READS = 3


def fixture():
    documents, cases = {}, []

    def add(name, text):
        documents[name] = text + "\n"
        return name

    def task(identifier, group, query, requirements=()):
        cases.append(dict(id=identifier, group=group, query=query,
                          requirements=[dict(path=path, needle=needle) for path, needle in requirements]))

    paraphrases = [
        ("departures", "Invalidate a departing teammate's access key before closing the offboarding ticket. The deadline is the employee's final shift.", "퇴사한 직원의 API 키는 언제 폐기해야 해?", "final shift"),
        ("recovery", "Automated recovery copies are kept for 37 days before deletion. The clock starts when each copy is created.", "How far back can we restore an older snapshot?", "37 days"),
        ("payments", "A duplicate charge is prevented by an idempotency key. Preserve that key for 24 hours after the first payment attempt.", "같은 결제 요청을 다시 보내도 두 번 청구되지 않게 하는 값은 얼마나 보관하나?", "24 hours"),
        ("rollout", "A release is reverted when the failed-request ratio exceeds 2 percent for five consecutive minutes. Cancel further rollout stages first.", "배포 후 장애가 늘면 어느 기준에서 이전 상태로 되돌리나?", "five consecutive minutes"),
        ("privacy", "Remove email addresses and telephone numbers before persisting diagnostic events. The event identifier can remain for incident correlation.", "What personal details need scrubbing from logs?", "email addresses and telephone numbers"),
        ("cancellation", "After cancellation, recurring billing stops at the next renewal boundary. Already issued invoices stay payable; no new subscription period is charged.", "구독을 해지하면 다음 달에도 돈이 나가나?", "no new subscription period"),
        ("accessibility", "All checkout actions must be reachable without a mouse. A visible focus ring must follow the currently selected control.", "What should we check so customers can finish buying using only the keyboard?", "visible focus ring"),
        ("onboarding", "A new engineer receives production permissions only after completing the recovery drill and getting the duty lead's approval.", "새 개발자에게 운영 접근 권한을 주기 전에 필요한 절차는?", "recovery drill"),
        ("reporting", "Store a short evidence gap and the expected canonical path when reporting a relevance miss. Never retain an entire chat transcript in that report.", "What can be saved when a search did not find the right thing?", "expected canonical path"),
        ("cache", "Freshness expires after 90 seconds. Following a content edit, explicitly invalidate the derived response instead of waiting for its lifetime to elapse.", "내용을 고쳤는데 이전 응답이 계속 보이면 무엇을 해야 하나?", "explicitly invalidate"),
        ("references", "A source digest and extraction fingerprint bind every opaque reference to its generation. A modified original must be searched again before it is quoted.", "How do we avoid citing a passage from a file that changed?", "searched again"),
        ("charts", "수치가 범주별로 얼마나 다른지 비교할 때는 막대 그래프를 사용한다. 시간에 따른 변화에는 꺾은선 그래프를 사용한다.", "Which visual should compare amounts across categories rather than over time?", "막대 그래프"),
    ]
    for index, (_topic, text, query, answer) in enumerate(paraphrases):
        name = add("notes/note-%02d.txt" % index, text)
        task("paraphrase-%02d" % index, "paraphrase", query, [(name, answer)])

    pairs = [("Mina", "Aurora", "CockroachDB"), ("Leon", "Borealis", "PostgreSQL"),
             ("Arun", "Cedar", "MariaDB"), ("소라", "별빛", "SQLite"),
             ("Hana", "Kestrel", "DynamoDB"), ("Owen", "Orion", "FoundationDB"),
             ("Jisu", "Harbor", "TiDB"), ("Noor", "Atlas", "MongoDB")]
    for index, (owner, service, store) in enumerate(pairs):
        first = add("portfolio/record-%02d-a.txt" % index, "%s maintains %s. This record identifies the service owner." % (owner, service))
        second = add("portfolio/record-%02d-b.txt" % index, "%s persists durable records in %s. This is its production system of record." % (service, store))
        add("portfolio/record-%02d-c.txt" % index, "%s uses Redis as a disposable response cache. Cache contents are not durable records." % service)
        task("two-hop-%02d" % index, "two_hop", "Which durable database belongs to the service %s maintains?" % owner,
             [(first, service), (second, store)])

    triples = [("Iris", "Solstice", "LarkQueue", 9), ("Theo", "Nimbus", "BeaconQueue", 12),
               ("Nia", "Quartz", "FjordQueue", 18), ("태오", "온누리", "구름큐", 21)]
    for index, (owner, service, queue, days) in enumerate(triples):
        first = add("events/record-%02d-a.txt" % index, "%s owns %s. The ownership record does not specify transport settings." % (owner, service))
        second = add("events/record-%02d-b.txt" % index, "%s delivers telemetry through %s. The queue policy is maintained separately." % (service, queue))
        third = add("events/record-%02d-c.txt" % index, "%s retains events for %d days. After that interval, acknowledged events are erased." % (queue, days))
        task("three-hop-%02d" % index, "three_hop", "How many days are events retained by the queue behind the service %s owns?" % owner,
             [(first, service), (second, queue), (third, "%d days" % days)])

    literals = [
        ("code/parse_ref.py", "def parse_ref(value):\n    return value.split('@', 1)\n", "Where is parse_ref defined in parse_ref.py?", "def parse_ref"),
        ("runbooks/INC-7842.md", "INC-7842: An expired certificate interrupted delivery. Renew it and retry only the unacknowledged messages.", "What caused INC-7842?", "expired certificate"),
        ("config/decision_log.md", "The approved production change window starts at 22:30 UTC. Follow the rollback checklist if validation fails.", "Read decision_log.md for the approved change window", "22:30 UTC"),
        ("code/cache_policy.py", "CACHE_TTL_SECONDS = 90\n# Invalidate this cache on content edits.\n", "What is CACHE_TTL_SECONDS in cache_policy.py?", "CACHE_TTL_SECONDS = 90"),
        ("runbooks/ERR-5901.md", "ERR-5901 means the read reference is stale. Search the canonical source again before opening another section.", "What should we do for ERR-5901?", "Search the canonical source again"),
        ("config/release-v3.md", "Release v3 requires two reviewers. Release v2 required one reviewer and is no longer the active process.", "How many reviewers does release-v3.md require?", "two reviewers"),
        ("code/receipt_writer.py", "def commitReceipt(payload):\n    return durable_write(payload)\n", "Which operation does commitReceipt call?", "durable_write(payload)"),
        ("config/권한정책.md", "운영 환경의 쓰기 권한은 작업 종료 후 30분 안에 회수한다. 읽기 전용 권한은 별도 검토한다.", "권한정책.md에서 운영 쓰기 권한 회수 시점을 찾아줘", "30분"),
    ]
    for index, (path, text, query, answer) in enumerate(literals):
        add(path, text)
        task("literal-%02d" % index, "literal", query, [(path, answer)])

    ambiguities = [
        ("Mercury service writes audit events to an append-only journal.", "Mercury is the closest planet to the Sun.", "Where does the Mercury service write audit events?", "append-only journal"),
        ("Jordan operates Aster in the payments team. Aster has a 14-day audit window.", "Jordan operates Elm in the publishing team. Elm has a 3-day audit window.", "What is the audit window for Jordan's payments service?", "14-day"),
        ("Current approved process: identity checks require two independent reviewers.", "Superseded historical process: identity checks required one independent reviewer.", "What does the current approved identity process require?", "two independent reviewers"),
        ("현재 승인된 보관 정책: 오류 진단 자료는 11일 뒤 삭제한다.", "폐기된 이전 보관 정책: 오류 진단 자료는 60일 뒤 삭제했다.", "현재 승인된 오류 진단 자료의 삭제 기한은?", "11일"),
    ]
    for index, (correct, decoy, query, answer) in enumerate(ambiguities):
        path = add("ambiguities/record-%02d-a.txt" % index, correct)
        add("ambiguities/record-%02d-b.txt" % index, decoy)
        task("ambiguous-%02d" % index, "ambiguous", query, [(path, answer)])

    for index, query in enumerate(("What is the airport parking reimbursement budget?", "회사의 치과 진료비 지원 한도는?",
                                   "Who won the local chess tournament?", "What is the password for the production database?")):
        task("absent-%02d" % index, "absent", query)

    for index in range(24):
        add("background/record-%02d.txt" % index,
            "Working note %d. Inspect service health, delivery status, storage capacity and cache usage during the weekly review. This draft records no approved policy or owner." % index)

    # Actual public code: success requires two complementary spans, not a file ID.
    source = Path(__file__).resolve().parents[1] / "src/agent_memory/search.py"
    add("public-code/search.py", source.read_text(encoding="utf-8"))
    task("real-code-definition-and-call", "public_code",
         "Please inspect the retrieval implementation and explain how a late source passage is chosen by _focus_span in search.py",
         [("public-code/search.py", "def _focus_span("), ("public-code/search.py", "focused = _focus_span")])
    task("real-code-policy-and-revalidation", "public_code",
         "Where does fetch validate the source before and after a focused read?",
         [("public-code/search.py", "def _validate_document("), ("public-code/search.py", "Canonical source changed while reading")])
    return documents, cases
