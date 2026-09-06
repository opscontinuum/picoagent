"""stdio transport for the Model Context Protocol: one child process, newline-delimited
JSON-RPC 2.0, one reader thread.

Protocol revision targeted: **2024-11-05**. That is what ``initialize`` announces (a server
entry can override it with ``protocol_version``). The four messages this client uses -
``initialize``, ``notifications/initialized``, ``tools/list`` and ``tools/call`` - keep the
same shape in the later revisions, which is why announcing the oldest widely implemented one
costs little: a server that supports it answers with it, and its tools work the same. When a
server answers with a *different* version we carry on and record what it said instead of
disconnecting; the README says why that deviation is deliberate.

Framing rules this module depends on: messages are UTF-8 JSON, one per line, and no message
contains an embedded newline (``json.dumps`` escapes them, so that holds by construction).
The child's stdout carries protocol traffic only, so a line that is not JSON is not a message;
stderr is log output, which is drained here and kept as a tail for error messages, because
"the server is not installed" and "the server crashed on startup" are both stderr stories.

Everything expected raises :class:`McpError`, which the tool layer turns into an error result.
"""
from __future__ import annotations

import itertools
import json
import logging
import os
import queue
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("mcp")

#: The revision announced in ``initialize`` unless a server entry overrides it.
PROTOCOL_VERSION = "2024-11-05"
CLIENT_INFO = {"name": "picoagent-mcp", "version": "0.1.0"}
#: Bound on ``tools/list`` pagination, so a server that keeps handing back a cursor cannot
#: spin the loop for ever.
MAX_TOOL_PAGES = 50
#: How often a waiting call wakes up to check its deadline and the abort flag.
POLL_SECONDS = 0.05


class McpError(Exception):
    """An MCP server refused, never answered, or died. Expected: tools turn it into a result."""


@dataclass
class ServerSpec:
    """One entry from ``[plugins.mcp.servers.<name>]``."""
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    timeout: float = 30.0             # per tools/call
    startup_timeout: float = 20.0     # for the initialize handshake and tools/list
    protocol_version: str = PROTOCOL_VERSION


