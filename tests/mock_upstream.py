#!/usr/bin/env python3
"""Mock OpenAI-compatible upstream with controllable delays, for shim tests.

  GET  /v1/models                    -> instant JSON
  POST /v1/chat/completions stream   -> waits PREFILL_S, then 3 tokens 1s apart
  POST /v1/chat/completions          -> waits GEN_S, then one JSON body
  POST /error                        -> waits 1s, then 500 JSON
Delays via query params ?prefill= / ?gen= or env defaults.
"""
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

PORT = int(os.environ.get("MOCK_PORT", "18099"))


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, status, obj):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/echo"):
            self._json(200, {"authorization": self.headers.get("Authorization") or ""})
            return
        self._json(200, {"object": "list", "data": [{"id": "qwen-mock"}]})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        q = parse_qs(urlparse(self.path).query)
        if self.path.startswith("/error"):
            time.sleep(float(q.get("late", ["1"])[0]))
            if "text" in q:  # non-JSON, multi-line error page
                body = b"<html>\n<h1>502 Bad Gateway</h1>\n</html>\n"
                self.send_response(502)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self._json(500, {"error": {"message": "mock boom"}})
            return
        try:
            req = json.loads(raw or b"{}")
        except Exception:
            req = {}
        if req.get("stream"):
            prefill = float(q.get("prefill", [os.environ.get("PREFILL_S", "0")])[0])
            time.sleep(prefill)  # headers withheld until first token, like a busy LLM
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for tok in ["Hello", " slow", " world"]:
                data = f"data: {json.dumps({'choices': [{'delta': {'content': tok}}]})}\n\n".encode()
                self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
                self.wfile.flush()
                time.sleep(1)
            end = b"data: [DONE]\n\n"
            self.wfile.write(f"{len(end):x}\r\n".encode() + end + b"\r\n0\r\n\r\n")
            self.wfile.flush()
            return
        gen = float(q.get("gen", [os.environ.get("GEN_S", "0")])[0])
        time.sleep(gen)
        self._json(200, {"choices": [{"message": {"role": "assistant",
                                                  "content": "slow answer"}}]})


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
