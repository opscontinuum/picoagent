"""mcp - expose the tools of Model Context Protocol servers as picoagent tools.

Scope: the **stdio** transport and the tool half of the protocol, against protocol revision
**2024-11-05**. Each configured server is a child process; this plugin performs the
``initialize`` handshake, asks for ``tools/list``, registers what comes back, and runs
``tools/call`` when the model picks one. HTTP transports, resources, prompts, sampling and
notifications are not implemented - see the README.

Two things this file is careful about, because both are silent when wrong:

* **Names are namespaced.** A picoagent registry replaces on name, so a server advertising a
  tool called ``read`` would take the built-in ``read`` away from the model without anything
  being logged as an error. Every registered tool is ``<server>_<tool>``, and a name that is
  already taken is refused rather than overridden.
* **Schemas are translated, not forwarded.** ``inputSchema`` is a whole JSON Schema; picoagent
  hands ``parameters`` to a provider's function-calling API, which needs a top-level object
  schema. Anything else is refused at registration, where the reason can be reported, rather
  than at the provider, where it is an opaque HTTP 400 on an unrelated turn.

Configuration (``[plugins.mcp]`` **in your own ~/.picoagent/config.toml**; a server named by a
repository's ``.picoagent/config.toml`` is refused, because connecting one runs its command)::

    [plugins.mcp]
    timeout = 30                      # seconds per tools/call; per-server override below
    startup_timeout = 20              # seconds for the handshake and tools/list

    [plugins.mcp.servers.notes]
    command = "python3"
    args = ["-m", "my_notes_server"]
    env = { NOTES_DIR = "/srv/notes" }   # merged over the agent's environment
    # cwd = "/srv/notes"                 # default: the project directory
    # timeout = 60
    # protocol_version = "2024-11-05"
"""
from __future__ import annotations

import asyncio
import atexit
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from mcp_stdio import PROTOCOL_VERSION, McpError, ServerSpec, StdioServer

from picoagent.core.tools import truncate
from picoagent.core.types import ToolResult

log = logging.getLogger("mcp")

DEFAULT_TIMEOUT = 30.0
DEFAULT_STARTUP_TIMEOUT = 20.0
#: The only ``[plugins.mcp]`` keys read from a repository's config. Both say how long to wait;
#: neither says what to run. ``servers`` is deliberately absent - see :func:`register`.
#:
#: Each carries its default, which is also the shape the repository's value must have. Without
#: that, ``timeout = "soon"`` reached ``float()`` in :func:`server_specs` and took ``register()``
#: down with it, so a repository could delete the user's own MCP servers by mistyping a number.
PROJECT_SETTABLE = {"timeout": DEFAULT_TIMEOUT, "startup_timeout": DEFAULT_STARTUP_TIMEOUT}
#: What OpenAI-style function names accept, and the narrowest of the rules across providers.
UNSAFE_IN_NAME = re.compile(r"[^a-zA-Z0-9_-]")
MAX_NAME_LENGTH = 64

#: Every server this process started, so exiting does not leave orphans behind.
_OPEN: list[StdioServer] = []


# ------------------------------------------------------------------ configuration

def server_specs(cfg: dict) -> list[ServerSpec]:
    """Build a :class:`ServerSpec` per configured server.

    Both TOML spellings are accepted, because both are natural to write and a config that is
    silently ignored is worse than either: a table per server
    (``[plugins.mcp.servers.notes]``, the name is the key) or an array of tables
    (``[[plugins.mcp.servers]]`` with ``name = "notes"``).
    """
    raw = cfg.get("servers") or {}
    entries = list(raw.items()) if isinstance(raw, dict) else \
        [(str(entry.get("name", "")), entry) for entry in raw if isinstance(entry, dict)]
    specs = []
    for name, entry in entries:
        if not name or not isinstance(entry, dict) or not entry.get("command"):
            log.warning("mcp: server %r has no name or no command; skipped", name)
            continue
        specs.append(ServerSpec(
            name=name, command=str(entry["command"]),
            args=[str(arg) for arg in entry.get("args", [])],
            env={str(key): str(value) for key, value in (entry.get("env") or {}).items()},
            cwd=entry.get("cwd"),
            timeout=float(entry.get("timeout", cfg.get("timeout", DEFAULT_TIMEOUT))),
            startup_timeout=float(entry.get("startup_timeout",
                                            cfg.get("startup_timeout", DEFAULT_STARTUP_TIMEOUT))),
            protocol_version=str(entry.get("protocol_version",
                                           cfg.get("protocol_version", PROTOCOL_VERSION)))))
    return specs


# ------------------------------------------------------------------ translation

