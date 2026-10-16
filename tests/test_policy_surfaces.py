from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import tempfile
import unittest

from agent_memory.cli import main
from agent_memory.config import load_config
from agent_memory.followup import followup_search
from agent_memory.indexer import sync_all
from agent_memory.mcp_server import TOOLS, handle_request
from agent_memory.memory_policy import MemoryPolicy
from agent_memory.search import StaleReferenceError, fetch, search
from agent_memory.source_navigation import locate
from tests.test_memory_policy import card
from tests.test_memory_system import make_home


CARDS = {
    "control": ("control.md", {}),
    "superseded": ("superseded.md", {"status": "superseded"}),
    "inbox": ("inbox/inbox.md", {"status": "inbox"}),
    "expired": ("expired.md", {"review_after": "2000-01-01"}),
    "future": ("future.md", {"valid_from": "2999-01-01"}),
    "scoped": ("scoped.md", {"scope": "project:zephyr"}),
    "invalid": ("invalid.md", {"source": ""}),
}
QUERY = " ".join("needle" + name for name in CARDS)


class PolicySurfaceTests(unittest.TestCase):
    maxDiff = None

    def test_default_policy_hides_cards_on_every_read_surface(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            home = make_home(root, source)
            for name, (relative, fields) in CARDS.items():
                (home / "vault" / relative).write_text(card(**fields) + "needle%s\nQuillborn uses Store%s.\n" % (name, name))
            (home / "vault" / "anchor.md").write_text(card() + "Quillborn coordinates every store.\n")
            config = load_config(home)
            sync_all(config)
            # Refs come from a review policy that admits every state, so each
            # card is indexed and only the default policy can hide it.
            review = MemoryPolicy(status="all", project="zephyr")
            hits = {name: next(hit for hit in search(config, "needle" + name, collection="vault", policy=review)
                               if hit.relative_path == relative) for name, (relative, _) in CARDS.items()}
            refs = {name: hit.ref for name, hit in hits.items()}
            ids = {name: hit.document_id for name, hit in hits.items()}
            anchor = search(config, "coordinates", collection="vault")[0].ref
            mcp_tools = set()

            def cli(*arguments):
                with redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()):
                    code = main(["--home", str(home), *arguments])
                return stdout.getvalue() if code == 0 else None

            def mcp(tool, **arguments):
                mcp_tools.add(tool)
                result = handle_request(config, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                 "params": {"name": tool, "arguments": arguments}})["result"]
                return None if result["isError"] else result["content"][0]["text"]

            def python(operation):
                try:
                    return operation()
                except StaleReferenceError:
                    return None

            listings = {
                "search": json.dumps([hit.document_id for hit in search(config, QUERY, limit=20)]),
                "cli search": cli("search", QUERY, "--limit", "20"),
                "cli search --json": cli("search", QUERY, "--limit", "20", "--json"),
                "cli search --explain": cli("search", QUERY, "--limit", "20", "--explain"),
                "mcp memory_search": mcp("memory_search", query=QUERY, limit=20),
                "mcp memory_search explain": mcp("memory_search", query=QUERY, limit=20, explain=True),
                "follow-up results": json.dumps([hit.document_id for hit in
                                                 followup_search(config, "Quillborn", anchor, limit=20).results]),
                "cli follow-up results": cli("search", "Quillborn", "--follow-ref", anchor, "--limit", "20", "--json"),
                "mcp follow-up results": mcp("memory_search", query="Quillborn", follow_ref=anchor, limit=20),
            }

            def reads(ref):
                return {
                    "fetch": python(lambda: fetch(config, ref)),
                    "fetch query": python(lambda: fetch(config, ref, query="Quillborn")),
                    "fetch span": python(lambda: fetch(config, ref, span=(0, 5))),
                    "fetch lines": python(lambda: fetch(config, ref, lines=(1, 3))),
                    "cli read": cli("read", ref),
                    "cli read --query": cli("read", ref, "--query", "Quillborn"),
                    "cli read span": cli("read", ref, "--span-start", "0", "--span-end", "5"),
                    "cli read lines": cli("read", ref, "--start-line", "1", "--end-line", "3"),
                    "mcp memory_fetch": mcp("memory_fetch", ref=ref),
                    "mcp memory_fetch query": mcp("memory_fetch", ref=ref, query="Quillborn"),
                    "mcp memory_fetch span": mcp("memory_fetch", ref=ref, span_start=0, span_end=5),
                    "mcp memory_fetch lines": mcp("memory_fetch", ref=ref, start_line=1, end_line=3),
                    "locate": python(lambda: locate(config, ref, "Quillborn")),
                    "cli locate": cli("locate", ref, "Quillborn"),
                    "mcp memory_locate": mcp("memory_locate", ref=ref, query="Quillborn"),
                    "follow-up anchor": python(lambda: followup_search(config, "Quillborn", ref).anchor.ref),
                    "cli follow-up anchor": cli("search", "Quillborn", "--follow-ref", ref),
                    "mcp follow-up anchor": mcp("memory_search", query="Quillborn", follow_ref=ref),
                }

            # Visibility means a successful call whose output carries the
            # card's document id; filenames can appear in unrelated text.
            outputs = {name: {**listings, **reads(ref)} for name, ref in refs.items()}
            visible = {name: sorted(surface for surface, text in outputs[name].items()
                                    if text is not None and ids[name] in text) for name in CARDS}
            self.assertEqual(visible.pop("control"), sorted(outputs["control"]))
            self.assertEqual(visible, {name: [] for name in CARDS if name != "control"})
            # Catalog is metadata only: counts, never document ids or text.
            listed = mcp("memory_catalog")
            self.assertFalse([name for name in CARDS if ids[name] in listed or "needle" + name in listed])
            # A new MCP tool must join this matrix before it can ship.
            self.assertEqual(mcp_tools, {tool["name"] for tool in TOOLS})


if __name__ == "__main__":
    unittest.main()
