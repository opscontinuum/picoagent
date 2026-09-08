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

## Publishing a plugin somebody can verify

A `python_deps` entry is an ordinary pip requirement line, so it may carry a version and hashes:

```toml
python_deps = [
  "requests==2.31.0 --hash=sha256:58cd2187c01e70e6e26505bca751777aa9f2ee0b7f4300988b709f44e013003f",
]
```

Sites that have written a `~/.picoagent/plugin-pins.toml` **require** that shape by default:
every entry pinned with `==` and carrying at least one `--hash`, whereupon pip is invoked with
`--require-hashes`, which also means you must list the whole transitive set rather than only the
package you import. Sites without that file take bare names as they always have. Writing the
hashed form costs you nothing on those machines and is the difference between installable and
refused on the others, so write it. `pip freeze` and `pip hash` produce the values.

Publish the plugin's digest alongside each release, too. It is the value an administrator pins
against, and they cannot check what you have not published:

```sh
python3 -m picoagent.plugins.loader ./my-plugin
```

Every file in the directory is covered, so a release note's digest and the directory a user
clones have to match exactly - a stray editor backup file changes it. See
[docs/security/trust-boundaries.md](security/trust-boundaries.md#requiring-a-hash-or-a-signature-before-a-plugin-may-be-installed)
for what a site does with it, and for the `signed_by` alternative if you sign your tags.

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
from picoagent.core.tools import tool_result, resolve_path, file_lock

class GrepTool:
    name = "grep"
    description = "Search file contents with a regex. Returns file:line:text."
    parameters = {"type": "object",
                  "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}},
                  "required": ["pattern"]}

    async def execute(self, args, ctx):
        code, out = await run_ripgrep(args, ctx.cwd)          # your implementation
        return tool_result(ctx, out, is_error=code > 1, pattern=args["pattern"])
```

`tool_result(ctx, text, is_error=False, **details)` is the last line of most tools: it cuts
`text` to the session's limits, appends `[truncated]` when it cut, and wraps the rest with
`ctx.tool_call_id`. Keyword arguments become `ToolResult.details`, which UIs and plugins read
and the model never sees, so structured facts about the call go there and `text` stays what the
model is meant to read. It takes `ctx` rather than the two limits because the result carries the
call id as well, and both arrive in that one object.

It keeps the *head* of the output, which is what a document or a listing needs. A tool whose
output matters at the end - a command's - calls `truncate(out, max_bytes, max_lines,
keep="tail")` itself and assembles the `ToolResult`, the way the built-in `shell` does, because
it has a footer to add after the cut.

Rules of thumb:

* Return `ToolResult(..., is_error=True)` for expected failures; don't raise.
* Always truncate output. Unbounded output is the fastest way to break a session. `tool_result`
  is that, done; four shipped plugins wrote their own copy of it before it moved into core.
* If you write files, wrap the read-modify-write in `async with file_lock(path):` so you
  cooperate with the built-in `edit`/`write` when tool calls run in parallel.
* Registering a tool named `read`, `write`, `edit` or `shell` replaces the built-in.
* Take the path from `resolve_path(ctx, args["path"])`, never from `args["path"]` directly.
  It is the same resolution the built-ins use, and it is what a gate is inspecting.
* If your tool's path argument is normally the model's own construction rather than something
  the user typed, take it from `resolve_path_inside_project(ctx, args["path"])` instead. Same
  resolution, one more rule - see below.

## Gating a path argument

**A `tool_call` handler that decides about a path must resolve it with
`resolve_tool_path(raw, rt.cfg)` from `picoagent.core.tools`, and decide about what comes
back.** Never about the string the model wrote, and never about your own `Path(raw).resolve()`.

```python
from picoagent.core.tools import resolve_tool_path

async def guard(event, rt):
    raw = event["args"].get("path")
    if not isinstance(raw, str) or not raw:
        return None
    target = resolve_tool_path(raw, rt.cfg)        # rt.cfg is the tool's ctx.config
    if target.path in protected_files:
        return {"block": True, "reason": "protected"}
    return None
```

The reason is not tidiness. A guard runs *before* the tool, on the argument string, and the
tool then opens whatever `resolve_path` makes of that string. If the two do not agree on which
file that is, the guard is inspecting one file while the tool opens another, and every
difference between them is a way past the guard. Three shipped bypasses came from exactly this,
each from a guard doing its own resolution:

| The spelling | What the guard saw | What the tool opened |
|---|---|---|
| `@~/.picoagent/credentials` | a filename with an `@` in it, which nothing opens | the credentials file, key into a tool result and from there into the next prompt |
| `../../.picoagent/credentials` under `picoagent -C project` | a path under the directory the *process* was started in | a path under the *session* directory, which `-C` moved |
| `<project>/.git/hooks/pre-commit` | a string that did not match the pattern `.git/**` | the hook that runs on the user's next commit |

`resolve_tool_path` is the one place those decisions are taken: it strips a leading `@` (models
copy it from `@file` mentions), expands `~`, resolves a relative path against the session
directory rather than the process's, and follows symlinks - including for a file that does not
exist yet, whose parents `write` is about to create.

What it returns, and why it is not a `Path`:

```python
ResolvedPath(path=PosixPath("/home/u/proj/.git/config"), refusal=None)
```

* `path` is always there, always absolute, always symlink-resolved. Even for a path the tool
  is going to refuse. A guard asking "which file is this?" about a refused path needs an
  answer, and `None` is not one: `None` reads as *no file here*, which is what an unrelated
  argument looks like, so a guard that reads it that way allows the call.
* `refusal` is the sentence explaining why the tool will not open it (outside the project under
  `confine_to_project`, or a path the OS cannot resolve at all), or `None`. Ignoring it is fine
  and usually right - blocking a call the tool would refuse anyway costs nothing. Reading it as
  "there is no file" is the mistake.
* Nothing raises. An expected failure is a value here, following the same rule tools follow, so
  a guard never needs a `try` around the question.

**A relative path that leaves the project.** `confine_to_project` is off by default, because a
coding agent legitimately edits the sibling repository and `~/.config`. That is right for a
`path` the user typed and wrong for one the model built for itself: `repo="../.."` is a mistake
nobody asked for, and a tool that quietly scans the parent of the work tree is worse than one
that says no. The pair for that is one rule further on:

```python
from picoagent.core.tools import PathRefused, resolve_path_inside_project, resolve_tool_path_inside_project

# in a tool: the refusal is an exception the call site turns into an error result
try:
    root = resolve_path_inside_project(ctx, args["repo"])
except PathRefused as exc:
    return tool_result(ctx, str(exc), is_error=True)

# in a guard: the refusal is a value, as always
answer = resolve_tool_path_inside_project(raw, rt.cfg)
```

A *relative* path must land inside the project; an absolute one is allowed, because it is
somebody naming a place on purpose. It is a **usability** rule, not a security boundary, and
must not be sold as one: whatever can write `../..` can write `/etc`. `confine_to_project` is
the boundary, it is enforced underneath this, and it is unchanged by it.

The escape is judged on the *resolved* path - after the `@` is stripped, `~` expanded and
symlinks followed - for the same reason everything else here is. Two shipped plugins wrote this
rule for themselves and the copies disagreed: one read the unstripped string, so `@../..` passed
where `../..` did not, and compared text rather than the resolved path, so a link inside the
project pointing out of it passed as a child of it. That is the drift this whole section is
about, arriving one level up.

**Matching patterns against a resolved path.** A resolved path is absolute, so a relative
pattern like `.git/**` or `.env` will not `fnmatch` it. Match against every trailing run of the
path's components instead - `permission_gate._spellings` is nine lines of it:

```python
def _spellings(path):
    parts = path.parts
    return [path.as_posix()] + ["/".join(parts[i:]) for i in range(1, len(parts))]
```

`/home/u/proj/.git/hooks/pre-commit` offers itself, then `proj/.git/hooks/pre-commit`,
`.git/hooks/pre-commit`, `hooks/pre-commit` and `pre-commit`. `.git/**` matches the third,
`.env` and `**/*.pem` match the last, and the absolute spelling that used to slip past matches
the first. Patterns people already wrote keep working, and each of them now covers every way of
naming the same file. It also widens every slash-bearing pattern to the same structure anywhere
the agent can reach rather than only the project's: `.git/**` covers a sibling checkout's `.git`,
and a user's `config/database.yml` covers that file in whichever repository they open next.
Deliberate - a protected pattern is the user saying "do not touch this kind of file", and the
kind does not stop at the project boundary - and the direction a protected list should be wrong
in, since it refuses more and never less. What it costs is that the refused file is often not the
one the tool argument appears to name, so say both in the block: the file that was refused and
the pattern that refused it. A refusal nobody can act on gets worked around.

A plugin that reads paths without gating them - `rules`, which uses tool arguments to work out
which file the agent is on - has the same drift with a smaller cost: a rule that quietly does
not fire rather than a guard that quietly does not guard. Resolve through the seam there too,
and the two stay in step.

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
