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
requires = ["picoagent>=0.1"]          # optional: the picoagent versions this plugin expects
```

`requires` is checked at load time and answered with a warning, never a refusal. A constraint the
running picoagent does not meet prints the plugin's name, what it asked for and what is actually
running, and then the plugin loads anyway; so does an entry that cannot be read as a constraint on
picoagent's version at all. The warning is for you, so that a plugin written against a newer
picoagent says so instead of failing in the middle of `register()`. It is not a gate, because a
version line is not worth a user losing a plugin over. A package your code imports is not this
field: put it in `python_deps`.

If your plugin is a security control rather than a convenience, add two more lines. See
[If being skipped is not acceptable](#if-being-skipped-is-not-acceptable).

```python
# my_plugin.py
def register(api):
    api.register_command("hello", say_hello, "say hello")

async def say_hello(args, rt):
    return f"hello {args or 'world'}"
```

Try it: `picoagent -e ./my-plugin` then type `/hello`.

## More than one file

A plugin is a directory, so split it up as it grows. Import your other files by name, the way
you would in any script:

```python
# es_doctor.py
from es_client import ESError, request   # a file beside this one
import es_admin                          # also fine inside a function, imported on demand
```

Those imports resolve *inside your plugin*. The entry module is loaded as a package rooted at
your directory, and the loader rewrites your top-level imports into it, so your `utils.py` is
yours: another installed plugin shipping a `utils.py` gets its own, and load order does not
decide who wins because neither of you takes the bare name `utils`. Two checkouts of the *same*
plugin loaded in one process get separate namespaces too, so `-e` on a second copy does not
quietly run the first one's code. Your directory never joins `sys.path`, so it cannot shadow the
standard library or an installed package for the rest of the session either.

Worth knowing:

* A file you ship wins over an installed distribution of the same name, but only for your own
  modules. Calling a file `json.py` changes what *your* code gets from `import json`.
* Relative imports (`from .es_client import ESError`) reach the same modules. Either style
  works; pick one.
* Anything outside your directory imports normally: the standard library, `picoagent.core.*`,
  a package listed in `python_deps`.
* Subdirectories work the same way, all the way down. `from pkg import mod` gets your `mod`,
  and a module inside `pkg/` may import a file at the top of your plugin by name or reach its
  neighbours relatively. Every module the loader reaches this way is inside your namespace, so
  none of them can pick up an installed package of the same name by accident.
* Use the `import` statement, not `importlib.import_module("utils")`. That function calls the
  interpreter's import machinery directly and cannot be redirected into your plugin, so it looks
  along `sys.path` instead: it will not find your files, and if something installed happens to
  share the name it will silently give you that instead. This is a real limit rather than an
  oversight - see the note in
  [docs/security/trust-boundaries.md](security/trust-boundaries.md#known-limits). If you need a
  module chosen at runtime, import your plugin's modules with the statement and pick between
  them yourself.
* Every file in the directory is part of the trust fingerprint, so a change to any of them
  sends users back to the trust prompt, not only a change to the entry module.
* An approval covers the directory the code was read from. Renaming your plugin is a change
  like any other change, not a fresh start: users see "this changed" rather than "here is
  something new". Two checkouts of your plugin get an approval each.

## If being skipped is not acceptable

A plugin that does not load is skipped and the session carries on. That is right for a
formatter and wrong for a gate: from inside the session, "the guard refused that command" and
"there is no guard" look identical.

Say so in `plugin.toml`:

```toml
required = true
required_reason = "this session's only check on destructive commands"
```

Read before any of your code runs, so it covers the cases a runtime call cannot: a plugin the
trust check refuses never reaches `register()`, and neither does one whose entry module will
not import. With it set, four things stop the session instead of being announced:

* a copy the user approved that has since been replaced by different code,
* the same replacement carrying a different `name` in its `plugin.toml`, because the approval
  covers the directory rather than the name written in it,
* an import that raises, including a dependency that left the environment after approval,
* your plugin no longer being there at all, if the user approved it in their own
  `~/.picoagent/plugins`. You cannot declare anything once you are gone, so the requirement is
  recorded when they approve and read back for that one question; a deleted checkout is as
  absent as a replaced one.

A `register()` that raises stops the session too.

Two things it deliberately does not do. A **first run**, before the user has ever approved you,
is announced loudly and left to continue: install-then-run is the normal path, and being fatal
there would mean the user cannot open a session to trust you from. And when your plugin is the
copy a *repository* suggested rather than one the user installed, `required` is announced
rather than enforced, because a line in a cloned repo's `plugin.toml` must not be able to stop
someone's session.

One thing it does not cover, said plainly because a control that is oversold is worse than one
that is absent: whoever replaces your code can delete the `required` line along with it, and that
replacement is refused as code the user never approved, announced as urgent, and not loaded - but
it does not stop the session. The stop covers your code being replaced, failing to import, failing
to register, or being gone; it does not survive a replacement that also edits your manifest.

The user keeps a way out of every one of these. `picoagent plugin untrust <directory-or-name>`
withdraws the approval your requirement is enforced against, and it reads the trust store without
loading a single plugin, so it works from a machine whose sessions are refusing to start. It takes
a name as well as a directory, so it still works after the directory is gone. Setting
`required = true` is therefore a statement about what your plugin is for, not a switch the user
cannot reach.

`api.declare_required("reason")` is the same declaration made at runtime, for a plugin that
only discovers inside `register()` that it cannot do its job. It sets the same field the
manifest seeds, so you do not need both; if you write both, the runtime reason wins.

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
api.unregister_tool("shell")                     # remove one outright, not just hide it
api.register_skill(skill)                        # or list folders in plugin.toml `skills`
api.register_system_prompt_section("mine", lambda: "# House rules\n...")
api.remove_system_prompt_section("mine")         # the other half; an unknown name does nothing
```

