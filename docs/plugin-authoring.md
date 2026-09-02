# Writing a plugin

A plugin is a directory (usually a git repo) with two files:

```
my-plugin/
  plugin.toml
  my_plugin.py
```

```toml
# plugin.toml
name = "my-plugin"
version = "0.1.0"
entry = "my_plugin:register"          # module:function, relative to this directory
description = "One line users see when installing"
python_deps = []                       # pip-installed by `picoagent plugin add`
skills = ["skills"]                    # optional: folders of SKILL.md to expose
```

```python
# my_plugin.py
def register(api):
    api.register_command("hello", say_hello, "say hello")

async def say_hello(args, rt):
    return f"hello {args or 'world'}"
```

Try it: `picoagent -e ./my-plugin` then type `/hello`.

## The `api` object

`register(api)` gets a `PluginAPI`. Everything you need is on it.

**React to what the agent does**

```python
api.on("tool_call", handler)      # handler(event: dict, rt) -> dict | None
```

Return `None` to observe, a dict to patch the event, `{"block": True, "reason": "..."}` to
stop a tool call. Handlers may be sync or async. See [events-reference.md](events-reference.md).

**Add things the model can use**

```python
api.register_tool(MyTool())                      # object with name/description/parameters/execute
api.register_skill(skill)                        # or list folders in plugin.toml `skills`
api.register_system_prompt_section("mine", lambda: "# House rules\n...")
```

**Add things the user can use**

```python
api.register_command("deploy", handler, "deploy to an environment")   # /deploy prod
api.register_frontend(MyTUI())                                          # replace the REPL
```

**Change how the model is called**

```python
api.register_provider(MyProvider())              # then --provider myprovider
await api.set_model("gpt-4.1", provider="openai")
api.set_thinking("high")
api.set_active_tools(["read", "shell"])          # read-only mode; None restores all
```

**Talk to the model or the user**

```python
api.send_message("Focus on the tests", deliver_as="steer")   # steer | follow_up | next_turn
ok = await api.ui.ask("confirm", "Delete build/?")            # confirm | select | input
await api.ui.emit("notice", {"text": "done"})
```

**Remember things**

```python
api.append_entry("todo", {"items": [...]})       # saved in the session, never sent to the model
for entry in api.entries("todo"): ...
api.plugin_config()                              # [plugins.my-plugin] from config.toml
```

**Run things**

```python
code, output = await api.exec("git", "status")
```

## Writing a tool

```python
from picoagent.core.types import ToolResult
from picoagent.core.tools import truncate, resolve_path, file_lock

class GrepTool:
    name = "grep"
    description = "Search file contents with a regex. Returns file:line:text."
    parameters = {"type": "object",
                  "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}},
                  "required": ["pattern"]}

    async def execute(self, args, ctx):
        code, out = await run_ripgrep(args, ctx.cwd)          # your implementation
        body, cut = truncate(out, ctx.config["tool_output_max_bytes"], ctx.config["tool_output_max_lines"])
        return ToolResult(ctx.tool_call_id, body + ("\n[truncated]" if cut else ""), is_error=code > 1)
```

Rules of thumb:

* Return `ToolResult(..., is_error=True)` for expected failures; don't raise.
* Always truncate output. Unbounded output is the fastest way to break a session.
* If you write files, wrap the read-modify-write in `async with file_lock(path):` so you
  cooperate with the built-in `edit`/`write` when tool calls run in parallel.
* Registering a tool named `read`, `write`, `edit` or `shell` replaces the built-in.

## Writing a provider

Implement one async generator:

```python
from picoagent.core.types import StreamEvent, ToolCall

class MyProvider:
    name = "mine"
    async def stream(self, *, system, messages, tools, model, max_tokens, thinking):
        ...                                   # map `messages` to your wire format
        yield StreamEvent("text", text="hello")
        yield StreamEvent("tool_call", tool_call=ToolCall("id1", "shell", {"command": "ls"}))
        yield StreamEvent("done", usage={"input": 10, "output": 5})
        # on failure: yield StreamEvent("error", error="...") and return
```

If your API is OpenAI-compatible you don't need any of this - see
`examples/plugins/grok-provider` (12 lines). For a different dialect, see
`examples/plugins/vertex-provider`.

## Keeping state across restarts

Store it in the session with `api.append_entry(...)` and rebuild it in a `session_start`
handler by iterating `api.entries(...)`. Because the session is a tree, state stored this
way automatically follows branches when a rewind plugin moves the leaf.

## Testing your plugin

The test helpers in `tests/helpers.py` give you a `ScriptedProvider` (replay a list of
turns) and a `CaptureFrontend` (record events, answer questions). A typical test:

```python
rt = make_runtime(tmp, provider=ScriptedProvider([[call("shell", command="rm -rf x")], [text("ok")]]))
loader.load_plugin(Path("my-plugin"), rt, trust, allow_untrusted=True)
run(AgentLoop(rt).run("clean up"))
assert rt.frontend.tool_results()[0].is_error
```

See `tests/test_example_plugins.py` for complete examples.

## A complete domain plugin

`examples/plugins/es-doctor` is the reference for "teach the agent a system": it combines
seven tools, three runbook skills, a system-prompt section of domain knowledge, a `/es`
command and a `tool_call` guard, all against plain HTTP. Its tests run against a fake
Elasticsearch (`picoagent/testing/fake_es.py`) that serves a scripted incident, which is the
pattern to copy for any plugin that talks to an external service: fake the service, script a
scenario, assert on the queries the plugin sends and the text it returns.

## Publishing

Push the directory to git and tag it. Users install with
`picoagent plugin add git:github.com/you/my-plugin@v0.1.0`. Bump the tag when you change
the entry module; users will be asked to re-trust because the fingerprint changed.
