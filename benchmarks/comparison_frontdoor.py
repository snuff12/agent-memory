"""Loopback model discovery and forwarding for the native Letta client."""
import json
import argparse
import copy
from pathlib import Path
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.request


def visible_budget(request):
    request=copy.deepcopy(request)
    names,seen,views={},{},[]
    search_bytes=searches=reads=0
    for message in request["messages"]:
        if message.get("role")=="assistant":
            for call in message.get("tool_calls") or []:
                names[call["id"]]=call.get("function",{}).get("name")
        if message.get("role")!="tool":continue
        identifier=message.get("tool_call_id")
        if identifier in seen:
            message["content"]=seen[identifier]
            continue
        name=names.get(identifier)
        content=message.get("content") or ""
        if not isinstance(content,str):content=json.dumps(content,ensure_ascii=False)
        if name in {"Grep","Glob"}:
            searches+=1
            cap=min(1200,4000-search_bytes) if searches<=6 else 0
        elif name=="Read":
            reads+=1
            cap=1200 if reads<=3 else 0
        else:cap=0
        if cap<=0:
            content="[Source output withheld: tool/byte budget exhausted.]"
            counted=0
        elif len(content.encode())>cap:
            marker="\n[Truncated by shared byte budget.]"
            if cap<len(marker.encode()):content=marker.encode()[:cap].decode(errors="ignore")
            else:content=content.encode()[:cap-len(marker.encode())].decode(errors="ignore")+marker
            counted=len(content.encode())
        else:counted=len(content.encode())
        message["content"]=content
        seen[identifier]=content
        if name in {"Grep","Glob"}:search_bytes+=counted
        views.append(dict(tool_call_id=identifier,name=name,content=content,source_output_bytes=counted))
    assert search_bytes<=4000
    return request,views


class Handler(BaseHTTPRequestHandler):
    budgeted=False
    workspace=None
    ledger_lock=threading.Lock()
    def log_message(self, *args):
        pass

    def send(self, body):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.send(json.dumps(dict(object="list",data=[dict(id="gpt-6.1-sol",object="model",
                                                          created=0,owned_by="local-benchmark")])).encode())

    def do_POST(self):
        incoming=json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        streaming=incoming.pop("stream",False)
        incoming["stream"]=False
        if self.budgeted:
            incoming,views=visible_budget(incoming)
            with self.ledger_lock:
                with (self.workspace/"logs/letta-budgeted-views.jsonl").open("a") as stream:
                    stream.write(json.dumps(dict(views=views),ensure_ascii=False)+"\n")
        request = urllib.request.Request("http://127.0.0.1:55961"+self.path,
            data=json.dumps(incoming).encode(), headers={"Content-Type":"application/json"})
        try:
            with urllib.request.urlopen(request,timeout=480) as response:
                body=response.read()
            result=json.loads(body)
            for i,call in enumerate(result["choices"][0]["message"].get("tool_calls") or []):
                call["id"]="call_%s_%d" % (result["id"].replace("bench-", ""),i)
            body=json.dumps(result).encode()
            if not streaming:
                self.send(body)
                return
            # Transport-only SSE framing. Model generation is buffered, so this
            # arm cannot establish true streaming latency or throughput.
            self.send_response(200)
            self.send_header("Content-Type","text/event-stream")
            self.end_headers()
            message=result["choices"][0]["message"]
            delta={"role":"assistant","content":message.get("content") or ""}
            if message.get("tool_calls"):
                delta["tool_calls"]=[dict(index=i,**call) for i,call in enumerate(message["tool_calls"])]
            for payload in [dict(delta=delta,finish_reason=None),dict(delta={},finish_reason=result["choices"][0]["finish_reason"])]:
                chunk=dict(id=result["id"],object="chat.completion.chunk",created=result["created"],
                           model=result["model"],choices=[dict(index=0,**payload)])
                self.wfile.write(("data: "+json.dumps(chunk)+"\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
        except Exception as exc:
            body=json.dumps(dict(error=dict(message=str(exc)))).encode()
            self.send_error(502, body.decode())


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--budgeted",action="store_true")
    parser.add_argument("--workspace",type=Path)
    args=parser.parse_args()
    Handler.budgeted=args.budgeted
    Handler.workspace=args.workspace
    if args.budgeted and args.workspace is None:parser.error("--workspace is required for budgeted views")
    ThreadingHTTPServer(("127.0.0.1",55963 if args.budgeted else 55962),Handler).serve_forever()
