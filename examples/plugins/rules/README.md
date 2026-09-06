# rules

Instruction files that switch on when the files they describe come into play. Standard library
only, no third-party dependencies.

A rule is a markdown file with frontmatter naming one or more globs, plus a body:

```markdown
---
name: python-style
globs: *.py, tests/**
description: How this codebase writes Python
---
Type hints on public signatures. No bare `except`. Line length 110.
```

Put it in `~/.picoagent/rules/` (yours) or `<project>/.picoagent/rules/` (travels in the repo).
When a tool call brings a matching file into play, the body is delivered once, as a user-role
message, at the end of that turn.

This sits between the two things picoagent already had. A context file (`AGENTS.md`) is always
loaded and always paid for. A skill is loaded only when someone names it. A rule is paid for when
it becomes relevant, and nobody has to remember to invoke it.

## The trust gate

Rule text enters the model's context, which is exactly the surface `USER_ONLY` in
`picoagent/core/config.py` exists to protect. `context_files` and `skill_dirs` are user-only
*because* their contents enter the system prompt, so a cloned repository does not get to choose
them. A rules file shipped in a repository and applied automatically is that same attack with a
convenience feature painted on it: clone a repo, and its text reaches your model before you have
read a single line of it.

So trust follows **location, not configuration**:

| Where the rule lives | What happens |
|---|---|
| `~/.picoagent/rules/` | Injected. It is your own text; there is nothing to approve. |
| `<project>/.picoagent/rules/` | Shown to you in full, fingerprinted, approved once, then injected. |
| Any directory `[plugins.rules].dirs` names | Same as a project rule, whatever it points at. |
| Anywhere, in a headless `-p` run | Project rules are **not** injected. User rules still are. |

`[plugins.rules]` is not itself in `USER_ONLY`, so a repository's `config.toml` can set `dirs`.
That is why a configured directory is always treated as project-supplied. Honouring it as
user-level would hand the repository the one key it must not hold, and `USER_ONLY` would have been
re-opened through a plugin.

The approval record lives in `~/.picoagent/rules-trust.json`, next to `trust.json` and for the
same reason: a store the project can write is a store the project can forge. It records the
fingerprint, the globs, and the body length, so a re-prompt can name what moved rather than saying
"something changed, approve again?". `RuleTrust.status()` answers `new` / `trusted` / `changed`,
mirroring `TrustStore` in `picoagent/plugins/loader.py`.

Approval covers the bytes about to be injected, not the bytes read at startup. A project rule is
re-read from disk at the moment it matches, and that one read is what gets fingerprinted, shown,
and sent. A branch switch, a background pull, or the agent writing to `.picoagent/rules/` earlier
in the same session cannot get between the approval and the injection.

Headless fails closed. `picoagent -p` answers every confirm with `False`, so a project rule is
never approved and never injected. The value of the gate is a human reading the text; an
unattended run has no human, so it gets no repository text.

## What this does not protect against

Be clear-eyed about the boundary.

* **The preview is truncated at 40 lines.** You approve a fingerprint over the whole file but you
  read the top of it. A long rule can hide text below the fold. The prompt names the file and says
  how many lines it did not show, and that is the mitigation: it tells you when to go read the
  file. It does not force you to.
* **Approval is per file, not per effect.** A rule approved when `globs: *` covered a small repo
  keeps applying as the repo grows. The fingerprint catches the rule changing; nothing catches the
  repository changing underneath a rule that has not.
* **The gate protects your context, not the model's judgment.** An approved rule is still text the
  model reads and may act on. The system-prompt note tells the model that a `<rule>` block is
  reference material rather than an instruction from you, and the `<rule>` envelope names the
  source file so provenance survives into the transcript. That is framing. Framing shifts odds; it
  does not enforce anything. If you approve a hostile rule, you have approved it.
* **Path extraction from shell commands is a heuristic.** `pytest tests/test_x.py` counts as
  bringing `tests/test_x.py` into play; a path built from a shell variable does not. Getting this
  wrong costs a rule injected that was not needed, or guidance arriving a turn late. It cannot get
  text past the gate, which is the only reason a heuristic is acceptable here.
* **It is void if the plugin is not loaded.** An untrusted plugin does not load, and then no rules
  apply at all, which is the safe direction.

## Design notes

**Which event, and why.** The plugin hooks `tool_call` to observe and `turn_end` to deliver.
"Which files are in play" has one honest answer in this harness: the arguments of the tool calls
the model just made. `tool_call` is the only event carrying every tool's arguments, it fires for
tools other plugins registered, it runs sequentially in model order, and it fires before execution
so a rule is queued even for a call that then fails. Nothing is injected there, because a
`tool_call` handler may only block a call or rewrite its arguments, and a rules plugin that could
block a tool call would be a permission system wearing the wrong name. Delivery waits for
`turn_end`, where `api.send_message(..., deliver_as="steer")` lands the text immediately after the
tool results the model is about to read.

The cost is that guidance arrives one turn after the first tool call, never before it. That is the
right trade: a rule that fires before the agent has touched anything is a rule that costs tokens
on every session regardless of relevance, and picoagent already has that. It is called
`context_files`.

**Message stream, not system prompt.** Timing is the feature. A rule is relevant because a file
came into play at a particular moment, and a message sits at that point in the conversation. The
system prompt has no position: it is re-rendered before every model call, so a section that grows
during a session silently rewrites the prefix the model already read and invalidates any cache of
it. Provenance is the second reason. In the system prompt, repository text is indistinguishable
from picoagent's own standing orders, which is the confusion `USER_ONLY` exists to prevent; in the
stream it arrives inside a `<rule>` envelope naming its source.

One fixed, plugin-authored note does go in the system prompt, via
`register_system_prompt_section`. It is constant text carrying no repository content, so it costs
the same handful of tokens every session and does not reintroduce the surface the gate closes.

**No repeats.** A delivered rule is recorded by path for the rest of the session and never sent
again. Re-sending it whenever a matching file is touched is precisely the token cost this plugin
exists to avoid. A declined rule is also remembered, so it asks at most once per session, and that
refusal is deliberately *not* written to disk: "not now" is weaker than "never", and you may want
to read the file and approve it later with `/rules trust <name>`.

`max_rules_per_turn` (default 4) caps how much one turn may deliver, so a wide glob cannot flood
the context in a single batch.

## Commands

* `/rules` lists every rule found, its trust state (`user`, `new`, `trusted`, `changed`), its
  globs and its path. Read-only; it injects nothing.
* `/rules trust <name>` runs the approval gate for one project rule up front, so it does not have
  to interrupt a run to ask.

## Configuration

```toml
[plugins.rules]
dirs = ["docs/rules"]     # extra directories, always gated as project-supplied
max_rules_per_turn = 4
```

## Use

```bash
picoagent -e path/to/examples/plugins/rules
```

or add it to `.picoagent/config.toml`:

```toml
[plugins]
enabled = ["./examples/plugins/rules"]
```

## Tests

```bash
python3 -m unittest discover -s tests -q
```

`tests/test_rules_plugin.py` covers the gate from both sides: a user rule injected with no prompt,
a project rule refused before approval and injected after, a rule edited after approval re-gated
with a description of what moved, a headless run injecting nothing, and a non-matching glob
staying quiet.
