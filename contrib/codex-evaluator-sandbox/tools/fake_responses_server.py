#!/usr/bin/env python3
"""Scripted stand-in for the OpenAI Responses API (model-free evidence).

Bound to 127.0.0.1 only. Point a codex model provider at it:

    model_provider = "a3fake"
    [model_providers.a3fake]
    name = "a3fake"
    base_url = "http://127.0.0.1:<port>/v1"
    wire_api = "responses"
    requires_openai_auth = false

Every POST body codex sends is appended to --log (JSONL: the full request, so
the EXACT tool list the model would see is recorded). Each request is answered
with the next scripted step from --script (JSON list); a step is a list of
output items, e.g.
    [{"type": "function_call", "name": "exec_command", "arguments": {...}}]
    [{"type": "custom_tool_call", "name": "apply_patch", "input": "*** Begin Patch..."}]
    [{"type": "message", "text": "done"}]
When the script is exhausted it answers with a final assistant message.

Usage: fake_responses_server.py --log FILE [--script FILE] [--port-file FILE]
"""
import argparse
import http.server
import itertools
import json
import threading

ap = argparse.ArgumentParser()
ap.add_argument("--log", required=True)
ap.add_argument("--script")
ap.add_argument("--port-file")
a = ap.parse_args()

STEPS = json.load(open(a.script)) if a.script else []
lock = threading.Lock()
step_idx = itertools.count()
ids = itertools.count(1)


def sse(ev):
    return ("event: %s\ndata: %s\n\n" % (ev["type"], json.dumps(ev))).encode()


def items_for(step):
    out = []
    for it in step:
        n = next(ids)
        if it["type"] == "function_call":
            args = it["arguments"]
            fc = {"type": "function_call", "id": "fc_%d" % n, "call_id": "call_%d" % n,
                  "name": it["name"],
                  "arguments": args if isinstance(args, str) else json.dumps(args)}
            if it.get("namespace"):
                fc["namespace"] = it["namespace"]
            out.append(fc)
        elif it["type"] == "custom_tool_call":
            out.append({"type": "custom_tool_call", "id": "ctc_%d" % n, "call_id": "call_%d" % n,
                        "name": it["name"], "input": it["input"]})
        else:
            out.append({"type": "message", "role": "assistant", "id": "msg_%d" % n,
                        "content": [{"type": "output_text", "text": it.get("text", "done"),
                                     "annotations": []}]})
    return out


class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):  # /v1/models etc.
        body = json.dumps({"object": "list", "data": [{"id": "a3-fake", "object": "model"}]}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("content-length") or 0))
        try:
            req = json.loads(raw)
        except ValueError:
            req = {"_raw": raw[:2000].decode("utf-8", "replace")}
        i = next(step_idx)
        with lock, open(a.log, "a") as f:
            f.write(json.dumps({"n": i, "path": self.path, "request": req}) + "\n")
        step = STEPS[i] if i < len(STEPS) else [{"type": "message", "text": "A3_FAKE_DONE"}]
        rid = "resp_%d" % i
        body = sse({"type": "response.created", "response": {"id": rid}})
        for k, item in enumerate(items_for(step)):
            body += sse({"type": "response.output_item.done", "output_index": k, "item": item})
        body += sse({"type": "response.completed", "response": {
            "id": rid, "usage": {"input_tokens": 1, "input_tokens_details": None, "output_tokens": 1,
                                 "output_tokens_details": None, "total_tokens": 2}}})
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
if a.port_file:
    with open(a.port_file, "w") as f:
        f.write(str(srv.server_address[1]))
srv.serve_forever()
