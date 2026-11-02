#!/usr/bin/env python3
"""End-to-end tests: shim (compressed timers) in front of the mock upstream."""
import http.client
import json
import os
import socket
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHIM_PORT = 18098
MOCK_PORT = 18099
KEY = "testkey123"
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {detail}")


def wait_port(port, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            return True
        except OSError:
            time.sleep(0.2)
    return False


def req(method, path, body=None, headers=None, timeout=30):
    conn = http.client.HTTPConnection("127.0.0.1", SHIM_PORT, timeout=timeout)
    t0 = time.time()
    conn.request(method, path, body=body, headers=headers or {})
    resp = conn.getresponse()
    t_headers = time.time() - t0
    data = resp.read()
    total = time.time() - t0
    conn.close()
    return resp.status, dict(resp.getheaders()), data, t_headers, total


def raw(data, timeout=5):
    """Send raw bytes; return everything received until the server closes
    (None if it keeps the connection open past the timeout)."""
    s = socket.create_connection(("127.0.0.1", SHIM_PORT), timeout=timeout)
    s.sendall(data)
    out = b""
    try:
        while chunk := s.recv(65536):
            out += chunk
    except socket.timeout:
        return None
    finally:
        s.close()
    return out


AUTH = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}


def main():
    mock = subprocess.Popen([sys.executable, os.path.join(ROOT, "tests/mock_upstream.py")],
                            env={**os.environ, "MOCK_PORT": str(MOCK_PORT)})
    shim_env = {**os.environ,
                "UPSTREAM_BASE": f"http://127.0.0.1:{MOCK_PORT}",
                "LISTEN_PORT": str(SHIM_PORT),
                "SHIM_API_KEY": KEY,
                "HEARTBEAT_INTERVAL": "1",
                "STREAM_COMMIT_DELAY": "2",
                "NONSTREAM_COMMIT_DELAY": "3"}
    shim = subprocess.Popen([sys.executable, os.path.join(ROOT, "proxy.py")],
                            env=shim_env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    try:
        assert wait_port(MOCK_PORT) and wait_port(SHIM_PORT), "servers did not start"

        s, _, b, _, _ = req("GET", "/health")
        check("health (no auth needed)", s == 200 and b'"ok"' in b, f"status={s}")

        s, _, b, _, _ = req("GET", "/v1/models")
        check("unauthenticated request rejected", s == 401, f"status={s}")

        s, _, b, _, _ = req("GET", "/v1/models", headers=AUTH)
        check("fast GET passthrough", s == 200 and b"qwen-mock" in b, f"status={s}")

        payload = json.dumps({"model": "qwen-mock", "messages": []})
        s, _, b, _, total = req("POST", "/v1/chat/completions", payload, AUTH)
        ok = s == 200 and json.loads(b)["choices"][0]["message"]["content"] == "slow answer"
        check("fast non-stream passthrough", ok, f"status={s} total={total:.1f}s")

        s, _, b, t_head, total = req("POST", "/v1/chat/completions?gen=6", payload, AUTH)
        parsed = json.loads(b)  # leading heartbeat spaces must not break JSON
        ok = (s == 200 and t_head < 4.5 and total >= 6
              and parsed["choices"][0]["message"]["content"] == "slow answer")
        check("slow non-stream: early commit + JSON survives heartbeats", ok,
              f"status={s} headers@{t_head:.1f}s total={total:.1f}s body[:8]={b[:8]!r}")

        spayload = json.dumps({"model": "qwen-mock", "messages": [], "stream": True})
        s, h, b, t_head, total = req("POST", "/v1/chat/completions?prefill=5",
                                     spayload, AUTH)
        ok = (s == 200 and t_head < 3.5 and total >= 5 and b": keepalive" in b
              and b"Hello" in b and b"[DONE]" in b)
        check("slow stream: keepalives before first token, tokens intact", ok,
              f"status={s} headers@{t_head:.1f}s total={total:.1f}s")

        s, _, b, _, _ = req("POST", "/v1/chat/completions?prefill=0", spayload, AUTH)
        ok = s == 200 and b"Hello" in b and b"[DONE]" in b
        check("fast stream passthrough", ok, f"status={s}")

        s, _, b, _, _ = req("POST", "/error", payload, AUTH)
        check("fast upstream error keeps real status", s == 500 and b"mock boom" in b,
              f"status={s}")

        # Security: a 401's unread body must not be parsed as a second request on
        # the reused connection, and chunked uploads are refused outright.
        smuggled = (f"GET /v1/models HTTP/1.1\r\nHost: x\r\n"
                    f"Authorization: Bearer {KEY}\r\n\r\n").encode()
        r1 = raw(b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\n"
                 b"Content-Length: %d\r\n\r\n%s" % (len(smuggled), smuggled))
        r2 = raw(b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\n"
                 b"Authorization: Bearer " + KEY.encode() + b"\r\n"
                 b"Transfer-Encoding: chunked\r\n\r\n"
                 b"5\r\nhello\r\n0\r\n\r\n" + smuggled)
        ok = (r1 is not None and r1.count(b"HTTP/1.1 ") == 1 and r1.startswith(b"HTTP/1.1 401")
              and b"qwen-mock" not in r1 and r2 is not None and r2.count(b"HTTP/1.1 ") == 1
              and r2.startswith(b"HTTP/1.1 400"))
        check("security: no smuggled 2nd response after 401 / chunked upload", ok,
              f"r1={r1[:12] if r1 else r1!r} r2={r2[:12] if r2 else r2!r}")
    finally:
        shim.terminate()
        mock.terminate()
        try:
            out, _ = shim.communicate(timeout=5)
            print("\n--- shim log ---\n" + (out or "")[-1500:])
        except Exception:
            pass

    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