def tool_name(server: str, tool: str) -> str:
    """``<server>_<tool>``, reduced to what a function-calling API accepts.

    Letters, digits, underscore and hyphen, 64 characters. A server is free to name a tool
    ``search.files`` or ``get user``; the model can only be told about a name the provider
    will accept back.
    """
    return f"{UNSAFE_IN_NAME.sub('_', server)}_{UNSAFE_IN_NAME.sub('_', tool)}"[:MAX_NAME_LENGTH]


def to_parameters(schema: Any) -> dict | None:
    """Translate an MCP ``inputSchema`` into picoagent ``parameters``, or ``None`` if it cannot be.

    The providers all want the same thing: an object schema with a ``properties`` map. So:

    * a missing schema becomes an object with no properties (a tool that takes no arguments),
    * a schema with no ``type`` is taken as an object, which is what every server means by it,
    * ``properties`` and ``required`` are repaired to the types the provider expects,
    * anything whose top level is not an object is refused, because there is no honest way to
      present "this tool takes a bare string" to a function-calling API.

    Everything else in the schema is passed through untouched, including keywords like
    ``$ref`` that some providers reject. Rewriting a server's schema beyond the shape fix
    above would change what the model is told the tool accepts, which is the server's call to
    make and not this plugin's.
    """
    if schema is None:
        return {"type": "object", "properties": {}}
    if not isinstance(schema, dict):
        return None
    declared = schema.get("type", "object")
    kinds = declared if isinstance(declared, list) else [declared]
    if "object" not in kinds:
        return None
    parameters = dict(schema)
    parameters["type"] = "object"
    if not isinstance(parameters.get("properties"), dict):
        parameters["properties"] = {}
    required = parameters.get("required")
    if required is not None and not (isinstance(required, list)
                                     and all(isinstance(name, str) for name in required)):
        parameters.pop("required")
    return parameters


def render_block(block: Any) -> str:
    """One content block as text. Non-text blocks are described rather than dropped.

    A ``ToolResult`` carries a string, so an image or an audio clip cannot be handed to the
    model here. Saying that a block arrived and what it was beats an empty result, which the
    model would read as "the tool did nothing".
    """
    if not isinstance(block, dict):
        return str(block)
    kind = block.get("type")
    if kind == "text":
        return str(block.get("text", ""))
    if kind in ("image", "audio"):
        return f"[{kind} block, {block.get('mimeType', 'unknown type')}, not shown: tool results are text]"
    if kind == "resource":
        resource = block.get("resource") if isinstance(block.get("resource"), dict) else {}
        uri = resource.get("uri", "?")
        if "text" in resource:
            return f"[resource {uri}]\n{resource['text']}"
        return f"[resource {uri}, {resource.get('mimeType', 'unknown type')}, binary: not shown]"
    if kind == "resource_link":
        return f"[resource link {block.get('uri', '?')}]"
    return f"[unsupported content block of type {kind!r}]"


def render_result(payload: Any) -> tuple[str, bool]:
    """A ``tools/call`` result as ``(text, is_error)``.

    ``isError`` is the protocol's way of saying the tool ran and failed, as opposed to a
    JSON-RPC error, which says the call itself was refused. Both end up as an error result for
    the model; only the wording differs.
    """
    if not isinstance(payload, dict):
        return json.dumps(payload), False
    blocks = payload.get("content")
    parts = [render_block(block) for block in blocks] if isinstance(blocks, list) else []
    text = "\n".join(part for part in parts if part)
    if not text and payload.get("structuredContent") is not None:
        # Later revisions added a structured result next to the text blocks. Servers that send
        # only the structured form would otherwise read as an empty answer.
        text = json.dumps(payload["structuredContent"], indent=1)
    return text or "(the server returned no content)", bool(payload.get("isError"))


# ------------------------------------------------------------------ the tool

class McpTool:
    """One remote tool, wrapped so the model cannot tell the difference."""

    def __init__(self, server: StdioServer, name: str, remote_name: str, description: str,
                 parameters: dict):
        self.server, self.name, self.remote_name = server, name, remote_name
        self.description = description
        self.parameters = parameters

    async def execute(self, args: dict, ctx) -> ToolResult:
        """Run ``tools/call`` off the event loop; every failure comes back as a value.

        ``asyncio.to_thread`` because the transport is a blocking pipe: without it, one slow
        server would stop the agent's loop, including the sibling tool calls in the same batch.
        """
        try:
            payload = await asyncio.to_thread(self.server.call_tool, self.remote_name, args or {},
                                              cancelled=ctx.abort.is_set)
        except McpError as exc:
            return ToolResult(ctx.tool_call_id, str(exc), is_error=True)
        text, is_error = render_result(payload)
        body, cut = truncate(text, ctx.config["tool_output_max_bytes"], ctx.config["tool_output_max_lines"])
        return ToolResult(ctx.tool_call_id, body + ("\n[truncated]" if cut else ""), is_error=is_error,
                          details={"server": self.server.name, "tool": self.remote_name})


