"""Fake model servers for tests and offline development. Standard library only.

Dialects:
  openai  -> POST /v1/chat/completions   (OpenAI, and also xAI Grok which speaks the same wire format)
  grok    -> same as openai but checks xAI-specific expectations (path /v1, Bearer key, reasoning_content)
  vertex  -> POST /v1/projects/{p}/locations/{l}/publishers/google/models/{m}:streamGenerateContent?alt=sse

Each fake plays a "script": on the first request it returns text + one or more tool calls; on later
requests it echoes the last tool result back as text. Requests are recorded on `server.requests`.

Standalone:  python -m picoagent.testing.fakes --dialect vertex --port 8766
"""
from __future__ import annotations
import argparse, json, threading, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

DEFAULT_SCRIPT = {
    "text": "Checking. ",
    "tool_calls": [{"name": "shell", "args": {"command": "echo from-server"}}],
    "reply": "tool said: {last_tool_result}",
    "usage": {"input": 11, "output": 3},
    "models": ["fake-large", "fake-small"],   # served by GET /v1/models
}


class _Base(BaseHTTPRequestHandler):
    server: "FakeServer"

    def log_message(self, *a: Any) -> None:
        pass

    def _json(self) -> dict:
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"{}")

    def _sse_start(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

    def _sse(self, d: dict) -> None:
        self.wfile.write(f"data: {json.dumps(d)}\n\n".encode()); self.wfile.flush()

    def _fail(self, code: int, msg: str) -> None:
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.end_headers()
        self.wfile.write(json.dumps({"error": {"message": msg}}).encode())


# ---------------------------------------------------------------- OpenAI / Grok --------------
class OpenAIHandler(_Base):
    def do_GET(self) -> None:
        """``GET /v1/models`` - what ``/model list`` and ``provider.list_models()`` call."""
        srv = self.server
        srv.requests.append({"path": self.path, "headers": dict(self.headers), "body": None})
        if not self.path.endswith("/models"):
            return self._fail(404, f"unexpected path {self.path}")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        data = [{"id": name, "object": "model"} for name in srv.script["models"]]
        self.wfile.write(json.dumps({"object": "list", "data": data}).encode())

    def do_POST(self) -> None:
        srv, body = self.server, self._json()
        srv.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
        if not self.path.endswith("/chat/completions"):
            return self._fail(404, f"unexpected path {self.path}")
        if srv.dialect == "grok":
            if not self.headers.get("Authorization", "").startswith("Bearer "):
                return self._fail(401, "xAI requires Bearer key")
            if not self.path.startswith("/v1/"):
                return self._fail(404, "xAI base URL must end in /v1")
        if not body.get("stream"):
            return self._fail(400, "fake only supports stream=true")
        if body["messages"][0]["role"] != "system":
            return self._fail(400, "expected system message first")
        s, n = srv.script, srv.count()
        self._sse_start()
        if n == 1:
            if srv.dialect == "grok":
                self._sse({"choices": [{"delta": {"reasoning_content": "thinking…"}}]})
            self._sse({"choices": [{"delta": {"content": s["text"]}}]})
            for i, tc in enumerate(s["tool_calls"]):
                a = json.dumps(tc["args"]); cut = len(a) // 2
                self._sse({"choices": [{"delta": {"tool_calls": [{"index": i, "id": f"call_{n}_{i}",
                           "function": {"name": tc["name"], "arguments": a[:cut]}}]}}]})
                self._sse({"choices": [{"delta": {"tool_calls": [{"index": i,
                           "function": {"arguments": a[cut:]}}]}}]})
            self._sse({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]})
        else:
            tools = [m for m in body["messages"] if m["role"] == "tool"]
            last = tools[-1]["content"].splitlines()[0] if tools else "(none)"
            self._sse({"choices": [{"delta": {"content": s["reply"].format(last_tool_result=last)}}]})
            self._sse({"choices": [{"delta": {}, "finish_reason": "stop"}]})
        self._sse({"choices": [], "usage": {"prompt_tokens": s["usage"]["input"], "completion_tokens": s["usage"]["output"]}})
        self.wfile.write(b"data: [DONE]\n\n"); self.wfile.flush()


# ---------------------------------------------------------------- Vertex AI (Gemini) --------
class VertexHandler(_Base):
    def do_POST(self) -> None:
        srv, body = self.server, self._json()
        url = urllib.parse.urlparse(self.path)
        srv.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
        if ":streamGenerateContent" not in url.path or "/publishers/google/models/" not in url.path:
            return self._fail(404, f"unexpected path {url.path}")
        if "alt=sse" not in (url.query or ""):
            return self._fail(400, "expected ?alt=sse")
        if not self.headers.get("Authorization", "").startswith("Bearer "):
            return self._fail(401, "Vertex requires OAuth Bearer token")
        if "systemInstruction" not in body or "contents" not in body:
            return self._fail(400, "expected systemInstruction + contents")
        for t in body.get("tools", []):
            for fd in t.get("functionDeclarations", []):
                if "additionalProperties" in json.dumps(fd.get("parameters", {})):
                    return self._fail(400, "Gemini schema rejects additionalProperties")
        s, n = srv.script, srv.count()
        self._sse_start()
        if n == 1:
            self._sse({"candidates": [{"content": {"role": "model", "parts": [{"text": "thinking…", "thought": True}]}}]})
            self._sse({"candidates": [{"content": {"role": "model", "parts": [{"text": s["text"]}]}}]})
            self._sse({"candidates": [{"content": {"role": "model", "parts": [
                {"functionCall": {"name": tc["name"], "args": tc["args"]}} for tc in s["tool_calls"]]},
                "finishReason": "STOP"}]})
        else:
            parts = [p for c in body["contents"] for p in c.get("parts", []) if "functionResponse" in p]
            if not parts:
                return self._fail(400, "expected a functionResponse part on turn 2")
            last = str(parts[-1]["functionResponse"]["response"].get("output", "")).splitlines()[0]
            self._sse({"candidates": [{"content": {"role": "model",
                       "parts": [{"text": s["reply"].format(last_tool_result=last)}]}, "finishReason": "STOP"}]})
        self._sse({"usageMetadata": {"promptTokenCount": s["usage"]["input"], "candidatesTokenCount": s["usage"]["output"]}})
        self.wfile.flush()


HANDLERS = {"openai": OpenAIHandler, "grok": OpenAIHandler, "vertex": VertexHandler}


class FakeServer(ThreadingHTTPServer):
    def __init__(self, dialect: str = "openai", port: int = 0, script: dict | None = None):
        super().__init__(("127.0.0.1", port), HANDLERS[dialect])
        self.dialect = dialect
        self.script = {**DEFAULT_SCRIPT, **(script or {})}
        self.requests: list[dict] = []
        self._n = 0
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def count(self) -> int:
        with self._lock:
            self._n += 1
            return self._n

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def start(self) -> "FakeServer":
        self._thread = threading.Thread(target=self.serve_forever, daemon=True); self._thread.start()
        return self

    def stop(self) -> None:
        self.shutdown(); self.server_close()

    def __enter__(self): return self.start()
    def __exit__(self, *a): self.stop()


def main() -> None:
    ap = argparse.ArgumentParser(); ap.add_argument("--dialect", choices=HANDLERS, default="openai")
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    srv = FakeServer(a.dialect, a.port)
    print(f"fake {a.dialect} server on {srv.url}"); srv.serve_forever()


if __name__ == "__main__":
    main()
