"""Offline, reviewed answer evaluation, separate from retrieval hit rates.

No answering model or judge is invoked. Reviewers assess semantics; the harness
checks provenance, exact citations, review binding and the explicit rubric.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import __version__
from .evaluation import implementation_fingerprint, requirements
from .util import atomic_create_text, utc_now


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _cases(path):
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("Evaluation rows must be objects")
            if row.get("enabled", True) is not False:
                rows.append(row)
    return rows


def _by_id(rows, label):
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("%s must be a list of case objects" % label)
    result = {}
    for row in rows:
        identity = row.get("id")
        if not isinstance(identity, str) or not identity.strip() or identity in result:
            raise ValueError("%s must have unique nonempty case IDs" % label)
        result[identity] = row
    return result


def write_evidence_packet(fixture, destination, cases):
    _by_id(cases, "Evidence packets")
    if not cases:
        raise ValueError("Cannot export an empty evidence suite")
    packet = {"version": 1, "engine_version": __version__, "created_at": utc_now(),
              "engine_sha256": implementation_fingerprint(),
              "fixture_sha256": hashlib.sha256(fixture.read_bytes()).hexdigest(), "cases": cases}
    # Gold requirements and answer rubrics are intentionally absent.
    atomic_create_text(destination, json.dumps(packet, ensure_ascii=False, indent=2) + "\n", mode=0o600)


def _nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def _delivery(case, packet):
    paths, evidence, read_limit, read_bytes = requirements(case)
    reads, visible = packet["evidence"], packet["visible_results"]
    if not isinstance(reads, list) or not isinstance(visible, list):
        raise ValueError("Evidence and visible results must be lists")
    if (not isinstance(packet.get("search"), str)
            or any(not isinstance(item, dict) or any(not _nonempty(item.get(key))
                   for key in ("ref", "path", "relative_path", "collection")) for item in reads + visible)):
        raise ValueError("Evidence results require string refs, paths and collections")
    expected_policy = {"memory_status": case.get("memory_status", "reviewed"),
                       "project": case.get("project"), "domain": case.get("domain"),
                       "shareable_only": case.get("shareable_only", False)}
    if packet["query"] != case["query"] or packet["policy"] != expected_policy:
        raise ValueError("Evidence query/policy does not match the fixture")
    if len(reads) > read_limit or len({item["ref"] for item in reads}) != len(reads):
        raise ValueError("Evidence packet violates the read limit or repeats refs")
    operations, read_errors = packet.get("read_operations"), packet.get("read_errors")
    if (type(operations) is not int or not isinstance(read_errors, list)
            or not 0 <= operations <= read_limit or operations != len(reads) + len(read_errors)):
        raise ValueError("Evidence packet must account for every attempted read")
    for item in reads:
        if (not _nonempty(item.get("ref")) or not isinstance(item.get("excerpt"), str)
                or not isinstance(item.get("rendered"), str)
                or item["excerpt"] not in item["rendered"]
                or len(item["rendered"].encode("utf-8")) > read_bytes
                or not any(all(item[key] == hit[key] for key in ("ref", "path", "relative_path")) for hit in visible)):
            raise ValueError("Invalid delivered excerpt or byte budget")
    def matches(item, path):
        return path in (item["path"], item["relative_path"])
    missing_paths = [path for path in paths if not any(matches(item, path) for item in visible)]
    missing_evidence = [i for i, check in enumerate(evidence) if not any(
        matches(item, check["path"]) and check["text"] in item["excerpt"] for item in reads)]
    contamination = [item["path"] for item in visible if item.get("collection") in case.get("forbidden_collections", [])
                     or any(path in item["path"] or path in item["relative_path"] for path in case.get("forbidden_paths", []))]
    relevant = case.get("relevant_paths", [])
    if case.get("expect_no_results", False):
        retrieval_passed = not visible
    elif relevant:
        retrieval_passed = any(path in item["path"] or path in item["relative_path"]
                               for path in relevant for item in visible)
    else:
        retrieval_passed = bool(visible)
    budget_violation = len(packet["search"].encode("utf-8")) > min(4000, case.get("max_search_units", 4000))
    return {"passed": retrieval_passed and not missing_paths and not missing_evidence and not contamination and not budget_violation,
            "retrieval_passed": retrieval_passed, "contamination": contamination, "budget_violation": budget_violation,
            "read_operations": operations, "read_errors": read_errors,
            "missing_required_paths": missing_paths, "missing_evidence_indexes": missing_evidence}


def evaluate_answers(fixture: Path, evidence_path: Path, answers_path: Path, reviews_path: Path):
    cases = _by_id(_cases(fixture), "Fixtures")
    packet = json.loads(evidence_path.read_text(encoding="utf-8"))
    if (not cases or not isinstance(packet, dict) or packet.get("version") != 1
            or not _nonempty(packet.get("engine_version"))
            or not _nonempty(packet.get("engine_sha256"))
            or packet.get("fixture_sha256") != hashlib.sha256(fixture.read_bytes()).hexdigest()):
        raise ValueError("Unsupported evidence packet, empty suite or changed fixture")
    packets = _by_id(packet["cases"], "Evidence")
    answers = _by_id(_cases(answers_path), "Answers")
    reviews = _by_id(_cases(reviews_path), "Reviews")
    if set(packets) != set(cases) or set(answers) - set(cases) or set(reviews) - set(cases):
        raise ValueError("Evidence/answer/review case IDs do not match the fixture")
    packet_hash = digest(packet)
    details = []
    for identity, case in cases.items():
        rubric = case.get("answer_rubric")
        version = case.get("answer_rubric_version")
        if (not isinstance(rubric, dict) or not rubric or not _nonempty(version)
                or any(not _nonempty(key) or not _nonempty(value) for key, value in rubric.items())):
            raise ValueError("Answer cases require a versioned, nonempty reviewed rubric")
        delivered = packets[identity]
        delivery = _delivery(case, delivered)
        answer, review = answers.get(identity), reviews.get(identity)
        errors, invalid_citations, failed_criteria = [], [], []
        if answer is None:
            errors.append("answer_missing")
        else:
            for key in ("answer", "model", "prompt_version"):
                if not _nonempty(answer.get(key)):
                    errors.append(key + "_missing")
            if answer.get("packet_sha256") != packet_hash:
                errors.append("answer_packet_mismatch")
            citations = answer.get("citations")
            if not isinstance(citations, list) or (not citations and delivered["evidence"]):
                errors.append("citations_missing")
            else:
                for i, citation in enumerate(citations):
                    if (not isinstance(citation, dict) or not _nonempty(citation.get("quote"))
                            or not any(citation.get("ref") == item["ref"] and citation["quote"] in item["excerpt"]
                                       for item in delivered["evidence"])):
                        invalid_citations.append(i)
        if review is None:
            errors.append("review_missing")
        else:
            if not _nonempty(review.get("reviewer")) or not _nonempty(review.get("notes")):
                errors.append("review_provenance_missing")
            if (review.get("packet_sha256") != packet_hash or answer is None
                    or review.get("answer_sha256") != digest(answer)):
                errors.append("review_binding_mismatch")
            verdicts = review.get("verdicts")
            if (review.get("rubric_version") != version or not isinstance(verdicts, dict)
                    or set(verdicts) != set(rubric) or any(type(value) is not bool for value in verdicts.values())):
                errors.append("review_rubric_mismatch")
            else:
                failed_criteria = [key for key, value in verdicts.items() if not value]
        interpretation_passed = not errors and not invalid_citations and not failed_criteria
        details.append({"id": identity, "delivery": delivery,
                        "interpretation_passed": interpretation_passed, "reviewed": review is not None,
                        "errors": errors, "invalid_citation_indexes": invalid_citations,
                        "failed_criteria": failed_criteria,
                        "model": answer.get("model") if answer else None,
                        "prompt_version": answer.get("prompt_version") if answer else None,
                        "rubric_version": version, "reviewer": review.get("reviewer") if review else None,
                        "review_notes": review.get("notes") if review else None,
                        "answer": answer.get("answer") if answer else None,
                        "citations": answer.get("citations") if answer else None,
                        "verdicts": review.get("verdicts") if review else None,
                        "answer_sha256": digest(answer) if answer else None})
    delivery_passes = sum(item["delivery"]["passed"] for item in details)
    interpretation_passes = sum(item["interpretation_passed"] for item in details)
    return {"version": 1, "evaluation_layer": "reviewed_answer_interpretation",
            "engine_version": packet["engine_version"], "engine_sha256": packet["engine_sha256"],
            "fixture_sha256": packet["fixture_sha256"],
            "packet_sha256": packet_hash, "cases": len(details), "delivery_passes": delivery_passes,
            "interpretation_passes": interpretation_passes,
            "unreviewed": sum(not item["reviewed"] for item in details),
            "invalid_citations": sum(len(item["invalid_citation_indexes"]) for item in details),
            "passed": delivery_passes == interpretation_passes == len(details), "details": details}
