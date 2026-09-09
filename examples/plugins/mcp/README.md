# mcp

A Model Context Protocol client for picoagent. It starts the MCP servers you configure, asks
each one what tools it has, and registers them so the model can call them like any built-in.
Standard library only: JSON-RPC 2.0 over a pipe is `subprocess` and `json`, so there is no SDK
to install and nothing to declare in `python_deps`.

## Protocol version

This client targets protocol revision **2024-11-05** and announces it in `initialize`. The
four messages it uses (`initialize`, `notifications/initialized`, `tools/list`, `tools/call`)
have the same shape in the later revisions, so announcing the oldest widely implemented one
buys compatibility without giving anything up. Override it per server with `protocol_version`
if you have a server that wants a newer one.

Two deliberate deviations from the specified lifecycle, both stated here so nobody has to read
the code to find them:

* **A different negotiated version is a warning, not a disconnect.** The spec says a client
  that cannot support the version the server answers with should disconnect. This client logs
  what the server said, records it (`/mcp` shows it) and keeps going, because the only two
  methods it calls are stable across every revision. If your server changes those, that
  tolerance is the first thing to remove.
* **A timed-out request is abandoned, not cancelled.** The tidy thing is to send
  `notifications/cancelled` so the server can stop working. Notifications are out of scope
  here (see below), so the request id is dropped instead and a late answer is discarded.

## What is out of scope

Stated rather than half-built:

| Not implemented | Why |
|---|---|
| HTTP and SSE transports | stdio only. Everything here assumes a child process and a pipe. |
| Resources (`resources/*`) | Nothing maps them onto a tool call without inventing semantics. |
| Prompts (`prompts/*`) | picoagent has its own skills and commands for that. |
| Sampling (`sampling/createMessage`) | Would let a server drive your model. That needs a consent design first. |
| Roots, elicitation, completion | Client capabilities this plugin does not declare, so servers will not ask. |
| Notifications | `notifications/initialized` is sent because the handshake requires it. Nothing else is sent, and anything the server sends (including `tools/list_changed`) is logged and dropped, so a server that adds tools later needs a restart to be seen. |

## Install

```bash
picoagent plugin add ./examples/plugins/mcp
```

```toml
# ~/.picoagent/config.toml or <project>/.picoagent/config.toml
[plugins]
enabled = ["./examples/plugins/mcp"]

[plugins.mcp]
timeout = 30                        # seconds per tools/call, unless a server overrides it
startup_timeout = 20                # seconds for the handshake and tools/list

[plugins.mcp.servers.notes]
command = "python3"
args = ["-m", "my_notes_server"]
env = { NOTES_DIR = "/srv/notes" }  # set here, on top of the minimal environment below
# pass_env = ["NOTES_TOKEN"]        # carry these over from your own environment, by name
# cwd = "/srv/notes"                # default: the project directory
# timeout = 60
# protocol_version = "2024-11-05"

[plugins.mcp.servers.tickets]
command = "/usr/local/bin/ticket-mcp"
args = ["--stdio"]
```

The array-of-tables spelling works too, with the name as a key:

```toml
[[plugins.mcp.servers]]
name = "notes"
command = "python3"
args = ["-m", "my_notes_server"]
```

## Environment

A server does **not** inherit your environment. It starts with the set in
`picoagent/plugins/api.py` (`MINIMAL_ENV_NAMES`): `PATH`, `HOME`, the locale and timezone
variables, a temp directory, and the Windows names a child cannot start without, `SystemRoot`
among them, because Winsock loads its provider DLLs through it and a server that opens a socket
without it fails naming a DLL. That is what a program needs to run. It is not what a program
needs to spend your cloud account.

The difference matters here more than anywhere else in picoagent. A server is long-lived, it
starts before your first prompt, and the arguments it executes come from the model. Inheriting
`os.environ` handed every one of them `AWS_SECRET_ACCESS_KEY`, `GH_TOKEN` and
`PICOAGENT_API_KEY` at startup, with nothing to read in the session that said so.

Two channels add to that set, both per server, both in your own config:

| Key | Use it for |
|---|---|
| `env` | a value you are stating: `NOTES_DIR = "/srv/notes"` |
| `pass_env` | a variable of yours the server needs by name: `pass_env = ["NOTES_TOKEN"]` |

```toml
[plugins.mcp.servers.tickets]
command = "/usr/local/bin/ticket-mcp"
args = ["--stdio"]
pass_env = ["TICKET_API_TOKEN"]     # this server, this variable, nothing else
```

Both live inside a `servers` entry, and `servers` is read from your config only, never from a
repository's `.picoagent/config.toml`. So a repository can no more widen what a server sees than
it can choose what a server is. A repository that tries says so at session start, in the same
notice that names every other key of its this plugin refused.

If a server starts on your machine and not under picoagent, a missing variable is the first
thing to check: `/mcp` reports the failure with the child's stderr, and a runtime complaining
about a home directory or a locale is telling you which name to add. Containerised servers are
the common case for `DOCKER_HOST`: rootless Docker, Colima and Podman all put the socket
somewhere the default does not cover, and that name goes in `pass_env` like any other.

