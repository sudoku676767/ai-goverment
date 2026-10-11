"""Pass-through recording proxy.

Point an agent harness at this proxy instead of the model provider:

    DEEPSEEK_BASE_URL=http://127.0.0.1:8787

Every request is forwarded unchanged (apart from transport-level headers) to
the upstream API. Streaming (SSE) responses are relayed chunk by chunk, so
the agent sees no extra latency beyond the local hop.

What gets recorded, per request:
  request         method, path, redacted headers and body, body hash
  response_start  status and time-to-headers, written as soon as headers
                  arrive so a crash mid-stream still leaves a trace
  response_end    full (redacted) body or SSE stream, timings, normalised
                  usage, tool calls, finish reasons
  error           upstream or client failures

Only the standard library is used, on purpose: an auditing tool should not
add its own supply-chain risk.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import platform
import ssl
import sys
import time
import uuid
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

from . import __version__
from .parse import ResponseAnalysis
from .recorder import Recorder
from .redact import redact_headers, redact_text, redact_value

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "trailers",
    "transfer-encoding",
    "upgrade",
}

DEFAULT_MAX_RECORD_BYTES = 50 * 1024 * 1024


class Upstream:
    def __init__(self, url: str, timeout: float = 600.0):
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            raise ValueError(f"upstream must be http(s)://..., got {url!r}")
        self.url = url
        self.scheme = parts.scheme
        self.host = parts.hostname or ""
        self.port = parts.port or (443 if parts.scheme == "https" else 80)
        self.base_path = parts.path.rstrip("/")
        self.timeout = timeout

    def connect(self) -> http.client.HTTPConnection:
        if self.scheme == "https":
            return http.client.HTTPSConnection(
                self.host, self.port, timeout=self.timeout, context=ssl.create_default_context()
            )
        return http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)

    @property
    def host_header(self) -> str:
        default = 443 if self.scheme == "https" else 80
        return self.host if self.port == default else f"{self.host}:{self.port}"


def _decode_for_log(data: bytes, encoding: str | None) -> bytes:
    """Undo gzip/deflate for the log copy only; forwarded bytes stay as-is."""
    enc = (encoding or "identity").lower()
    try:
        if enc == "gzip":
            return zlib.decompress(data, 16 + zlib.MAX_WBITS)
        if enc == "deflate":
            return zlib.decompress(data)
    except zlib.error:
        pass
    return data


def _body_for_log(data: bytes) -> dict[str, Any]:
    text = data.decode("utf-8", "replace")
    try:
        return {"json": redact_value(json.loads(text))}
    except (json.JSONDecodeError, ValueError):
        return {"text": redact_text(text)}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "skill-blackbox/" + __version__

    # silence default stderr access log; the recorder is the log
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D401
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    def do_GET(self) -> None:
        self._forward()

    def do_POST(self) -> None:
        self._forward()

    def do_PUT(self) -> None:
        self._forward()

    def do_PATCH(self) -> None:
        self._forward()

    def do_DELETE(self) -> None:
        self._forward()

    def do_OPTIONS(self) -> None:
        self._forward()

    # ------------------------------------------------------------------
    def _read_body(self) -> bytes:
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            parts = []
            while True:
                size_line = self.rfile.readline()
                size = int(size_line.split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    # consume trailers
                    while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                        pass
                    break
                parts.append(self.rfile.read(size))
                self.rfile.readline()
            return b"".join(parts)
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _write_chunk(self, data: bytes) -> None:
        self.wfile.write(f"{len(data):X}\r\n".encode("ascii") + data + b"\r\n")
        self.wfile.flush()

    def _forward(self) -> None:
        bb: BlackboxServer = self.server  # type: ignore[assignment]
        rec = bb.recorder
        req_id = uuid.uuid4().hex[:12]
        t0 = time.monotonic()

        body = self._read_body()
        req_headers = {k: v for k, v in self.headers.items()}

        rec.write(
            "request",
            req_id=req_id,
            method=self.command,
            path=self.path,
            headers=redact_headers(req_headers),
            body_bytes=len(body),
            body_sha256=hashlib.sha256(body).hexdigest(),
            body=_body_for_log(body) if body else None,
            client=self.client_address[0],
        )

        # ---- build upstream request ---------------------------------
        up = bb.upstream
        fwd_headers = {
            k: v
            for k, v in req_headers.items()
            if k.lower() not in HOP_BY_HOP | {"host", "content-length", "accept-encoding"}
        }
        fwd_headers["Host"] = up.host_header
        fwd_headers["Accept-Encoding"] = "identity"
        if body:
            fwd_headers["Content-Length"] = str(len(body))

        conn = up.connect()
        try:
            conn.request(self.command, up.base_path + self.path, body=body or None, headers=fwd_headers)
            resp = conn.getresponse()
        except Exception as exc:  # upstream unreachable / TLS / timeout
            rec.write(
                "error",
                req_id=req_id,
                stage="upstream_connect",
                error=f"{type(exc).__name__}: {exc}",
                duration_ms=round((time.monotonic() - t0) * 1000, 1),
            )
            payload = json.dumps({"error": {"message": f"blackbox: upstream error: {exc}"}}).encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            conn.close()
            return

        headers_ms = round((time.monotonic() - t0) * 1000, 1)
        resp_headers = {k: v for k, v in resp.getheaders()}
        ctype = (resp.getheader("Content-Type") or "").lower()
        streamed = "text/event-stream" in ctype or (
            resp.getheader("Content-Length") is None and resp.chunked
        )

        rec.write(
            "response_start",
            req_id=req_id,
            status=resp.status,
            streamed=streamed,
            headers_ms=headers_ms,
        )

        captured: list[bytes] = []
        captured_bytes = 0
        total_bytes = 0
        truncated = False
        first_byte_ms: float | None = None
        abort: str | None = None

        def capture(chunk: bytes) -> None:
            nonlocal captured_bytes, truncated
            if captured_bytes + len(chunk) <= bb.max_record_bytes:
                captured.append(chunk)
                captured_bytes += len(chunk)
            else:
                truncated = True

        try:
            self.send_response(resp.status, resp.reason)
            for k, v in resp_headers.items():
                if k.lower() in HOP_BY_HOP | {"content-length", "server", "date"}:
                    continue
                self.send_header(k, v)

            if streamed:
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                while True:
                    chunk = resp.read1(65536)
                    if not chunk:
                        break
                    if first_byte_ms is None:
                        first_byte_ms = round((time.monotonic() - t0) * 1000, 1)
                    total_bytes += len(chunk)
                    capture(chunk)
                    self._write_chunk(chunk)
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            else:
                data = resp.read()
                first_byte_ms = headers_ms
                total_bytes = len(data)
                capture(data)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError) as exc:
            abort = f"client_disconnected: {type(exc).__name__}"
            self.close_connection = True
        except Exception as exc:  # upstream died mid-stream, timeout, ...
            abort = f"relay_failed: {type(exc).__name__}: {exc}"
            self.close_connection = True
        finally:
            conn.close()

            raw = _decode_for_log(b"".join(captured), resp.getheader("Content-Encoding"))
            analysis = ResponseAnalysis()
            text = raw.decode("utf-8", "replace")
            body_log: dict[str, Any] | None
            if streamed:
                analysis.feed_sse(text)
                body_log = {"sse": redact_text(text)}
            else:
                body_log = _body_for_log(raw) if raw else None
                if body_log and "json" in body_log:
                    try:
                        analysis.feed_json(json.loads(text), streamed=False)
                    except json.JSONDecodeError:
                        pass

            rec.write(
                "response_end",
                req_id=req_id,
                status=resp.status,
                streamed=streamed,
                headers=redact_headers(resp_headers),
                headers_ms=headers_ms,
                first_byte_ms=first_byte_ms,
                duration_ms=round((time.monotonic() - t0) * 1000, 1),
                bytes=total_bytes,
                truncated=truncated,
                aborted=abort,
                body=body_log,
                analysis=redact_value(analysis.summary()),
            )


class BlackboxServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        listen: tuple[str, int],
        upstream: Upstream,
        recorder: Recorder,
        max_record_bytes: int = DEFAULT_MAX_RECORD_BYTES,
        verbose: bool = False,
    ):
        super().__init__(listen, _Handler)
        self.upstream = upstream
        self.recorder = recorder
        self.max_record_bytes = max_record_bytes
        self.verbose = verbose

    def record_session_start(self) -> None:
        host, port = self.server_address[:2]
        self.recorder.write(
            "session_start",
            blackbox_version=__version__,
            upstream=self.upstream.url,
            listen=f"{host}:{port}",
            pid=os.getpid(),
            python=sys.version.split()[0],
            platform=platform.platform(),
        )

    def record_session_end(self, reason: str) -> dict[str, Any]:
        return self.recorder.write("session_end", reason=reason)