Every registration has an undo on this object, and this is the one for prompt sections. Removing a
name that was never registered is not an error, the same answer `unregister_tool` gives, so a
plugin can undo its own work without first proving it did any. It is also not the same as
re-registering the section with a render that returns `""`: the prompt reads alike either way,
because `build()` skips an empty section, but a blanked section is still registered, still called
every turn, and still the entry another plugin's section of that name would replace. Remove it when
you mean it to be gone; blank it when you mean it to be empty this turn.

`unregister_tool` and `set_active_tools` are not the same removal. `set_active_tools` narrows
what the *model* is offered and is reversible with `None`; the tool is still registered, so
another plugin can find it with `rt.tools.get(name)` and run it. `unregister_tool` takes it out
of the process. Use the first for a plan or read-only mode, the second when the tool must not
exist for anyone.

**Add things the user can use**

```python
api.register_command("deploy", handler, "deploy to an environment")   # /deploy prod
api.register_frontend(MyTUI())                                          # replace the REPL
```

A command handler that raises is logged and shown to the user as `/deploy failed: ...`; the
session stays up. Return a string for the normal case, since that is what the user is shown, or
`None` for a command that has nothing to say. Returning any other type is a bug and is reported
the same way as a raise, because a frontend is handed that value to render: the plain REPL
concatenates it onto its colour codes, and the TypeError from a returned dict used to end the
session, which is the outcome the catch exists to prevent.

**Change how the model is called**

```python
api.register_provider(MyProvider())              # then --provider myprovider
await api.set_model("gpt-4.1", provider="openai")
api.set_thinking("high")
api.set_active_tools(["read", "shell"])          # read-only mode; None restores all
api.get_active_tools()                           # what is offered now; all_tools() is everything
```

Read the active set before you narrow it. `api.set_active_tools(["read"])` replaces whatever
another plugin set, so a plugin that only wants `shell` gone should subtract from what is there:
`api.set_active_tools([t for t in api.get_active_tools() if t != "shell"])`.

**Talk to the model or the user**

```python
api.send_message("Focus on the tests", deliver_as="steer")   # steer | follow_up | next_turn
ok = await api.ui.ask("confirm", "Delete build/?")            # confirm | select | input
await api.ui.emit("notice", {"text": "done"})
```

`send_message` text arrives as a user-role message, so the model reads it as the person's own
words. It is announced to the frontend as `user_message` with `kind: "queued"` (`"injected"` for
text returned from `before_agent_start`, `"typed"` for what the user actually wrote), so a
transcript can show who said what instead of an instruction nobody gave appearing as theirs.

A `notice` is the one frontend event that carries two different things: a slash command's output,
and commentary about the session. In `picoagent -p "..."` stdout is the answer channel, which a
caller redirects and parses, so only a command's own output goes there and a plain
`api.ui.emit("notice", ...)` goes to stderr with the other diagnostics. The REPL prints both the
same way, and `--json` puts both on stdout as events, since that stream is the whole trace.

| `deliver_as` | The message arrives |
|---|---|
| `steer` (default) | right after the current tool batch, inside the run that is going on |
| `follow_up` | as a fresh prompt, once the agent has finished the current one |
| `next_turn` | as its own user-role message, immediately before the user's next prompt |

