"""A fake Model Context Protocol server over stdio, for tests and offline demos.

Standard library only, and deliberately a real child process rather than an in-process double:
the ``mcp`` plugin's job is spawning a program and talking JSON-RPC 2.0 down a pipe, so a
double that replaced the pipe would test everything except the part that breaks. A process
also gives the failure modes their real shape - exiting mid-conversation, writing to stderr,
never answering - which is what the plugin has to survive.

It speaks protocol revision ``2024-11-05`` and echoes back whichever revision the client
announces when it is one of :data:`SUPPORTED_VERSIONS`. ``tools/list`` and ``tools/call`` are
refused until the client sends ``notifications/initialized``, so a client that skips half the
handshake fails the tests rather than passing them by accident.

Tools advertised in ``serve`` mode:

    echo      returns the text it was given
    read      collides with picoagent's built-in ``read`` on purpose
    slow      sleeps ``seconds`` before answering, for timeout tests
    boom      always answers with a JSON-RPC error
    fail      answers with ``isError`` content: the tool ran and failed
    scalar    advertises a non-object ``inputSchema``, which no provider can call

Modes:

    serve             the above; exits when stdin closes
    exit-immediately  writes one line to stderr and exits 1 without reading stdin
    exit-after-list   answers initialize and tools/list, then exits before any tools/call

Standalone::

    python -m picoagent.testing.fake_mcp --mode serve
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

PROTOCOL_VERSION = "2024-11-05"
SUPPORTED_VERSIONS = (PROTOCOL_VERSION, "2025-03-26", "2025-06-18")
SERVER_INFO = {"name": "fake-mcp", "version": "0.1.0"}

#: Advertised tools, in the shape ``tools/list`` returns: name, description, inputSchema.
TOOLS: list[dict[str, Any]] = [
    {"name": "echo", "description": "Return the text you send.",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "read", "description": "Read a note by path. Named to collide with the built-in read tool.",
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "slow", "description": "Sleep, then answer.",
     "inputSchema": {"type": "object", "properties": {"seconds": {"type": "number"}}}},
    {"name": "boom", "description": "Always answers with a JSON-RPC error.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "fail", "description": "Runs and reports failure through isError.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "scalar", "description": "Takes a bare string, which no function-calling API can express.",
     "inputSchema": {"type": "string"}},
]


def text_result(text: str, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


class JsonRpcError(Exception):
    """Raised by a handler to answer with a JSON-RPC error object instead of a result."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def call_tool(params: dict) -> dict:
    name = params.get("name")
    arguments = params.get("arguments") or {}
    if name == "echo":
        return text_result(str(arguments.get("text", "")))
    if name == "read":
        return text_result(f"fake-mcp note at {arguments.get('path')}")
    if name == "slow":
        time.sleep(float(arguments.get("seconds", 5)))
        return text_result("awake")
    if name == "boom":
        raise JsonRpcError(-32602, "boom: the server refused this call")
    if name == "fail":
        return text_result("the tool ran and failed", is_error=True)
    if name == "scalar":
        return text_result("scalar tool ran")
    raise JsonRpcError(-32602, f"unknown tool: {name}")


class FakeMcpServer:
    """Reads requests from ``stdin``, writes responses to ``stdout``, one JSON object per line."""

    def __init__(self, mode: str = "serve"):
        self.mode = mode
        self.initialized = False

    def run(self) -> int:
        if self.mode == "exit-immediately":
            print("fake-mcp: refusing to start", file=sys.stderr, flush=True)
            return 1
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if self.handle(message) == "stop":
                return 0
        return 0

    def handle(self, message: dict) -> str | None:
        """Answer one message. Returns ``"stop"`` when the configured mode wants to exit now."""
        ident, method = message.get("id"), message.get("method")
        if ident is None:                       # a notification: acknowledge nothing, per JSON-RPC
            if method == "notifications/initialized":
                self.initialized = True
            return None
        try:
            self.respond(ident, result=self.dispatch(method, message.get("params") or {}))
        except JsonRpcError as exc:
            self.respond(ident, error={"code": exc.code, "message": exc.message})
        if self.mode == "exit-after-list" and method == "tools/list":
            return "stop"
        return None

    def dispatch(self, method: str | None, params: dict) -> Any:
        if method == "initialize":
            wanted = params.get("protocolVersion")
            return {"protocolVersion": wanted if wanted in SUPPORTED_VERSIONS else PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}}, "serverInfo": SERVER_INFO}
        if method in ("tools/list", "tools/call"):
            # Refusing until the client has sent ``notifications/initialized`` is what makes
            # the handshake load-bearing here: a client that skipped it gets nothing.
            if not self.initialized:
                raise JsonRpcError(-32002, f"{method} before notifications/initialized")
            return {"tools": TOOLS} if method == "tools/list" else call_tool(params)
        raise JsonRpcError(-32601, f"method not found: {method}")

    def respond(self, ident: Any, result: Any = None, error: dict | None = None) -> None:
        message = {"jsonrpc": "2.0", "id": ident}
        message.update({"error": error} if error is not None else {"result": result})
        sys.stdout.write(json.dumps(message) + "\n")
        sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fake MCP server over stdio")
    parser.add_argument("--mode", default="serve",
                        choices=["serve", "exit-immediately", "exit-after-list"])
    args = parser.parse_args(argv)
    return FakeMcpServer(args.mode).run()


if __name__ == "__main__":
    sys.exit(main())
