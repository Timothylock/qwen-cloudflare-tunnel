#!/usr/bin/env python3
"""Keepalive shim between Cloudflare Tunnel and an OpenAI-compatible server.

Cloudflare kills a proxied request whose origin is silent for ~125s (524).
A local LLM is often silent that long: prefill before the first streamed
token, or the whole generation of a non-streaming reply. This shim keeps
the line alive:

  * streaming: no upstream headers after STREAM_COMMIT_DELAY -> commit a 200
    SSE response and send `: keepalive` comments every HEARTBEAT_INTERVAL of
    silence, before and between tokens.
  * non-streaming: no full body after NONSTREAM_COMMIT_DELAY -> commit a 200
    JSON response and send single spaces (valid leading JSON whitespace).
  * fast replies pass through with their real status and headers.

Trade-off: after an early commit the real status is gone, so a late
upstream error arrives as an error body under 200. Better than a 524.
Stdlib only; configured by environment variables (see .env.example).
"""

import hmac
import http.client
import json
import os
import queue
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

UPSTREAM_BASE = os.environ.get("UPSTREAM_BASE", "http://100.110.158.5:8080").rstrip("/")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8080"))
HEARTBEAT_INTERVAL = float(os.environ.get("HEARTBEAT_INTERVAL", "15"))
STREAM_COMMIT_DELAY = float(os.environ.get("STREAM_COMMIT_DELAY", "10"))
NONSTREAM_COMMIT_DELAY = float(os.environ.get("NONSTREAM_COMMIT_DELAY", "45"))
MAX_REQUEST_SECONDS = float(os.environ.get("MAX_REQUEST_SECONDS", "1800"))
MAX_BODY_BYTES = int(os.environ.get("MAX_BODY_BYTES", str(32 * 1024 * 1024)))
SHIM_API_KEY = os.environ.get("SHIM_API_KEY", "")

_up = urlparse(UPSTREAM_BASE)
UPSTREAM_PREFIX = _up.path.rstrip("/")

HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
              "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length"}
DROP_REQUEST = HOP_BY_HOP | {"accept-encoding", "expect"}  # identity bodies only
DROP_RESPONSE = HOP_BY_HOP | {"date", "server"}  # BaseHTTPRequestHandler adds its own
EARLY_HEADERS = [("Cache-Control", "no-cache"), ("X-Accel-Buffering", "no")]


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def make_conn(timeout):
    cls = http.client.HTTPSConnection if _up.scheme == "https" else http.client.HTTPConnection
    return cls(_up.hostname, _up.port, timeout=timeout)


def close_conn(conn):
    """Close the upstream socket, waking a worker blocked in recv()."""
    try:
        if conn.sock:
            conn.sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    conn.close()


def is_stream_request(body, headers):
    if "text/event-stream" in (headers.get("Accept") or ""):
        return True
    try:
        data = json.loads(body) if body else None
    except ValueError:
        return False
    return isinstance(data, dict) and data.get("stream") is True


