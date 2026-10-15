"""Explicit local document candidates; never a default channel or memory vault."""
from __future__ import annotations

import json
import hashlib
import os
import subprocess
import uuid
from dataclasses import asdict, replace
from pathlib import Path

from .config import source_by_name
from .database import connect
from .memory_policy import eligible_documents
from .search import SearchHit, StaleReferenceError, _snippet, _validate_document, fetch, format_search
from .util import atomic_create_text

MODEL_REVISION = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
WARNING = "EXPERIMENTAL semantic candidates: similarity is not evidence or confidence; unrelated queries may return results."


def _model_fingerprint(config):
    model = config.home / "runtime" / "models" / "multilingual-e5-small"
    digest = hashlib.sha256()
    for name in ("onnx/model.onnx", "tokenizer.json", "tokenizer_config.json", "config.json", "special_tokens_map.json"):
        path = model / name
        digest.update(name.encode())
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    return digest.hexdigest()


def _worker(config, request):
    python = config.home / "runtime" / "semantic" / "bin" / "python"
    model = config.home / "runtime" / "models" / "multilingual-e5-small"
    if not python.is_file() or not (model / "onnx" / "model.onnx").is_file():
        raise ValueError("Optional semantic runtime/model missing; see docs/semantic-pilot.md")
    request["model_path"] = str(model)
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    try:
        result = subprocess.run([str(python), "-I", str(Path(__file__).with_name("semantic_worker.py"))],
            input=json.dumps(request), text=True, capture_output=True, env=environment,
            timeout=1800 if request["action"] == "build" else 120)
    except subprocess.TimeoutExpired:
        raise RuntimeError("Optional semantic inference timed out; reduce the selected scope") from None
    if result.returncode:
        # No query/body is copied into logs or error output.
        raise RuntimeError("Semantic worker failed; check the optional runtime/model and input limits")
    return json.loads(result.stdout)


def _check_collection(config, collection):
    try:
        source = source_by_name(config, collection)
    except KeyError:
        raise ValueError("Unknown semantic document collection") from None
    if not source.enabled or source.kind not in {"knowledge", "research", "domain_knowledge", "project"}:
        raise ValueError("Semantic pilot requires an enabled document collection, not a memory vault")


def build(config, collection="research", path_prefixes=()):
    _check_collection(config, collection)
    clauses, parameters = [], [collection]
    for prefix in path_prefixes:
        if not prefix or prefix.startswith("/") or "\\" in prefix or ".." in prefix.split("/"):
            raise ValueError("Semantic path prefixes must be nonempty relative paths without '..'")
        escaped = prefix.rstrip("/").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        clauses.append("d.relative_path LIKE ? ESCAPE '\\'")
        parameters.append(escaped + "/%")
    scope = " AND (" + " OR ".join(clauses) + ")" if clauses else ""
    model_fingerprint = _model_fingerprint(config)
    connection = connect(config, readonly=True)
    try:
        eligible_documents(connection)
        rows = connection.execute("""SELECT c.*,d.id AS doc_id,d.collection_name,d.path,d.real_path,d.relative_path,
            d.title,d.generation,d.extension,d.chunker_version,d.source_sha256,d.size_bytes,d.mtime_ns
            FROM chunks c JOIN eligible_documents d ON d.id=c.document_id
            JOIN collections col ON col.name=d.collection_name
            WHERE d.status='ok' AND col.kind!='curated_memory' AND d.collection_name=?""" + scope +
            " ORDER BY d.id,c.ordinal LIMIT 4097", parameters).fetchall()
        if not rows or len(rows) > 4096:
            raise ValueError("Research pilot must contain 1–4096 chunks; sync its selected directories first")
        cache = {}
        records = []
        for row in rows:
            if not _validate_document(row, config, cache):
                raise StaleReferenceError("Research pilot source changed; sync before semantic build")
            records.append({"ref": "%s@%s#%d" % (row["doc_id"], row["generation"], row["ordinal"]),
                "text": row["text"], "title": row["title"], "heading": row["heading"]})
    finally:
        connection.close()
    state = config.database.parent / "semantic"
    if state.is_symlink():
        raise ValueError("Semantic state must not be a symlink")
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    vectors = state / (uuid.uuid4().hex + ".npz")
    try:
        result = _worker(config, {"action": "build", "records": records, "vectors": str(vectors)})
        # Revalidate every source after a potentially long embedding run.
        connection = connect(config, readonly=True)
        try:
            cache = {}
            if not all(_validate_document(row, config, cache) for row in rows):
                raise StaleReferenceError("Research source changed during semantic build; rebuild")
        finally:
            connection.close()
        if model_fingerprint != _model_fingerprint(config):
            raise ValueError("Semantic model changed during build")
        manifest = dict(result, embedding_version=2, model_fingerprint=model_fingerprint, vectors=vectors.name, model="intfloat/multilingual-e5-small", revision=MODEL_REVISION,
            collection=collection, path_prefixes=list(path_prefixes), chunks=len(records), experimental=True)
        temporary = state / (uuid.uuid4().hex + ".json")
        atomic_create_text(temporary, json.dumps(manifest, ensure_ascii=False), mode=0o600)
        temporary.replace(state / "manifest.json")
        # Avoid deleting a previous vector file while an experimental reader holds it.
        return {"chunks": len(records), "windows": len(result["spans"]), "max_tokens": result["max_tokens"], "experimental": True}
    except BaseException:
        vectors.unlink(missing_ok=True)
        raise


