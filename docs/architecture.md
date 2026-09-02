# Architecture

## The one-sentence version

The core is a loop that sends a conversation to a model, runs the tools the model asks for,
and repeats; everything else attaches to that loop through events.

## The pieces

```
picoagent/
  cli.py                 argument parsing; wires everything below together
  core/
    loop.py              AgentLoop (control flow) + Runtime (shared registries)
    provider.py          Provider protocol + built-in OpenAI-compatible client
    tools.py             Tool protocol, registry, read/write/edit/bash
    skills.py            SKILL.md discovery and /skill:name expansion
    context.py           system-prompt sections, AGENTS.md discovery
    session.py           append-only JSONL log with parent ids (a tree)
    commands.py          slash-command registry
    events.py            the event bus
    config.py            layered TOML config
    types.py             dataclasses shared by everything
  plugins/
    api.py               PluginAPI - the only thing plugins import
    manifest.py          plugin.toml
    loader.py            discovery, git install, trust, import
  frontends/
    base.py              Frontend protocol
    plain.py             line REPL (default)
    print.py             -p / --json headless modes
  testing/fakes.py       fake model servers (openai, grok, vertex)
```

Each module has one job and depends only on `types.py` and the modules below it in the
list. `loop.py` is the only place that knows about all the registries.

## Life of a prompt

```
user types "add a --verbose flag"
  │
  ├─ commands.parse()        "/help", "/model"...      -> handler runs, no model call
  ├─ events: input           plugin may transform text or mark it handled
  ├─ skills.expand()         "/skill:x args" -> skill body
  │
  └─ AgentLoop.run(prompt)
       ├─ prompt.build() + skills.prompt_section()     -> system prompt
       ├─ events: before_agent_start                   plugin may rewrite system prompt / inject a message
       ├─ session.append_message(user)
       └─ loop until the model stops calling tools:
            ├─ events: turn_start
            ├─ events: context                         plugin may rewrite the history (compaction lives here)
            ├─ provider.stream()                       text deltas -> frontend; tool calls collected
            ├─ events: tool_call  (per call, in order)  plugin may block or rewrite args
            ├─ execute tools (parallel; same-file edits serialise on a lock)
            ├─ events: tool_result (per call)           plugin may patch the result
            ├─ session.append_message(tool results)
            └─ events: turn_end
       ├─ events: agent_end
       ├─ follow_up messages queued by plugins?  -> run again
       └─ events: agent_settled
```

## Why the core is this small

The four popular harnesses (Claude Code, Codex CLI, Pi, OpenCode) disagree about almost
everything except: a model loop, read/write/edit/shell tools, SKILL.md skills, a context
file, a session log, and lifecycle hooks. Pi ships with only that and pushes the rest to
packages, which proves it is enough. Keeping permissions, compaction, MCP, subagents, plan
mode and the TUI out of core means:

* each of those can be replaced by a better implementation without a fork;
* the core has few reasons to change, so plugins stay compatible;
* the whole thing is small enough to read in an afternoon.

## Overriding built-ins

Every registry replaces on name. A plugin that registers a tool called `shell` *is* the shell
tool from then on. Same for providers (`openai`), commands (`help`), system-prompt sections
(`base`), and the frontend. Load order is user config -> project config -> user plugin dir ->
project plugin dir -> `-e` paths, so a project can override what a user installed, and a
one-off `-e` beats both.

## Sessions are a tree

Each JSONL entry has an `id` and a `parent`. `Session.leaf` points at the newest entry;
`Session.branch()` walks parents back to the root. Moving `leaf` to an older entry and
appending creates a branch. Compaction is just another entry that says "when building the
model context, replace everything before entry X with this summary". Nothing is ever
deleted, so undo/rewind/tree UIs are plugin work on top of this file.

## Providers

`Provider.stream()` is an async generator of `StreamEvent`s: `text`, `thinking`,
`tool_call`, `done`, `error`. The built-in client speaks OpenAI's dialect over `urllib`;
the blocking HTTP read runs in a thread that feeds an `asyncio.Queue`. A provider with a
different dialect (Vertex/Gemini in `examples/plugins/vertex-provider`) implements the
same generator and maps messages itself.

## Frontends

The loop never prints. It calls `frontend.emit(event, payload)` for display and
`frontend.ask(kind, prompt)` for questions. `PlainFrontend` is a REPL; `PrintFrontend` is
for scripts; a TUI or an RPC server is a plugin that calls `api.register_frontend(...)`.

## Trust

Plugin code runs with your privileges. `picoagent plugin add` shows the manifest, asks, and
records the approval in `~/.picoagent/trust.json`: a hash of `plugin.toml` plus the entry
module, the same hash per-file, and the git commit when the plugin is a checkout. A changed
file invalidates the fingerprint and the plugin is skipped until re-approved.

The record holds more than the fingerprint on purpose. A bare hash can only say *that*
something changed, which leaves one blunt response — re-approve and hope. The per-file hashes
name the file that moved, and the commit turns an upgrade into a reviewable range, so
`plugin trust` can show what is actually being accepted:

| `plugin list` status | meaning | what to do |
|---|---|---|
| `trusted` | approved, and the code still matches | nothing |
| `CHANGED` | approved before, but this is not that code | review the change and accept, or leave it unloaded |
| `UNTRUSTED` | never approved | review the manifest and approve |

`CHANGED` is the security-relevant one: code the user vetted has been replaced by code they
haven't seen. It arises just as often from editing a plugin yourself as from an upgrade
pulling in new commits, and only the user can tell those apart — so the CLI describes the
change and asks rather than deciding. Declining is a real outcome: the plugin stays unloaded,
and startup reports it on stderr rather than letting it vanish silently.

`-e path` trusts a directory for a single run (the development escape hatch, so an edit-run
loop doesn't demand re-approval each time); `--dangerously-trust-all` does what it says.