def error_obj(msg):
    return {"error": {"message": msg, "type": "upstream_error"}}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "qwen-shim/1.1"
    timeout = 60  # idle/slow clients cannot pin a thread forever

    def log_message(self, fmt, *args):
        pass

    # ---- response helpers ------------------------------------------------
    def _start(self, status, headers, length=None):
        self.send_response(status)
        for k, v in headers:
            self.send_header(k, v)
        if length is None:
            self.send_header("Transfer-Encoding", "chunked")
        else:
            self.send_header("Content-Length", str(length))
        if status >= 400 or self.close_connection:
            self.send_header("Connection", "close")  # also sets close_connection
        self.end_headers()

    def _reply(self, status, obj):
        body = json.dumps(obj).encode()
        self._start(status, [("Content-Type", "application/json")], len(body))
        self.wfile.write(body)

    def _chunk(self, data):
        if data:
            self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))

    def _fail(self, committed, stream, status, msg):
        """Deliver an error before or after the response was committed."""
        if not committed:
            return self._reply(status, error_obj(msg))
        err = json.dumps(error_obj(msg)).encode()
        self._chunk(b"data: " + err + b"\n\n" if stream else err)
        self.wfile.write(b"0\r\n\r\n")

    def _authorized(self):
        if not SHIM_API_KEY:
            return True
        auth = str(self.headers.get("Authorization") or "")  # first value only
        return hmac.compare_digest(auth.encode("latin-1", "replace"),
                                   f"Bearer {SHIM_API_KEY}".encode())

    # ---- entry point -------------------------------------------------------
    def _handle(self):
        lengths = [v.strip() for v in self.headers.get_all("Content-Length") or []]
        if (self.headers.get("Transfer-Encoding") or len(lengths) > 1
                or (lengths and not (lengths[0].isascii() and lengths[0].isdigit()))):
            return self._reply(400, error_obj("bad request framing (chunked or bad Content-Length)"))
        length = int(lengths[0]) if lengths else 0
        keep_alive = not self.close_connection
        if length > MAX_BODY_BYTES:
            return self._reply(413, error_obj("request body too large"))
        if length:
            self.close_connection = True  # until the body is actually consumed

        if self.path == "/health":
            return self._reply(200, {"status": "ok"})
        if self.path == "/health/upstream":
            return self._health_upstream()
        if not self._authorized():
            return self._reply(401, {"error": {"message": "invalid api key",
                                               "type": "invalid_request_error"}})

        body = self.rfile.read(length) if length else b""
        if len(body) != length:
            return self._reply(400, error_obj("incomplete request body"))
        self.close_connection = not keep_alive

        headers = {}
        for k, v in self.headers.items():
            if k.lower() not in DROP_REQUEST:
                headers.setdefault(k.lower(), v)  # duplicates: first value wins
        self._proxy(body, headers, is_stream_request(body, self.headers))

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _handle

    def _health_upstream(self):
        conn = make_conn(timeout=5)
        try:
            conn.request("GET", UPSTREAM_PREFIX + "/v1/models")
            status = conn.getresponse().status
            self._reply(200, {"status": "reachable", "upstream_status": status})
        except Exception as e:
            log(f"health/upstream: {UPSTREAM_BASE} unreachable: {e!r}")
            self._reply(502, {"status": "unreachable"})
        finally:
            conn.close()

    # ---- proxy: one worker thread, one heartbeat loop ----------------------
    def _fetch(self, conn, body, headers, q):
        try:
            conn.request(self.command, UPSTREAM_PREFIX + self.path,
                         body=body or None, headers=headers)
            resp = conn.getresponse()
            q.put(("head", resp))
            while data := resp.read1(65536):
                q.put(("data", data))
            q.put(("end", None))
        except Exception as e:
            log(f"{self.command} {self.path} upstream error: {e!r}")
            q.put(("error", None))

    def _proxy(self, body, headers, stream):
        started = time.monotonic()
        deadline = started + MAX_REQUEST_SECONDS
        commit_at = started + (STREAM_COMMIT_DELAY if stream else NONSTREAM_COMMIT_DELAY)
        beat = b": keepalive\n\n" if stream else b" "
        q = queue.Queue()
        conn = make_conn(timeout=MAX_REQUEST_SECONDS)
        threading.Thread(target=self._fetch, args=(conn, body, headers, q), daemon=True).start()
        # mode: None = nothing sent, "early" = committed 200 + heartbeats,
        # "pass" = relaying upstream bytes as they arrive (streams only).
        mode, resp, buf, last = None, None, [], started
        try:
            while True:
                now = time.monotonic()
                if now >= deadline:
                    log(f"{self.command} {self.path}: max request time hit")
                    return self._fail(mode, stream, 504, "upstream exceeded max request time")
                if mode is None and now >= commit_at:
                    ctype = "text/event-stream" if stream else "application/json"
                    self._start(200, [("Content-Type", ctype)] + EARLY_HEADERS)
                    mode, last = "early", now
                    log(f"{self.command} {self.path}: committed early at {now - started:.1f}s")
                if mode and now - last >= HEARTBEAT_INTERVAL:
                    self._chunk(beat)
                    last = now
                try:
                    kind, val = q.get(timeout=0.5)
                except queue.Empty:
                    continue

                if kind == "error":
                    return self._fail(mode, stream, 502, "upstream error")
                if kind == "head":
                    resp = val
                    if stream and resp.status == 200 and mode != "pass":
                        if mode is None:
                            self._start(200, [(k, v) for k, v in resp.getheaders()
                                              if k.lower() not in DROP_RESPONSE])
                        mode = "pass"
                elif kind == "data":
                    if mode == "pass":
                        self._chunk(val)
                        last = time.monotonic()
                    else:
                        buf.append(val)
                else:  # end
                    payload = b"".join(buf)
                    if mode is None:
                        self._start(resp.status, [(k, v) for k, v in resp.getheaders()
                                                  if k.lower() not in DROP_RESPONSE],
                                    len(payload))
                        self.wfile.write(payload)
                    else:
                        if mode == "early" and stream:  # late non-200 under SSE
                            payload = b"data: " + payload.strip() + b"\n\n"
                        self._chunk(payload)
                        self.wfile.write(b"0\r\n\r\n")
                    log(f"{self.command} {self.path} -> {resp.status} "
                        f"({mode or 'direct'}) in {time.monotonic() - started:.1f}s")
                    return
        except OSError as e:  # client went away or stalled past the timeout
            log(f"{self.command} {self.path}: client gone after "
                f"{time.monotonic() - started:.1f}s ({e!r})")
            self.close_connection = True
        except Exception as e:
            log(f"{self.command} {self.path}: shim error: {e!r}")
            self.close_connection = True
            try:
                self._fail(mode, stream, 502, "shim error")
            except Exception:
                pass
        finally:
            close_conn(conn)


def main():
    if not SHIM_API_KEY:
        log("WARNING: SHIM_API_KEY is empty - the shim will forward any request.")
    log(f"qwen-shim listening on :{LISTEN_PORT} -> {UPSTREAM_BASE} "
        f"(heartbeat {HEARTBEAT_INTERVAL}s, stream commit {STREAM_COMMIT_DELAY}s, "
        f"non-stream commit {NONSTREAM_COMMIT_DELAY}s, max body {MAX_BODY_BYTES}B)")
    ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler).serve_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
