import io
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from agent_memory.cli import main
from agent_memory.config import load_config
from agent_memory.indexer import sync_all
from agent_memory.memory_policy import MemoryPolicy
from agent_memory.search import fetch, search, StaleReferenceError
from agent_memory.source_navigation import locate
from tests.test_memory_policy import card
from tests.test_memory_system import make_home
from tests.test_practical_retrieval import call_tool


class SourceNavigationTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root=Path(temporary.name)
        self.source=root/"source"
        self.source.mkdir()
        self.home=make_home(root,self.source)
        self.config=load_config(self.home)
        self.file=self.source/"handler.py"
        self.original="\n".join(["# intro"]*20+["def settle_receipt():", "    return '완료'"]+
            ["# background"]*30+["result = settle_receipt()", "print(result)"])+"\n"
        self.file.write_text(self.original)
        sync_all(self.config)
        self.ref=search(self.config,"settle_receipt",collection="test")[0].ref

    def test_literal_navigation_and_absolute_lines_cross_chunk_boundaries(self):
        located=locate(self.config,self.ref,"settle_receipt",max_bytes=1200)
        self.assertIn("21: def settle_receipt():",located)
        self.assertIn("53: result = settle_receipt()",located)
        excerpt=fetch(self.config,self.ref,lines=(53,54))
        self.assertIn("53: result = settle_receipt()\n54: print(result)",excerpt)
        self.assertIn(str(self.file),excerpt)
        self.assertIn("sha256:",excerpt)
        self.assertEqual(self.file.read_text(),self.original)
        self.assertIn("No literal match",locate(self.config,self.ref,"settle.*receipt"))
        self.assertIn("53:",locate(self.config,self.ref,"settle_receipt",after_line=21))
        self.assertNotIn("21: def",locate(self.config,self.ref,"settle_receipt",after_line=21))

    def test_complete_utf8_lines_and_continuation_fit_the_byte_budget(self):
        self.file.write_text("\n".join("evidence %03d 한글 원문 원문 원문"%i for i in range(100)))
        sync_all(self.config)
        ref=search(self.config,"evidence",collection="test")[0].ref
        output=fetch(self.config,ref,lines=(1,100),max_tokens=800)
        self.assertLessEqual(len(output.encode()),800)
        next_line=int(re.search(r"continuation: start_line=(\d+)",output).group(1))
        self.assertIn("%d: evidence"%(next_line-1),output)
        self.assertNotIn("%d: evidence"%next_line,output)
        rest=fetch(self.config,ref,lines=(next_line,100),max_tokens=800)
        self.assertIn("%d: evidence"%next_line,rest)
        located=locate(self.config,ref,"evidence",limit=2,max_bytes=800)
        self.assertIn("after_line=2",located)
        self.assertLessEqual(len(located.encode()),800)
        with self.assertRaises(ValueError):fetch(self.config,ref,lines=(0,1))
        with self.assertRaises(ValueError):fetch(self.config,ref,lines=(1,101))
        with self.assertRaises(ValueError):fetch(self.config,ref,lines=(1,2),query="evidence")

    def test_live_policy_and_source_revalidation_before_and_after_read(self):
        target=self.home/"vault/private.md"
        target.write_text(card(scope="project:one")+"\nprotected evidence\n")
        sync_all(self.config)
        policy=MemoryPolicy(project="one")
        ref=search(self.config,"protected",collection="vault",policy=policy)[0].ref
        for operation in [lambda:locate(self.config,ref,"protected"),lambda:fetch(self.config,ref,lines=(1,2))]:
            with self.assertRaises(StaleReferenceError):operation()
        self.assertIn("protected evidence",locate(self.config,ref,"protected",policy=policy))
        import agent_memory.source_navigation as navigation
        original_header=navigation._header
        def expire(row,ref):
            from agent_memory.database import connect
            connection=connect(self.config)
            connection.execute("UPDATE memory_metadata SET scope='project:two' WHERE document_id=?",(row["id"],))
            connection.commit();connection.close()
            return original_header(row,ref)
        with patch.object(navigation,"_header",side_effect=expire):
            with self.assertRaises(StaleReferenceError):locate(self.config,ref,"protected",policy=policy)
        self.file.write_text(self.original+"changed")
        with self.assertRaises(StaleReferenceError):locate(self.config,self.ref,"settle_receipt")

    def test_read_payload_hash_is_checked_even_if_metadata_validation_races(self):
        import agent_memory.source_navigation as navigation
        row=navigation._document(self.config,self.ref,None)
        def race(*args,**kwargs):
            self.file.write_text("unindexed transient source bytes")
            return row
        with patch.object(navigation,"_document",side_effect=race):
            with self.assertRaisesRegex(StaleReferenceError,"Read bytes differ"):
                locate(self.config,self.ref,"transient")

    def test_mcp_and_cli_expose_byte_alias_and_unambiguous_navigation(self):
        response=call_tool(self.config,"memory_locate",ref=self.ref,query="settle_receipt")
        self.assertIn("53: result",response["result"]["content"][0]["text"])
        for argument in [dict(start_line=21,end_line=22,max_bytes=1200),dict(start_line=21,end_line=22,max_tokens=1200)]:
            response=call_tool(self.config,"memory_fetch",ref=self.ref,**argument)
            self.assertIn("return '완료'",response["result"]["content"][0]["text"])
        for argument in [dict(start_line=True,end_line=22),dict(start_line=21),dict(span_start=0),
                         dict(max_bytes=1200,max_tokens=1200),dict(start_line=21,end_line=22,query="return")]:
            response=call_tool(self.config,"memory_fetch",ref=self.ref,**argument)
            self.assertEqual(response["error"]["code"],-32602)
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["--home",str(self.home),"read",self.ref,"--start-line","53","--end-line","54","--max-bytes","1200"]),0)
        self.assertIn("53: result",output.getvalue())

    def test_ref_does_not_authorize_nonexistent_section_or_other_paths(self):
        invented=self.ref.rsplit("#",1)[0]+"#999999"
        with self.assertRaises(StaleReferenceError):locate(self.config,invented,"settle_receipt")
        self.file.unlink()
        self.file.symlink_to(self.home/"config.json")
        with self.assertRaises(StaleReferenceError):locate(self.config,self.ref,"database")
