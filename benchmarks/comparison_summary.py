"""Export compact synthetic/public result counts; never publish run transcripts."""
import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import statistics


def read_rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


def summarize(rows,expected):
    groups={}
    for group in sorted({row["group"] for row in rows}):
        selected=[row for row in rows if row["group"]==group]
        groups[group]=dict(cases=len(selected),complete=sum(row.get("complete") is True for row in selected))
        if all("source_reads" in row for row in selected):
            groups[group]["source_reads"]=sum(row["source_reads"] for row in selected)
        else:
            groups[group]["native_read_attempts"]=sum(row.get("tools",{}).get("Read",0) for row in selected)
    unique=len({row["id"] for row in rows})
    result=dict(status="complete" if len(rows)==unique==expected else "incomplete",record_count=len(rows),unique_cases=unique,expected=expected,
                positives=sum(row.get("complete") is not None for row in rows),
                complete=sum(row.get("complete") is True for row in rows),groups=groups)
    for key in ["elapsed_ms","retrieval_ms","search_bytes","read_bytes","source_reads","tool_output_bytes"]:
        values=[row[key] for row in rows if row.get(key) is not None]
        if values:result[key]=dict(median=statistics.median(values),maximum=max(values))
    result["records"]=[{key:row[key] for key in ["id","group","complete","requirements_met","search_calls",
        "search_attempts","search_bytes","source_reads","read_bytes","tools","tool_output_bytes","original_tool_output_bytes",
        "search_visible_bytes","read_visible_bytes","read_call_limit_exceeded","unexpected_tools",
        "failed_tool_calls","status","returncode"] if key in row} for row in rows]
    return result


def paired(baseline,other):
    first={row["id"]:row for row in baseline}
    second={row["id"]:row for row in other}
    common=sorted(set(first)&set(second))
    return dict(common_cases=len(common),gains=[key for key in common if first[key].get("complete") is False and second[key].get("complete") is True],
                losses=[key for key in common if first[key].get("complete") is True and second[key].get("complete") is False])


