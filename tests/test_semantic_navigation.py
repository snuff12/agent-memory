import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_memory.config import load_config
from agent_memory.indexer import sync_all
from agent_memory.search import fetch
from agent_memory.semantic import build, search_hits, format_candidates
from agent_memory.semantic_worker import windows, MAX_BODY_BYTES
from tests.test_memory_system import make_home
from tests.test_practical_retrieval import call_tool


class CharacterTokenizer:
    def encode(self,text,add_special_tokens=True):
        return SimpleNamespace(ids=list(range(len(text)+(2 if add_special_tokens else 0))),
                               offsets=[(i,i+1) for i in range(len(text))])


class SemanticNavigationTests(unittest.TestCase):
    def test_windows_preserve_every_character_under_token_and_source_byte_caps(self):
        body=("가나다라마바사🙂 function_body\n"*150)
        record=dict(text=body,title="아주 긴 제목"*100,heading="source section")
        chunks=list(windows(CharacterTokenizer(),record))
        covered=set()
        for text,start,end,tokens in chunks:
            self.assertLessEqual(tokens,512)
            self.assertLessEqual(len(body[start:end].encode()),MAX_BODY_BYTES)
            self.assertTrue(text.endswith(body[start:end]))
            covered.update(range(start,end))
        self.assertTrue(all(char.isspace() or i in covered for i,char in enumerate(body)))
        self.assertGreater(len(chunks),1)

    def test_opt_in_returns_complementary_chunks_and_exact_source_spans(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            source=root/"source";source.mkdir()
            (source/"flow.txt").write_text("Source paragraph with unchanged evidence.\n"*30)
            (source/"other.txt").write_text("Different source with independent evidence.\n"*10)
            config=load_config(make_home(root,source));sync_all(config)
            saved=[]
            def worker(configuration,request):
                if request["action"]=="build":
                    spans=[]
                    for record in request["records"]:
                        # Two windows per chunk exercise deduplication without
                        # hiding a complementary second chunk from the file.
                        for start,end in [(0,50),(50,len(record["text"]))]:
                            if end>start:spans.append(dict(ref=record["ref"],start=start,end=end,tokens=50))
                    saved.extend(spans)
                    Path(request["vectors"]).write_bytes(b"synthetic vectors")
                    return dict(spans=spans,max_tokens=50,runtime_versions={})
                return [[index,1-index/1000] for index in range(len(saved))]
            with patch("agent_memory.semantic._model_fingerprint",return_value="test-model"),patch("agent_memory.semantic._worker",side_effect=worker) as run:
                build(config,"test")
                hits=search_hits(config,"another way to describe it",4,"test")
                self.assertEqual(len(hits),4)
                self.assertEqual(len({hit.ref for hit in hits}),4)
                self.assertTrue(all(count<=2 for count in Counter(hit.document_id for hit in hits).values()))
                for hit in hits:
                    self.assertIsNotNone(hit.span_start)
                    self.assertIn("chunk chars",fetch(config,hit.ref,span=(hit.span_start,hit.span_end)))
                rendered=format_candidates(hits,max_units=1200)
                self.assertIn("chunk chars",rendered)
                self.assertIn("similarity is not evidence",rendered)
                self.assertLessEqual(len(rendered.encode()),1200)
                before=run.call_count
                with self.assertRaises(ValueError):search_hits(config,"query",3,"vault")
                with self.assertRaises(ValueError):search_hits(config,"query",3,"different")
                with self.assertRaises(ValueError):search_hits(config,"query",3)
                self.assertEqual(run.call_count,before)
                result=call_tool(config,"memory_search",query="another phrasing",collection="test",semantic=True)
                self.assertIn("chunk chars",result["result"]["content"][0]["text"])
                for arguments in [dict(semantic="true"),dict(semantic=True,explain=True),dict(semantic=True,follow_ref=hits[0].ref)]:
                    result=call_tool(config,"memory_search",query="query",**arguments)
                    self.assertEqual(result["error"]["code"],-32602)
                (source/"flow.txt").write_text("changed bytes")
                remaining=search_hits(config,"another phrasing",4,"test")
                self.assertTrue(all(hit.relative_path!="flow.txt" for hit in remaining))