def search_semantic(config, query, limit=5, collection=None, policy=None):
    if not query.strip() or len(query) > 2000:
        raise ValueError("Semantic query must contain 1–2000 characters")
    if not 1 <= limit <= 20:
        raise ValueError("Semantic limit must be within 1–20")
    state = config.database.parent / "semantic"
    if state.is_symlink() or (state/"manifest.json").is_symlink():
        raise ValueError("Semantic state must not be a symlink")
    if not (state/"manifest.json").is_file():
        raise ValueError("Optional semantic index missing; run semantic build --experimental first")
    manifest = json.loads((state / "manifest.json").read_text())
    if manifest.get("revision") != MODEL_REVISION or not isinstance(manifest.get("collection"), str) or manifest.get("experimental") is not True:
        raise ValueError("Semantic manifest identity changed; rebuild")
    if collection is not None and collection != manifest["collection"]:
        raise ValueError("Semantic collection does not match the built index; select its collection or rebuild")
    if manifest.get("embedding_version") not in {1,2} or manifest.get("model_fingerprint") != _model_fingerprint(config):
        raise ValueError("Semantic embedding policy/model changed; rebuild")
    _check_collection(config, manifest["collection"])
    vectors = state / manifest["vectors"]
    if vectors.parent != state or vectors.suffix != ".npz" or vectors.is_symlink():
        raise ValueError("Invalid semantic vector path")
    ranked = _worker(config, {"action": "search", "query": query, "vectors": str(vectors), "runtime_versions": manifest["runtime_versions"]})
    if manifest["model_fingerprint"] != _model_fingerprint(config):
        raise ValueError("Semantic model changed during search; rebuild")
    connection = connect(config, readonly=True)
    output, seen, cache, per_document = [], set(), {}, {}
    try:
        eligible_documents(connection, policy)
        for index, score in ranked:
            span = manifest["spans"][index]
            document_id, rest = span["ref"].split("@")
            generation, ordinal = rest.split("#")
            if span["ref"] in seen or per_document.get(document_id,0) >= 2:
                continue
            row = connection.execute("""SELECT c.*,d.id AS doc_id,d.collection_name,d.path,d.real_path,d.relative_path,
                d.title,d.generation,d.extension,d.chunker_version,d.source_sha256,d.size_bytes,d.mtime_ns,col.kind,col.trust
                FROM chunks c JOIN eligible_documents d ON d.id=c.document_id JOIN collections col ON col.name=d.collection_name
                WHERE d.id=? AND c.ordinal=? AND d.status='ok' AND col.kind!='curated_memory' AND d.collection_name=?""", (document_id, int(ordinal), manifest["collection"])).fetchone()
            if not row or row["generation"] != generation or not _validate_document(row, config, cache):
                continue
            if not 0 <= span["start"] < span["end"] <= len(row["text"]):
                continue
            seen.add(span["ref"])
            per_document[document_id]=per_document.get(document_id,0)+1
            hit = SearchHit(len(output) + 1, span["ref"], document_id, generation, row["collection_name"], row["kind"],
                row["trust"], row["title"], row["path"], row["relative_path"], row["heading"], row["locator"],
                row["start_line"], row["end_line"], row["page"], row["token_estimate"],
                _snippet(row["text"][span["start"]:span["end"]], [], config.default_snippet_chars), round(score, 8))
            output.append(dict(hit=asdict(hit), span_start=span["start"], span_end=span["end"]))
            if len(output) == limit:
                break
    finally:
        connection.close()
    return output


def search_hits(config, query, limit=5, collection=None, policy=None):
    if not collection:
        raise ValueError("Semantic search requires an explicit collection matching the built document index")
    _check_collection(config, collection)
    return [replace(SearchHit(**result["hit"]),span_start=result["span_start"],span_end=result["span_end"])
            for result in search_semantic(config,query,limit,collection,policy)]


def format_candidates(hits, as_json=False, max_units=None):
    if as_json:
        return json.dumps(dict(warning=WARNING,results=[asdict(hit) for hit in hits]),ensure_ascii=False,indent=2)
    from .util import estimate_tokens, truncate_to_token_budget
    notice=WARNING+"\n"
    if max_units is not None and estimate_tokens(notice)>max_units:
        return truncate_to_token_budget(notice,max(0,max_units),marker="")[0]
    return notice+format_search(hits,max_units=None if max_units is None else max_units-estimate_tokens(notice))
