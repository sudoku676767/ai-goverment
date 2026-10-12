"""End-to-end tests: fake upstream <- blackbox proxy <- http client.

Run with:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import http.client
import json
import socket
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from blackbox.proxy import BlackboxServer, Upstream
from blackbox.recorder import Recorder
from blackbox.summary import summarise
from blackbox.verify import verify_log

FAKE_KEY = "sk-test0123456789abcdefABCDEF"


class FakeUpstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen: list[dict] = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        body = json.dumps({"data": [{"id": "deepseek-v4-pro"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        req = json.loads(self.rfile.read(length))
        FakeUpstream.seen.append({"headers": dict(self.headers), "body": req})

        if req.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            events = [
                {"model": "deepseek-v4-pro", "choices": [{"index": 0, "delta": {"reasoning_content": "think"}}]},
                {"choices": [{"index": 0, "delta": {"content": "Hel"}}]},
                {"choices": [{"index": 0, "delta": {"content": "lo"}}]},
                {"choices": [{"index": 0, "delta": {"tool_calls": [
                    {"index": 0, "function": {"name": "bash", "arguments": "{\"cmd\":"}}]}}]},
                {"choices": [{"index": 0, "delta": {"tool_calls": [
                    {"index": 0, "function": {"arguments": "\"ls\"}"}}]}, "finish_reason": "tool_calls"}]},
                {"choices": [], "usage": {"prompt_tokens": 120, "completion_tokens": 30,
                                          "prompt_cache_hit_tokens": 100,
                                          "completion_tokens_details": {"reasoning_tokens": 12}}},
            ]
            for ev in events:
                data = f"data: {json.dumps(ev)}\n\n".encode()
                self.wfile.write(f"{len(data):X}\r\n".encode() + data + b"\r\n")
                self.wfile.flush()
                time.sleep(0.01)
            done = b"data: [DONE]\n\n"
            self.wfile.write(f"{len(done):X}\r\n".encode() + done + b"\r\n0\r\n\r\n")
            self.wfile.flush()
            return

        body = json.dumps({
            "model": "deepseek-v4-pro",
            "choices": [{
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "content": "ok",
                    "tool_calls": [
                        {"function": {"name": "read_file", "arguments": "{\"path\":\"a.py\"}"}},
                        {"function": {"name": "read_file", "arguments": "{\"path\":\"a.py\"}"}},
                    ],
                },
            }],
            "usage": {"prompt_tokens": 50, "completion_tokens": 10},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _serve(server):
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return t


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ProxyTests(unittest.TestCase):
    def setUp(self):
        FakeUpstream.seen = []
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Path(self.tmp.name) / "logs" / "t.jsonl"

        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream)
        _serve(self.upstream)
        up_url = f"http://127.0.0.1:{self.upstream.server_address[1]}"

        self.recorder = Recorder(self.log, session="t")
        self.proxy = BlackboxServer(("127.0.0.1", 0), Upstream(up_url), self.recorder)
        self.proxy.record_session_start()
        _serve(self.proxy)
        self.port = self.proxy.server_address[1]

    def tearDown(self):
        self.proxy.shutdown()
        self.proxy.server_close()
        self.upstream.shutdown()
        self.upstream.server_close()
        self.recorder.close()
        self.tmp.cleanup()

    def _post(self, payload, path="/chat/completions"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", path, body=json.dumps(payload),
                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {FAKE_KEY}"})
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp, data

    def _records(self):
        return [json.loads(l) for l in self.log.read_text().splitlines() if l.strip()]

    # ------------------------------------------------------------------
    def test_non_streaming_passthrough_and_analysis(self):
        resp, data = self._post({"model": "m", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(resp.status, 200)
        self.assertEqual(json.loads(data)["choices"][0]["message"]["content"], "ok")

        # upstream received the real key: forwarding is not altered
        self.assertEqual(FakeUpstream.seen[0]["headers"]["Authorization"], f"Bearer {FAKE_KEY}")

        end = [r for r in self._records() if r["event"] == "response_end"][0]
        a = end["analysis"]
        self.assertEqual(a["usage"]["input_tokens"], 50)
        self.assertEqual([t["name"] for t in a["tool_calls"]], ["read_file", "read_file"])
        self.assertEqual(a["finish_reasons"], ["tool_calls"])

    def test_streaming_relayed_and_reassembled(self):
        resp, data = self._post({"model": "m", "stream": True, "messages": []})
        self.assertEqual(resp.status, 200)
        text = data.decode()
        self.assertIn('"Hel"', text)
        self.assertIn("[DONE]", text)

        recs = self._records()
        start = [r for r in recs if r["event"] == "response_start"][0]
        end = [r for r in recs if r["event"] == "response_end"][0]
        self.assertTrue(start["streamed"])
        a = end["analysis"]
        self.assertEqual(a["usage"]["input_tokens"], 120)
        self.assertEqual(a["usage"]["cached_input_tokens"], 100)
        self.assertEqual(a["usage"]["reasoning_tokens"], 12)
        self.assertEqual(a["tool_calls"], [{"name": "bash", "arguments": '{"cmd":"ls"}'}])
        self.assertEqual(a["text_chars"], 5)
        self.assertEqual(a["reasoning_chars"], 5)
        self.assertIsNotNone(end["first_byte_ms"])

    def test_secrets_never_reach_the_log(self):
        self._post({"model": "m", "messages": [{"role": "user", "content": f"my key is {FAKE_KEY}"}]})
        raw = self.log.read_text()
        self.assertNotIn(FAKE_KEY, raw)
        self.assertIn("[REDACTED:", raw)

    def test_get_models(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/models")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        self.assertIn("deepseek-v4-pro", resp.read().decode())
        conn.close()

    def test_hash_chain_verifies_and_detects_tampering(self):
        self._post({"model": "m", "messages": []})
        self.assertTrue(verify_log(self.log)["ok"])

        lines = self.log.read_text().splitlines()
        rec = json.loads(lines[1])
        rec["path"] = "/something-else"
        lines[1] = json.dumps(rec)
        tampered = Path(self.tmp.name) / "tampered.jsonl"
        tampered.write_text("\n".join(lines) + "\n")
        result = verify_log(tampered)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["seq"], 2)

        deleted = Path(self.tmp.name) / "deleted.jsonl"
        deleted.write_text("\n".join(lines[:1] + lines[2:]) + "\n")
        self.assertFalse(verify_log(deleted)["ok"])

    def test_summary_counts_repeats(self):
        tools = [{"type": "function", "function": {"name": "bash"}}]
        self._post({"model": "m", "messages": [], "tools": tools})
        self._post({"model": "m", "stream": True, "messages": [], "tools": tools})
        s = summarise(self.log)
        t = s["totals"]["agent"]
        self.assertEqual(t["turns"], 2)
        self.assertEqual(t["input_tokens"], 170)
        self.assertEqual(t["tool_calls"], 3)
        self.assertEqual(t["repeated_identical_tool_calls"], 1)
        self.assertEqual(s["tool_calls_by_name"], {"read_file": 2, "bash": 1})

    def test_log_file_permissions(self):
        self.assertEqual(self.log.stat().st_mode & 0o777, 0o600)


class FailureTests(unittest.TestCase):
    def test_unreachable_upstream_gives_502_and_error_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "e.jsonl"
            rec = Recorder(log, session="e")
            dead = f"http://127.0.0.1:{_free_port()}"
            proxy = BlackboxServer(("127.0.0.1", 0), Upstream(dead, timeout=2), rec)
            _serve(proxy)
            conn = http.client.HTTPConnection("127.0.0.1", proxy.server_address[1], timeout=10)
            conn.request("POST", "/chat/completions", body="{}", headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            resp.read()
            conn.close()
            self.assertEqual(resp.status, 502)
            proxy.shutdown()
            proxy.server_close()
            rec.close()
            events = [json.loads(l)["event"] for l in log.read_text().splitlines()]
            self.assertEqual(events, ["request", "error"])

    def test_resume_continues_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "r.jsonl"
            r1 = Recorder(log, session="r")
            r1.write("note", n=1)
            r1.close()
            r2 = Recorder(log, session="r")
            r2.write("note", n=2)
            r2.close()
            result = verify_log(log)
            self.assertTrue(result["ok"])
            self.assertEqual(result["records"], 2)


if __name__ == "__main__":
    unittest.main()
