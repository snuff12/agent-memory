"""Opt-in evidence checks for bounded, source-grounded retrieval evaluations."""
from __future__ import annotations

from .search import StaleReferenceError, _fetch_with_excerpt


def requirements(case: dict) -> tuple:
    paths = case.get("required_paths", [])
    evidence = case.get("required_evidence", [])
    if (not isinstance(paths, list) or len(paths) > 20
            or any(not isinstance(path, str) or not path.strip() for path in paths)):
        raise ValueError("required_paths must be a list of at most 20 nonempty exact paths")
    if (not isinstance(evidence, list) or len(evidence) > 20
            or any(not isinstance(item, dict) or set(item) != {"path", "text"}
                   or any(not isinstance(item[key], str) or not item[key].strip() for key in ("path", "text"))
                   for item in evidence)):
        raise ValueError("required_evidence must contain at most 20 {path, text} literal checks")
    read_limit, read_bytes = case.get("read_limit", 3), case.get("read_max_bytes", 1200)
    for value, label, low, high in ((read_limit, "read_limit", 1, 3), (read_bytes, "read_max_bytes", 128, 8000)):
        if type(value) is not int or not low <= value <= high:
            raise ValueError("%s must be an integer within [%d, %d]" % (label, low, high))
    if case.get("expect_no_results", False) and (paths or evidence):
        raise ValueError("Negative eval cases cannot require paths or evidence")
    return paths, evidence, read_limit, read_bytes


def exact_path(hit, path: str) -> bool:
    return path in (hit.path, hit.relative_path)


def check_requirements(config, query, hits, policy, parsed: tuple) -> dict:
    paths, evidence, read_limit, read_bytes = parsed
    missing_paths = [path for path in paths if not any(exact_path(hit, path) for hit in hits)]
    found, seen = set(), set()
    operations = units = 0
    errors = []
    if evidence:
        # Use returned ranking and the original question, never the expected
        # answer/path to select or focus reads. Output-hidden hits earn no credit.
        for hit in hits:
            if hit.ref in seen:
                continue
            if operations >= read_limit:
                break
            seen.add(hit.ref)
            operations += 1
            try:
                rendered, excerpt = _fetch_with_excerpt(
                    config, hit.ref, max_tokens=read_bytes, query=query, policy=policy)
            except (StaleReferenceError, ValueError, OSError) as exc:
                errors.append({"ref": hit.ref, "error": type(exc).__name__})
                continue
            units += len(rendered.encode("utf-8"))
            found.update(index for index, item in enumerate(evidence)
                         if exact_path(hit, item["path"]) and item["text"] in excerpt)
    missing_evidence = [index for index in range(len(evidence)) if index not in found]
    return {
        "requirements_met": not missing_paths and not missing_evidence,
        "missing_required_paths": missing_paths,
        "missing_evidence_indexes": missing_evidence,
        "read_operations": operations,
        "read_units": units,
        "read_errors": errors,
    }