A `steer` queued from the final `turn_end` has no tool batch left to follow. It is not dropped:
it is carried to the user's next prompt alongside `next_turn` text, ahead of what they typed.
So queue a `steer` only when the guidance still reads sensibly one prompt later, and use
`follow_up` when you want the agent to act on it rather than the user.

**Remember things**

```python
api.append_entry("todo", {"items": [...]})       # saved in the session, never sent to the model
for entry in api.entries("todo"): ...
api.plugin_config()                              # [plugins.my-plugin] from config.toml
```

**Run things**

```python
code, output = await api.exec("git", "status")
code, output = await api.exec("gh", "pr", "list", env={"GH_TOKEN": token})   # named, not inherited
code, output = await api.exec("make", "build", timeout=600)                  # default is 60s
```

A command that runs past its `timeout` comes back as `(124, "timed out after 600s")` rather than
raising, so the timeout is a result you can report like any other. The child gets a SIGTERM, then a
SIGKILL if it is still there, and it is waited for either way, so nothing it left behind outlives
the call. It runs in a process group of its own, which is what makes that reach the children a
`api.exec("sh", "-c", ...)` started for itself. Nothing the command printed before the timeout
survives - the read is cancelled with it - so there is no partial output to inspect.

## Spawning a process

**The rule: a child process a plugin starts gets a minimal environment, and anything more is
named one variable at a time.** Two kinds of child sit outside it, and both are stated rather
than overlooked: a command the *user* typed runs with the user's own environment (`!cmd` in the
REPL, and the `git`/`pip` calls that install a plugin, which authenticate through a credential
helper that reads it), and the built-in `shell` tool inherits everything, which is the leak
`examples/plugins/credential-guard` exists to close by replacing that tool. Everything a plugin
spawns for itself is on this side of the line.

`api.exec` already does this: the child starts from `minimal_env()` in `picoagent/plugins/api.py`
and your `env=` dict is merged over the top. If you spawn your own process instead of using
`api.exec`, use the same function:

```python
from picoagent.plugins.api import minimal_env

proc = await asyncio.create_subprocess_exec(
    "my-server", "--stdio", cwd=api.cwd,
    env=minimal_env({"MY_DIR": "/srv"}, pass_env=["MY_TOKEN"]),
    stdout=asyncio.subprocess.PIPE)
```

`MINIMAL_ENV_NAMES` holds what a program needs to *run*: `PATH` (which is also the search path
that finds your command once you pass `env` at all), `HOME`, the locale and timezone, a temp
directory, and the Windows names a child cannot start without, `SystemRoot` among them, because
a child that opens a socket fails without it. It holds nothing that identifies you to a service.
It is an allowlist rather than a denylist of secret-shaped names, because no such denylist is
complete: `DATABASE_URL` passes every one of them.

Why this and not "inherit, it is only my own code": the output of a command a plugin runs is the
thing plugins put into tool results and notices, and both go back to the model and into the
session log on the next turn. A subprocess the model can influence is a subprocess whose
environment is a prompt away from the transcript. Take the credential your command needs from
`os.environ` at the call site, so the widening is one visible line in your plugin and not the
default for everything you spawn.

Two things this deliberately does not do:

* **It is not a sandbox.** Nothing stops your plugin reading `os.environ` and passing all of it.
  The load-time trust decision is still the only boundary around plugin code. This makes the
  safe environment the one you get for free, not the one you have to remember.
* **It does not stop a child reading files.** `HOME` is in the set, so `~/.aws/credentials`,
  `~/.netrc` and every other on-disk credential is one `open()` away for a process running as
  you. Closing the environment closes the path where a program is *handed* a secret without
  asking; a program that goes looking is a program you chose to run.

Where user config decides what a child sees, keep the setting out of a repository's reach:
`api.plugin_config()` reads your user layers by default, and a key that widens an environment is
exactly the kind that `.from_project()` should not name. The `mcp` plugin's `pass_env` lives
inside its `servers` table for that reason, so a cloned repository can neither name a server's
command nor name the variables it receives.

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
    async def stream(self, *, system, messages, tools, model, max_tokens, thinking,
                     temperature=None):
        ...                                   # map `messages` to your wire format
        # `temperature` is None unless the user set it. Send it only when it is not None:
        # 0.0 is a setting, not an absence, so `if temperature:` would drop it.
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
`picoagent plugin add git:github.com/you/my-plugin@v0.1.0`. Bump the tag whenever you change
anything in the directory: the fingerprint covers every file, not just the entry module, so
users will be asked to re-trust. If you set `required = true`, that re-trust is a hard stop
rather than a notice, so ship upgrades that users will want to read.
