"""Guard the evaluation boundary, not a target retrieval score."""
import json
import contextlib
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from benchmarks.comparison_common import Sources, prepare
from benchmarks.system_comparison import controlled
from benchmarks.scip_comparison import ScipNavigation
from benchmarks.letta_comparison import score as score_letta
from benchmarks.comparison_frontdoor import visible_budget
from benchmarks.comparison_summary import collect, summarize


class FakeInference:
    def __init__(self, actions):
        self.actions=iter(actions)
        self.requests=[]

    def complete(self,messages,**kwargs):
        self.requests.append(messages)
        return dict(text=json.dumps(next(self.actions)),usage={},request_hash="fake")


class ComparisonBoundaryTests(unittest.TestCase):
    def test_public_summary_excludes_source_paths_and_transcripts(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace=Path(temporary)
            prepare(workspace)
            (workspace/"upstream").mkdir()
            (workspace/"runs/qmd-warm-cache-build-probe.json").write_text(json.dumps(dict(
                scope="warm-cache probe",scan_ms=1,embedding_and_model_load_ms=2,total_ms=3,
                status=dict(collections=[dict(path="private-source-location")]))))
            (workspace/"logs/private-transcript.json").write_text("do not publish this conversation")
            exported=json.dumps(collect(workspace))
            self.assertNotIn("private-source-location",exported)
            self.assertNotIn("do not publish this conversation",exported)
            duplicate=[dict(id="same",group="test",complete=True)]*2
            self.assertEqual(summarize(duplicate,2)["status"],"incomplete")

    def test_native_visible_budget_caps_utf8_and_replayed_tool_results(self):
        calls=[dict(id="search%d"%i,function=dict(name="Grep")) for i in range(7)]
        calls += [dict(id="read%d"%i,function=dict(name="Read")) for i in range(4)]
        request=dict(messages=[dict(role="assistant",tool_calls=calls)]+[
            dict(role="tool",tool_call_id=call["id"],content="한글 원문 "*2000) for call in calls])
        request["messages"].append(dict(request["messages"][1]))
        trimmed,views=visible_budget(request)
        self.assertLessEqual(sum(x["source_output_bytes"] for x in views if x["name"]=="Grep"),4000)
        reads=[x for x in views if x["name"]=="Read"]
        self.assertEqual(sum(x["source_output_bytes"]>0 for x in reads),3)
        self.assertTrue(all(x["source_output_bytes"]<=1200 for x in views))
        self.assertEqual(trimmed["messages"][1]["content"],trimmed["messages"][-1]["content"])
        self.assertGreater(len(request["messages"][1]["content"].encode()),1200)

    def test_native_grep_requires_a_real_matching_source_line(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace=Path(temporary)
            source=workspace/"memory/knowledge"
            file=source/"events/record-00-a.txt"
            file.parent.mkdir(parents=True)
            file.write_text("Retention is 37 days.\n")
            (workspace/"logs").mkdir()
            (workspace/"runs").mkdir()
            (workspace/"corpus.json").write_text(json.dumps([dict(path="events/record-00-a.txt")]))
            (workspace/"cases.json").write_text(json.dumps([dict(id="test",group="test",requirements=[
                dict(path="events/record-00-a.txt",needle="37 days")])]))
            for rendered,expected in [(str(file)+":1:Retention is 37 days.",True),
                                      ("events/record-00-a.txt-1-Retention is 37 days.",True),
                                      ("1:Retention is 37 days.",True),
                                      (str(file)+":1:Retention is 38 days.",False),
                                      (str(file)+":0:Retention is 37 days.",False)]:
                events=[dict(type="message",message_type="tool_call_message",tool_call=dict(name="Grep",
                    tool_call_id="call",arguments=json.dumps(dict(path=str(file))))),
                    dict(type="message",message_type="tool_return_message",tool_call_id="call",tool_return=rendered),
                    dict(type="result")]
                (workspace/"logs/letta-test.jsonl").write_text("\n".join(map(json.dumps,events)))
                with contextlib.redirect_stdout(io.StringIO()):score_letta(workspace)
                record=json.loads((workspace/"runs/letta-filesystem-evidence.jsonl").read_text())
                self.assertEqual(record["complete"],expected,rendered)

    def test_code_filename_does_not_trigger_same_named_function(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace=Path(temporary)
            (workspace/"runs").mkdir()
            (workspace/"runs/scip-index.json").write_text(json.dumps(dict(documents=[dict(
                relative_path="module.py",occurrences=[dict(symbol="scip-python pkg/module/foo().",
                symbol_roles=1,range=[0,0,3])])])) )
            native=SimpleNamespace(hit=SimpleNamespace(ref="native",document_id="one"))
            fallback=SimpleNamespace(hit=SimpleNamespace(ref="fallback",document_id="two"))
            sources=SimpleNamespace(workspace=workspace,rows=[dict(relative_path="module.py",start_line=1,end_line=3)],
                                    by_id={"p0000":native},lexical=lambda query:[fallback])
            navigation=ScipNavigation(sources)
            self.assertEqual(navigation.search("inspect foo.py"),[fallback])
            self.assertEqual(navigation.search("inspect `foo`"),[native,fallback])

    def test_native_corpus_has_no_questions_or_evidence_labels(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace=Path(temporary)
            frozen=prepare(workspace)
            corpus=json.loads((workspace/"corpus.json").read_text())
            cases=json.loads((workspace/"cases.json").read_text())
            queries=json.loads((workspace/"queries.json").read_text())
            self.assertTrue(all(set(row)=={"id","query"} for row in queries))
            self.assertEqual(frozen["cases"],50)
            for row in corpus:
                self.assertEqual(set(row),{"id","path","text","ref","ordinal","title"})
            indexed="\n".join(row["text"] for row in corpus)
            for case in cases:
                self.assertNotIn(case["query"],indexed)
            with self.assertRaisesRegex(ValueError,"overwrite"):
                prepare(workspace)

    def test_unknown_native_source_cannot_become_a_citation(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace=Path(temporary)
            prepare(workspace)
            sources=Sources(workspace)
            self.assertEqual(sources.candidates([dict(source="gold-answer-file",score=1)]),[])

    def test_controller_cannot_read_unreturned_refs(self):
        inference=FakeInference([dict(action="read",ref="invented",part=1),dict(action="finish",answer="No evidence")])
        sources=SimpleNamespace(config=None,by_ref={})
        with patch("benchmarks.system_comparison.fetch") as read:
            result=controlled(sources,"Find the retention rule",lambda query:[],inference)
        read.assert_not_called()
        self.assertEqual(result["source_reads"],0)
        self.assertIn("unknown ref",result["errors"][0])
        context=json.loads(inference.requests[0][1]["content"])
        self.assertEqual(set(context),{"question","remaining","observations"})

    def test_read_budget_counts_repeated_continuations(self):
        actions=[dict(action="search",query="bridge",follow_ref="")]
        actions += [dict(action="read",query="",ref="ref",part=i) for i in range(1,5)]
        actions += [dict(action="finish",answer="Stopped at the read budget")]
        inference=FakeInference(actions)
        hit=SimpleNamespace(ref="ref",relative_path="source.txt")
        sources=SimpleNamespace(config=None,by_ref={"ref":SimpleNamespace(hit=hit)})
        # Only formatting is mocked: every actual continuation still traverses
        # the same budget gate before the source reader can run.
        with patch("benchmarks.system_comparison.replace",side_effect=lambda hit,**kwargs:hit), \
             patch("benchmarks.system_comparison.format_search_with_visible_hits",return_value=("ref",[hit])), \
             patch("benchmarks.system_comparison.fetch",return_value="unchanged source") as read:
            result=controlled(sources,"Read the relevant source",lambda query:[SimpleNamespace(hit=hit)],inference)
        self.assertEqual(read.call_count,3)
        self.assertEqual(result["source_reads"],3)
        self.assertIn("Read budget exhausted",result["errors"][0])


if __name__=="__main__":
    unittest.main()