def collect(workspace):
    frozen=json.loads((workspace/"frozen.json").read_text())
    result=dict(protocol=frozen["protocol"],engine_revision=frozen["source_revision"],
        corpus_hash=frozen["corpus_hash"],cases_hash=frozen["cases_hash"],documents=frozen["documents"],
        chunks=frozen["chunks"],questions=frozen["cases"],controller_cases=frozen["controller_cases"],
        model=frozen["model"],reasoning=frozen["reasoning"],
        metric="All predeclared source spans delivered; not answer accuracy or population reliability",
        single={},controller={},native_filesystem={},native_mcp={},paired={})
    result["initial_harness_hashes"]=frozen.get("code_hashes",{})
    result["scoring_rule"]="Literal needles in delivered canonical source text; not full-function coverage or semantic answer judging"
    root=Path(__file__).resolve().parents[1]
    result["export_harness_hashes"]={str(path.relative_to(root)):hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((root/"benchmarks").iterdir()) if path.suffix in {".py",".mjs"}}
    result["configuration_notes"]=[
        "Graphiti uses benchmark-only punctuation-atom compatibility correction, native SAVE and verified reload; retry build reused exact-request cache",
        "Mem0 initial optional enrichment absent; BM25 backfill over unchanged facts is not full entity reingestion",
        "Native filesystem and MCP arms have matched output caps but different tools/prompts/evidence contracts",
        "Concurrent shared-queue wall times are not isolated hardware speed rankings"]
    for mode,names,expected in [("single",["lexical","lexical-scip","qmd","qmd-lexical","qmd-vector","qmd-hybrid","hipporag","graphiti","mem0-minimal","mem0"],50),
                               ("controller",["lexical","lexical-scip","qmd","hipporag","graphiti","mem0"],24)]:
        baseline=read_rows(workspace/("runs/lexical-"+mode+".jsonl"))
        for name in names:
            rows=read_rows(workspace/("runs/"+name+"-"+mode+".jsonl"))
            result[mode][name]=summarize(rows,expected)
            if rows and name!="lexical":result["paired"][mode+":"+name]=paired(baseline,rows)
    for name in ["letta","letta-budgeted"]:
        rows=read_rows(workspace/("runs/"+name+"-filesystem-evidence.jsonl"))
        result["native_filesystem"][name]=summarize(rows,24)
        result["native_filesystem"][name]["common_reader_interface"]=False
    native=read_rows(workspace/"runs/native-codex-evidence.jsonl")
    result["native_mcp"]["codex-core"]=summarize(native,24)
    result["native_mcp"]["codex-core"]["common_reader_interface"]=False
    result["native_mcp"]["codex-core"]["usage"]={key:sum(row.get("usage",{}).get(key,0) for row in native)
        for key in {key for row in native for key in row.get("usage",{})}}
    result["upstream_revisions"]={p.name.removesuffix(".revision.json"):json.loads(p.read_text())
        for p in sorted((workspace/"upstream").glob("*.revision.json"))}
    model_receipt=workspace/"models/qmd-verification.json"
    if model_receipt.exists():result["qmd_model_files"]=json.loads(model_receipt.read_text())
    versions=workspace/"runs/runtime-versions.json"
    if versions.exists():result["runtime_versions"]=json.loads(versions.read_text())
    window=workspace/"runs/e5-source-window-check.json"
    if window.exists():result["posthoc_e5_source_window_check"]=json.loads(window.read_text())
    backfill=workspace/"runs/mem0-state/bm25-backfill.json"
    if backfill.exists():result["mem0_backfill"]=json.loads(backfill.read_text())
    syntax=workspace/"runs/graphiti-fulltext-compatibility-check.json"
    if syntax.exists():
        value=json.loads(syntax.read_text())
        result["graphiti_syntax_check"]=dict(facts_checked=value["facts_checked"],errors=len(value["errors"]),patch=value["patch"])
    footprint=workspace/"runs/disk-footprints.json"
    if footprint.exists():result["disk_footprints"]=json.loads(footprint.read_text())
    result["native_build_ms_including_shared_queue"]={name:json.loads((workspace/("runs/"+name+"-build.json")).read_text())["build_ms"]
        for name in ["hipporag","graphiti","mem0"] if (workspace/("runs/"+name+"-build.json")).exists()}
    probe=workspace/"runs/qmd-warm-cache-build-probe.json"
    if probe.exists():
        value=json.loads(probe.read_text())
        result["qmd_separate_warm_cache_build_probe"]={key:value[key] for key in
            ["scope","scan_ms","embedding_and_model_load_ms","total_ms"]}
    coverage=workspace/"runs/scip-coverage.json"
    if coverage.exists():result["scip_index"]={key:value for key,value in json.loads(coverage.read_text()).items() if key!="functions"}
    # Count actual CLI invocations once, rather than counting a cached result
    # again for every framework/controller that reused it.
    usage=defaultdict(int)
    for path in (workspace/"inference").glob("*.json"):
        if len(path.stem)!=64:continue
        value=json.loads(path.read_text())
        if "usage" not in value:continue
        usage["successful_cli_invocations"]+=1
        for key,count in value["usage"].items():usage[key]+=count
    result["bridge_and_json_controller_codex_usage_including_preserved_trials"]=dict(usage)
    native_usage=defaultdict(int)
    transcripts=list((workspace/"logs").rglob("native-codex-*.events.jsonl"))
    for path in transcripts:
        for line in path.read_text().splitlines():
            event=json.loads(line)
            if event.get("type")=="turn.completed":
                native_usage["completed_cli_turns"]+=1
                for key,count in event.get("usage",{}).items():native_usage[key]+=count
    result["native_codex_event_usage_including_preserved_setup"]=dict(native_usage)
    return result


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    result=collect(args.workspace.resolve())
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n")
    for mode in ["single","controller","native_filesystem","native_mcp"]:
        print(mode,{name:(row["status"],row["complete"],row["positives"]) for name,row in result[mode].items()})