`command` is run as a child process with a pipe, so anything that starts a server works. Prefer a
container when you have the choice:

```toml
[plugins.mcp.servers.everything]
command = "docker"
args = ["run", "-i", "--rm", "node:22-alpine", "npx", "-y", "@modelcontextprotocol/server-everything"]
startup_timeout = 300   # --rm means a cold npm cache every start, so npx fetches the package again
```

`docker run -i` is a stdio command like any other, so a stdio-only client reaches any containerised
server without needing a transport it does not have. Nothing has to be installed on the host, the
server behaves the same on every machine, and cross-OS pipe problems go away.

That last one is not hypothetical. On a WSL machine with no Linux Node installed, `npx` resolves
through interop to the Windows binary (`/mnt/c/Program Files/nodejs/npx`). Running a server that way
starts it under `CMD.EXE` in a UNC path it cannot use. The child comes up, the pipe is there, and
the `initialize` handshake never completes. It looks exactly like a client bug and it is not one.
Run `command -v npx` before assuming otherwise, and use a container if that path is on `/mnt/c`.

The containerised form above is what `tests/test_mcp_live.py` drives against the reference server.
It is opt-in, so it skips unless you set `PICOAGENT_E2E_MCP=1`.

## What the model sees

Every tool is registered as `<server>_<tool>`: the `search` tool on the server you called
`notes` becomes `notes_search`. This is not a style choice. picoagent registries replace on
name, so a server offering a tool called `read` would take the built-in `read` away from the
model, and nothing in the log would call that an error. Prefixing also keeps two servers that
both offer `search` from eating each other.

Names are reduced to what function-calling APIs accept (letters, digits, `_`, `-`, 64
characters), so a server tool called `search.files` arrives as `notes_search_files`. If the
resulting name is already registered, the tool is refused rather than registered over the top,
and `/mcp` says which one and why.

`/mcp` prints every configured server: what it identified itself as, the protocol revision it
answered with, whether it is still running, the tools it gave us, and the ones that were
skipped.

## Schema translation

MCP tools carry a JSON Schema `inputSchema`; picoagent tools carry `parameters`, which goes to
the provider's function-calling API. Those APIs all want a top-level object schema, so:

* a missing schema becomes `{"type": "object", "properties": {}}` (a tool with no arguments),
* a schema with no `type` is read as an object, which is what servers mean by it,
* `properties` and `required` are repaired if they are the wrong type,
* a schema whose top level is **not** an object is refused at registration with a reason in
  `/mcp`, rather than passed on to fail later as an HTTP 400 on an unrelated turn.

Everything else is forwarded untouched, `$ref` and `$defs` included. Rewriting a server's
schema further would change what the model is told the tool accepts, and that is the server's
call to make. Providers vary in what they accept: the Gemini path in `vertex-provider` drops
keywords it does not know, other providers reject them.

## Lifecycle and failure

Servers start when the plugin loads, not on first use. Tool discovery is a protocol operation,
so there is no way to tell the model what exists without talking to the server first. The cost
is that a broken server costs you startup time; the compensation is that a broken server is
never more than a message. A server that will not start, exits, answers with a JSON-RPC error,
reports `isError`, or stops answering produces `ToolResult(is_error=True)` naming the server,
with its exit code and the tail of its stderr. Only genuine bugs raise.

Nothing restarts a dead server. Every call has a timeout (`timeout`, per call), and the reader
thread releases waiting calls the moment the child's stdout closes, so a server that dies
mid-call fails immediately rather than after the full timeout. Cancelling a run (the agent's
abort) also releases the call.

On `session_end`, and at process exit as a backstop, each child gets the shutdown the
transport prescribes: close stdin, wait, then terminate, then kill.

## Trust

An MCP server is a program that runs as you, with the environment described above and no
containment beyond it, and its tool descriptions go into your model's prompt, where they steer it.
The minimal environment is worth what it is worth and no more: it stops a server being *handed*
your keys at startup, and it does nothing to stop one that goes looking, since `HOME` is in the
set and `~/.aws/credentials` is a file you can read. Run a server you have not reviewed in a
container, which the `docker run -i` form above already shows. Reviewing it is not different from reviewing this plugin, which
picoagent already makes you trust by fingerprint before it will load. The `permission-gate`
plugin's confirmations apply to these tools as well, since they are ordinary registered tools.

## Tests

`tests/test_mcp_plugin.py`, against the fake stdio server in `picoagent/testing/fake_mcp.py`.
It lives there, next to `fake_es.py` and `fakes.py`, because it is a runnable fake server like
they are, and because a plugin's trust fingerprint covers every file in its directory: a
test-only file shipped inside the plugin would enlarge what a user has to approve and would
change the fingerprint every time a test changed.

```bash
python -m unittest discover -s tests -v
```