class StdioServer:
    """A running MCP server: the child process, its reader thread, and blocking calls.

    Blocking on purpose. picoagent executes tools inside whatever event loop is current, and
    that is not one loop for the life of the process (the tests drive each call through its own
    ``asyncio.run``), so an ``asyncio.subprocess`` pipe bound to the loop that spawned it would
    be unusable from the next one. A plain ``Popen`` plus a reader thread belongs to the
    process rather than to a loop; the tool wrapper hands the blocking wait to
    ``asyncio.to_thread`` so the agent's loop stays free.
    """

    def __init__(self, spec: ServerSpec, cwd: Path | None = None):
        self.spec = spec
        self.name = spec.name
        self.cwd = Path(spec.cwd) if spec.cwd else cwd
        self.server_info: dict[str, Any] = {}
        self.negotiated_version: str = ""
        self._proc: subprocess.Popen | None = None
        self._ids = itertools.count(1)
        self._pending: dict[int, queue.SimpleQueue] = {}
        self._pending_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._stderr_tail: deque[str] = deque(maxlen=20)
        self._stdout_closed = False

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> dict:
        """Spawn the child and complete the handshake. Returns the ``initialize`` result.

        The order is fixed by the protocol: the ``initialize`` request, then the
        ``notifications/initialized`` notification, and only then anything else.
        """
        try:
            self._proc = subprocess.Popen(
                [self.spec.command, *self.spec.args], cwd=str(self.cwd) if self.cwd else None,
                env={**os.environ, **self.spec.env}, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", bufsize=1)
        except OSError as exc:
            # The "server is not installed" path: no binary, no permission, no such directory.
            raise McpError(f"MCP server '{self.name}': cannot run {self.spec.command!r} ({exc})") from exc
        threading.Thread(target=self._read_stdout, name=f"mcp-{self.name}-out", daemon=True).start()
        threading.Thread(target=self._read_stderr, name=f"mcp-{self.name}-err", daemon=True).start()

        result = self.request("initialize", {
            "protocolVersion": self.spec.protocol_version,
            # No optional client feature is implemented, and an empty object says exactly that.
            "capabilities": {},
            "clientInfo": CLIENT_INFO,
        }, timeout=self.spec.startup_timeout)
        if not isinstance(result, dict):
            raise McpError(f"MCP server '{self.name}': initialize returned "
                           f"{type(result).__name__}, not an object")
        self.server_info = result.get("serverInfo") or {}
        self.negotiated_version = str(result.get("protocolVersion") or "")
        if self.negotiated_version and self.negotiated_version != self.spec.protocol_version:
            log.warning("mcp: server '%s' answered protocol %s, we announced %s; continuing, because "
                        "tools/list and tools/call have the same shape in both", self.name,
                        self.negotiated_version, self.spec.protocol_version)
        self.notify("notifications/initialized", {})
        return result

    def close(self) -> None:
        """Shut the child down the way the transport prescribes: close stdin, then signal.

        Closing stdin is the intended stop signal for a stdio server, so a well-behaved one
        exits on its own; terminate and kill are for the one that does not.
        """
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
        except OSError:
            pass
        for stop, grace in ((None, 1), (proc.terminate, 2), (proc.kill, 2)):
            if stop is not None:
                stop()
            try:
                proc.wait(timeout=grace)
                break
            except subprocess.TimeoutExpired:
                continue
        else:
            log.warning("mcp: server '%s' survived being killed", self.name)
        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except OSError:
                    pass

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def stderr_tail(self) -> str:
        return " | ".join(line for line in self._stderr_tail if line)

    # ------------------------------------------------------------------ MCP methods
    def list_tools(self, timeout: float | None = None) -> list[dict]:
        """Every advertised tool, following ``nextCursor`` pagination to the end."""
        tools: list[dict] = []
        cursor: str | None = None
        for _ in range(MAX_TOOL_PAGES):
            params = {"cursor": cursor} if cursor else {}
            page = self.request("tools/list", params, timeout=timeout or self.spec.startup_timeout)
            if not isinstance(page, dict):
                raise McpError(f"MCP server '{self.name}': tools/list did not return an object")
            tools += [item for item in (page.get("tools") or []) if isinstance(item, dict)]
            cursor = page.get("nextCursor")
            if not cursor:
                return tools
        log.warning("mcp: server '%s' kept paginating tools/list past %d pages", self.name, MAX_TOOL_PAGES)
        return tools

    def call_tool(self, name: str, arguments: dict, timeout: float | None = None,
                  cancelled: Callable[[], bool] | None = None) -> Any:
        """``tools/call``. Returns the result object; a JSON-RPC error becomes ``McpError``."""
        return self.request("tools/call", {"name": name, "arguments": arguments or {}},
                            timeout=timeout or self.spec.timeout, cancelled=cancelled)

    # ------------------------------------------------------------------ JSON-RPC
    def request(self, method: str, params: dict, timeout: float,
                cancelled: Callable[[], bool] | None = None) -> Any:
        """Send a request and wait for its response, or raise ``McpError`` trying."""
        ident = next(self._ids)
        waiter: queue.SimpleQueue = queue.SimpleQueue()
        with self._pending_lock:
            if self._stdout_closed:
                raise McpError(self._gone_message(method))
            self._pending[ident] = waiter
        try:
            self._send({"jsonrpc": "2.0", "id": ident, "method": method, "params": params})
            message = self._await_response(ident, method, timeout, cancelled, waiter)
        finally:
            self._forget(ident)
        if "error" in message:
            error = message["error"] if isinstance(message["error"], dict) else {"message": message["error"]}
            raise McpError(f"MCP server '{self.name}': {method} failed with JSON-RPC error "
                           f"{error.get('code', '?')}: {error.get('message', 'no message')}")
        return message.get("result")

    def notify(self, method: str, params: dict) -> None:
        """Fire-and-forget message (no id, so no response). Used only by the handshake."""
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _await_response(self, ident: int, method: str, timeout: float,
                        cancelled: Callable[[], bool] | None, waiter: queue.SimpleQueue) -> dict:
        """Wait in short slices, so a hung server hits the deadline and an abort is noticed.

        A timed-out request id is abandoned rather than cancelled: ``notifications/cancelled``
        is out of scope here, and the reader drops responses nobody is waiting for.
        """
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise McpError(f"MCP server '{self.name}': no response to {method} within "
                               f"{timeout:g}s{self._context_suffix()}")
            if cancelled is not None and cancelled():
                raise McpError(f"MCP server '{self.name}': {method} abandoned, the run was cancelled")
            try:
                message = waiter.get(timeout=min(POLL_SECONDS, remaining))
            except queue.Empty:
                continue
            if message is None:                      # the reader thread saw EOF
                raise McpError(self._gone_message(method))
            return message

    def _send(self, payload: dict) -> None:
        line = json.dumps(payload) + "\n"
        with self._write_lock:
            proc = self._proc
            if proc is None or proc.stdin is None or proc.poll() is not None:
                raise McpError(self._gone_message(payload.get("method", "?")))
            try:
                proc.stdin.write(line)
                proc.stdin.flush()
            except (OSError, ValueError) as exc:
                raise McpError(f"MCP server '{self.name}': cannot write {payload.get('method')} "
                               f"({exc}){self._context_suffix()}") from exc

    def _forget(self, ident: int) -> None:
        with self._pending_lock:
            self._pending.pop(ident, None)

    def _gone_message(self, method: str) -> str:
        return f"MCP server '{self.name}': exited before answering {method}{self._context_suffix()}"

    def _context_suffix(self) -> str:
        """Exit code and the tail of stderr - the two things that say what actually happened."""
        proc = self._proc
        code = proc.poll() if proc is not None else None
        parts = [f"exit code {code}"] if code is not None else []
        if self.stderr_tail():
            parts.append(f"stderr: {self.stderr_tail()[-400:]}")
        return f" ({'; '.join(parts)})" if parts else ""

    # ------------------------------------------------------------------ reader threads
    def _read_stdout(self) -> None:
        """Demultiplex responses by id until the pipe closes, then release every waiter.

        Releasing on EOF is what stops a dead server hanging the agent for a whole timeout: the
        call fails as soon as the process is gone, not when the clock runs out.
        """
        proc = self._proc
        try:
            for line in proc.stdout:                      # type: ignore[union-attr]
                self._dispatch(line.strip())
        except (OSError, ValueError) as exc:
            log.debug("mcp: server '%s' stdout closed: %s", self.name, exc)
        with self._pending_lock:
            self._stdout_closed = True
            pending, self._pending = self._pending, {}
        for waiter in pending.values():
            waiter.put(None)

    def _dispatch(self, line: str) -> None:
        if not line:
            return
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            # A server that prints a banner on stdout is out of spec but not rare; a line that
            # is not JSON cannot be a message, so log it and keep reading rather than tear down.
            log.debug("mcp: server '%s' wrote a non-JSON line: %.200s", self.name, line)
            return
        if not isinstance(message, dict) or message.get("id") is None:
            # Notifications and server-to-client requests are out of scope for this client.
            log.debug("mcp: server '%s' sent an unhandled %s", self.name,
                      message.get("method") if isinstance(message, dict) else type(message).__name__)
            return
        with self._pending_lock:
            waiter = self._pending.get(message["id"])
        if waiter is None:
            log.debug("mcp: server '%s' answered id %s that nobody waits for", self.name, message["id"])
            return
        waiter.put(message)

    def _read_stderr(self) -> None:
        proc = self._proc
        try:
            for line in proc.stderr:                      # type: ignore[union-attr]
                stripped = line.rstrip()
                self._stderr_tail.append(stripped)
                log.debug("mcp[%s] %s", self.name, stripped)
        except (OSError, ValueError):
            pass