# ------------------------------------------------------------------ registration

@dataclass
class ServerStatus:
    """What happened with one server, so ``/mcp`` can say more than "it did not work"."""
    spec: ServerSpec
    server: StdioServer | None = None
    error: str = ""
    registered: list[tuple[str, str]] = field(default_factory=list)   # (picoagent name, remote name)
    skipped: list[tuple[str, str]] = field(default_factory=list)      # (remote name, reason)

    def describe(self) -> str:
        if self.error:
            return f"{self.spec.name}: not connected - {self.error}"
        info = self.server.server_info if self.server else {}
        identity = f"{info.get('name', 'unnamed server')} {info.get('version', '')}".strip()
        version = self.server.negotiated_version if self.server else "?"
        state = "running" if self.server and self.server.alive else "not running"
        count = len(self.registered)
        lines = [f"{self.spec.name}: {identity} (protocol {version}, {state}), "
                 f"{count} tool{'' if count == 1 else 's'}"]
        lines += [f"  {name} <- {remote}" for name, remote in self.registered]
        lines += [f"  skipped {remote}: {reason}" for remote, reason in self.skipped]
        return "\n".join(lines)


def connect(spec: ServerSpec, api) -> ServerStatus:
    """Start one server and register its tools. Returns what happened; raises nothing expected.

    Discovery happens here, at load time, rather than on first use: the tools a server offers
    are only knowable by asking it, and the model has to be told what exists before turn one.
    The cost is that a broken server is a startup cost, which is why every failure below is
    caught and turned into a status line instead of stopping the agent.
    """
    status = ServerStatus(spec=spec)
    server = StdioServer(spec, cwd=api.cwd)
    try:
        server.start()
        advertised = server.list_tools()
    except McpError as exc:
        status.error = str(exc)
        log.warning("mcp: %s", exc)
        server.close()
        return status
    _OPEN.append(server)
    status.server = server

    for descriptor in advertised:
        remote = str(descriptor.get("name", "")).strip()
        if not remote:
            status.skipped.append(("(unnamed)", "the server advertised a tool with no name"))
            continue
        parameters = to_parameters(descriptor.get("inputSchema"))
        if parameters is None:
            status.skipped.append((remote, "inputSchema is not an object schema, so no provider can call it"))
            continue
        name = tool_name(spec.name, remote)
        if name in api.all_tools():
            # Registration replaces on name, so this is the line that keeps a server's `read`
            # from taking the built-in `read` away, and two servers from eating each other.
            status.skipped.append((remote, f"the name '{name}' is already registered"))
            continue
        description = str(descriptor.get("description") or f"{remote} on MCP server {spec.name}")
        api.register_tool(McpTool(server, name, remote, description, parameters))
        status.registered.append((name, remote))
    return status


def prompt_section(statuses: list[ServerStatus]) -> str:
    """Tell the model where these tools come from, in three lines."""
    served = [status for status in statuses if status.registered]
    listing = ", ".join(f"{status.spec.name} ({len(status.registered)})" for status in served)
    return ("# MCP servers\n"
            f"Tools named <server>_<tool> come from external MCP servers, one child process each: {listing}. "
            "Their arguments follow the server's own schema. A server that dies or stops answering "
            "returns an error result naming itself; nothing restarts it, so report that failure "
            "rather than retrying.")


def close_all() -> None:
    """Stop every server this process started. Safe to call twice."""
    while _OPEN:
        _OPEN.pop().close()


atexit.register(close_all)


def register(api) -> None:
    # ``servers`` is a list of commands this function spawns before the first turn, with the
    # user's environment and the project directory as cwd. That makes it the one setting in this
    # plugin that must never come out of a cloned repository: ``[plugins.mcp.servers.x]`` with a
    # ``command`` would be arbitrary code executed at session start, ahead of any prompt, ahead
    # of the plugin trust store that gates every other way a repository gets code to run.
    # ``api.plugin_config()`` reads the user layer, so the servers here are the user's own.
    # The two timeouts are how long to wait, not what to run, so a repository may set them.
    settings = api.plugin_config()
    api.warn_about_project_config(*PROJECT_SETTABLE)
    statuses = [connect(spec, api) for spec in server_specs(settings.with_project(**PROJECT_SETTABLE))]
    if any(status.registered for status in statuses):
        api.register_system_prompt_section("mcp", lambda: prompt_section(statuses))

    async def mcp_command(args: str, rt) -> str:
        """``/mcp`` - which servers are connected, which tools they gave us, what was refused."""
        if not statuses:
            return "mcp: no servers configured. Add [plugins.mcp.servers.<name>] with a command."
        return "\n".join(status.describe() for status in statuses)
    api.register_command("mcp", mcp_command, "MCP servers, their tools, and their failures")

    async def shutdown(event, rt) -> None:
        close_all()
    api.on("session_end", shutdown)
