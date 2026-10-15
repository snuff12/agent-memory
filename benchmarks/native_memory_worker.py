"""Execute actual optional upstream packages against a private frozen corpus."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import time
import urllib.request
import uuid

ENDPOINT = "http://127.0.0.1:55961/v1"


def embeddings(texts, query=False):
    payload = json.dumps(dict(input=texts, model="e5-query" if query else "e5-passage")).encode()
    request = urllib.request.Request(ENDPOINT + "/embeddings", data=payload,
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=240) as response:
        return [row["embedding"] for row in json.load(response)["data"]]


def hipporag_backend(workspace, corpus, build):
    import numpy as np
    from hipporag import HippoRAG
    from hipporag.embedding_model.base import BaseEmbeddingModel
    from hipporag.utils.config_utils import BaseConfig

    class SharedEmbedder(BaseEmbeddingModel):
        query_instruction_mode = "shared"
        embedding_dim = 384

        def batch_encode(self, texts, **kwargs):
            scalar = isinstance(texts, str)
            result = np.asarray(embeddings([texts] if scalar else texts, bool(kwargs.get("instruction"))), dtype=np.float32)
            return result[0] if scalar else result

    state = workspace / "runs/hipporag-state"
    config = BaseConfig(save_dir=str(state), llm_name="gpt-6.1-sol", llm_base_url=ENDPOINT,
                        embedding_model_name="shared-e5-small", max_retry_attempts=1)
    engine = HippoRAG(global_config=config, embedding_model=SharedEmbedder(config),
                     index_identity="shared-e5-small-codex-cli-v1")
    if build:
        engine.index([row["text"] for row in corpus])
    text_map = {}
    for row in corpus:
        text_map.setdefault(row["text"], []).append(row["id"])

    def retrieve(query):
        result = engine.retrieve([query], num_to_retrieve=20)[0]
        return [dict(source=identifier, score=float(score), excerpt=text[:240])
                for text, score in zip(result.docs, result.doc_scores)
                for identifier in text_map.get(text, [])]
    return retrieve, engine.close


def mem0_backend(workspace, corpus, build, bm25_backfill=False):
    from mem0 import Memory
    state = workspace / "runs/mem0-state"
    state.mkdir(parents=True, exist_ok=True)
    config = dict(version="v1.1", history_db_path=str(state / "history.sqlite"),
        vector_store=dict(provider="qdrant", config=dict(collection_name="comparison", embedding_model_dims=384,
                                                        path=str(state / "vectors"))),
        llm=dict(provider="openai", config=dict(model="gpt-6.1-sol", api_key="benchmark-local", openai_base_url=ENDPOINT)),
        embedder=dict(provider="openai", config=dict(model="e5-passage", api_key="benchmark-local",
                                                    openai_base_url=ENDPOINT, embedding_dims=384)))
    engine = Memory.from_config(config)
    if build:
        completed = state / "ingested.json"
        done = set(json.loads(completed.read_text()) if completed.exists() else [])
        for row in corpus:
            if row["id"] in done:
                continue
            engine.add(row["text"], user_id="synthetic-comparison", metadata={"source_id": row["id"]})
            done.add(row["id"])
            completed.write_text(json.dumps(sorted(done)))
            print("mem0 indexed", len(done), "/", len(corpus), file=sys.stderr, flush=True)

    if bm25_backfill:
        import hashlib
        from mem0.utils.lemmatization import lemmatize_for_bm25
        before, count, offset = [], 0, None
        while True:
            points, offset = engine.vector_store.client.scroll(collection_name="comparison",limit=128,
                offset=offset,with_vectors=True,with_payload=True)
            for point in points:
                payload=dict(point.payload)
                before.append((str(point.id),payload.get("data"),payload.get("source_id")))
                payload["text_lemmatized"]=lemmatize_for_bm25(payload.get("data", ""))
                vector=point.vector[""] if isinstance(point.vector,dict) else point.vector
                engine.vector_store.update(point.id,vector=vector,payload=payload)
                count+=1
            if offset is None:
                break
        if not engine.vector_store._get_bm25_encoder():
            raise RuntimeError("BM25 backfill was requested but native sparse encoder is unavailable")
        after=[]
        for point in engine.vector_store.client.scroll(collection_name="comparison",limit=10000,with_payload=True)[0]:
            after.append((str(point.id),point.payload.get("data"),point.payload.get("source_id")))
        assert sorted(before)==sorted(after),"Native backfill changed facts or provenance"
        (state/"bm25-backfill.json").write_text(json.dumps(dict(memories=count,
            unchanged_fact_hash=hashlib.sha256(json.dumps(sorted(before),ensure_ascii=False).encode()).hexdigest())))

    def retrieve(query):
        engine.embedding_model.config.model = "e5-query"
        result = engine.search(query, filters={"user_id": "synthetic-comparison"}, limit=20)
        return [dict(source=row.get("metadata", {}).get("source_id"), score=float(row.get("score", 0)),
                     excerpt=row.get("memory", "")[:240]) for row in result["results"]]
    return retrieve, lambda: None


async def graphiti_backend(workspace, corpus, build):
    from datetime import datetime, timezone
    from graphiti_core import Graphiti
    from graphiti_core.driver.falkordb_driver import FalkorDriver
    from redislite.async_falkordb_client import AsyncFalkorDB
    from graphiti_core.embedder.client import EmbedderClient
    from graphiti_core.cross_encoder.client import CrossEncoderClient
    from graphiti_core.llm_client.config import LLMConfig
    from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient
    from graphiti_core.nodes import EpisodeType
    from graphiti_core.utils.bulk_utils import RawEpisode
    from graphiti_core.search.search_config import EdgeSearchConfig, EdgeSearchMethod, EdgeReranker, SearchConfig

    class ComparisonFalkorDriver(FalkorDriver):
        def clone(self,database):
            # Native group routing clones the concrete base class, so preserve
            # this same query-syntax correction in per-group connections too.
            if database==self._database:return self
            return type(self)(falkor_db=self.client,database=database)

        def build_fulltext_query(self,query,group_ids=None,max_query_length=128):
            # The pinned upstream sanitizer leaves standalone '_' terms, which
            # RediSearch rejects. Drop punctuation-only atoms, preserving native
            # construction/ranking and every word with a letter or digit.
            clean=" ".join(word for word in self.sanitize(query).split()
                           if any(character.isalnum() for character in word))
            return super().build_fulltext_query(clean,group_ids,max_query_length)

    class SharedEmbedder(EmbedderClient):
        query = False

        async def create(self, input_data):
            if isinstance(input_data, list):
                input_data = " ".join(input_data)
            return (await asyncio.to_thread(embeddings, [input_data], self.query))[0]

        async def create_batch(self, inputs):
            return await asyncio.to_thread(embeddings, inputs, self.query)

    class UnusedCrossEncoder(CrossEncoderClient):
        async def rank(self, query, passages):
            raise RuntimeError("This declared Graphiti arm uses native RRF, not a cross-encoder")

    state = workspace / "runs/graphiti-state"
    state.mkdir(parents=True, exist_ok=True)
    if not build and not (state/"graph.rdb").is_file():
        raise RuntimeError("Graphiti persisted database missing; do not score an empty reload")
    client = AsyncFalkorDB(dbfilename=str(state / "graph.rdb"))
    driver = ComparisonFalkorDriver(falkor_db=client, database="synthetic-comparison")
    embedder = SharedEmbedder()
    engine = Graphiti(graph_driver=driver, embedder=embedder, cross_encoder=UnusedCrossEncoder(),
        llm_client=OpenAIGenericClient(LLMConfig(model="gpt-6.1-sol", small_model="gpt-6.1-sol",
                                              api_key="benchmark-local", base_url=ENDPOINT)))
    episode_map_path = state / "episode-map.json"
    episode_map = json.loads(episode_map_path.read_text()) if episode_map_path.exists() else {}
    if build:
        await engine.build_indices_and_constraints()
        if not (state / "built.json").exists():
            episodes = [RawEpisode(name=row["id"], content=row["text"], source=EpisodeType.text,
                           source_description=row["path"], reference_time=datetime(2026,10,15,tzinfo=timezone.utc))
                        for row in corpus]
            built = await engine.add_episode_bulk(episodes, group_id="synthetic-comparison")
            # Embedded async cleanup does not reliably flush an RDB at process
            # exit. Persist before the success marker and verify on reload.
            await client.connection.save()
            episode_map = {episode.uuid: episode.name for episode in built.episodes}
            episode_map_path.write_text(json.dumps(episode_map))
            (state / "built.json").write_text(json.dumps(dict(episodes=len(episodes))))
    records,_,_=await driver.execute_query("MATCH (e:Episodic) RETURN count(e) AS episodes")
    if not episode_map or records[0]["episodes"]!=len(episode_map):
        raise RuntimeError("Native Graphiti reload does not contain the indexed episodes")
    # Native search recipe: text + vector + graph expansion, fused by RRF.
    recipe = SearchConfig(edge_config=EdgeSearchConfig(search_methods=[EdgeSearchMethod.bm25,
        EdgeSearchMethod.cosine_similarity, EdgeSearchMethod.bfs], reranker=EdgeReranker.rrf), limit=20)

    async def retrieve(query):
        embedder.query = True
        result = await engine.search_(query, config=recipe, group_ids=["synthetic-comparison"])
        return [dict(source=episode_map[episode], score=1/(i+1), excerpt=edge.fact[:240])
                for i, edge in enumerate(result.edges) for episode in edge.episodes if episode in episode_map]
    return retrieve, engine.close


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--method", choices=["hipporag", "graphiti", "mem0"], required=True)
    parser.add_argument("--no-build", action="store_true")
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--bm25-backfill",action="store_true")
    args = parser.parse_args()
    os.environ.update(OPENAI_API_KEY="benchmark-local", OPENAI_BASE_URL=ENDPOINT,
                      MEM0_TELEMETRY="false", GRAPHITI_TELEMETRY_ENABLED="false", DO_NOT_TRACK="1", SEMAPHORE_LIMIT="2",
                      HF_HUB_DISABLE_TELEMETRY="1", TOKENIZERS_PARALLELISM="false")
    os.environ["FASTEMBED_CACHE_PATH"]=str(args.workspace/"runs/fastembed-cache")
    corpus = json.loads((args.workspace / "corpus.json").read_text())
    started = time.perf_counter()
    if args.method == "graphiti":
        retrieve, close = await graphiti_backend(args.workspace, corpus, not args.no_build)
    else:
        kwargs={"bm25_backfill":True} if args.method=="mem0" and args.bm25_backfill else {}
        retrieve, close = globals()[args.method + "_backend"](args.workspace, corpus, not args.no_build,**kwargs)
    build_ms = (time.perf_counter()-started)*1000
    if not args.no_build:
        (args.workspace / ("runs/"+args.method+"-build.json")).write_text(json.dumps(dict(build_ms=build_ms)))
    try:
        if args.serve:
            print(json.dumps(dict(ready=True, build_ms=build_ms)), flush=True)
            for line in sys.stdin:
                data = json.loads(line)
                started = time.perf_counter()
                result = retrieve(data["query"])
                if asyncio.iscoroutine(result):
                    result = await result
                print(json.dumps(dict(results=result, retrieval_ms=(time.perf_counter()-started)*1000)), flush=True)
        else:
            queries = json.loads((args.workspace / "queries.json").read_text())
            result_path = args.workspace / ("runs/"+args.method+"-raw.jsonl")
            for case in queries:
                started = time.perf_counter()
                result = retrieve(case["query"])
                if asyncio.iscoroutine(result):
                    result = await result
                record = dict(id=case["id"], results=result, retrieval_ms=(time.perf_counter()-started)*1000)
                with result_path.open("a") as stream:
                    stream.write(json.dumps(record, ensure_ascii=False)+"\n")
                print(args.method, "retrieved", case["id"], file=sys.stderr, flush=True)
    finally:
        result = close()
        if asyncio.iscoroutine(result):
            await result


if __name__ == "__main__":
    asyncio.run(main())
