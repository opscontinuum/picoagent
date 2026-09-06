# Plugin roadmap: agents, scheduling, rules, completion, MCP

**Status:** decomposition agreed, individual designs pending
**Date:** 2026-09-06

Seven capabilities were requested together. They are not one change. This document splits
them into shippable pieces, records the decisions taken so far, and names the one structural
problem that half of them share.

## The structural problem

**picoagent is a short-lived process.** `main()` runs `asyncio.run(run_agent(args))`, the REPL
or one-shot finishes, and the process exits. There is no daemon, no scheduler, no watcher, and
no trigger evaluator anywhere in core.

Anything that must fire while picoagent is *not* running therefore needs something outside it.
That single fact separates the requested work into two very different halves, and it is why
"background agents" and "scheduled tasks" are not variations of the same plugin.

## Clusters

| Cluster | Plugins | Shared problem |
|---|---|---|
| A. Execution | subagent, background, schedule, automations | what runs, and when nobody is present |
| B. Instructions | rules | repository-supplied text enters the prompt |
| C. Interop | mcp | a protocol client, transports, tool translation |
| D. Interface | complete | none; it is small |

## Decisions taken

Recorded so they can be overturned deliberately rather than discovered later.

1. **Subagents are for context isolation**, not parallelism. The parent asks, the child works,
   only the answer returns.
2. **A child runs in-process**, not as a subprocess. A fresh `Runtime` and a throwaway
   `Session`, reusing the parent's provider. No process spawn, no plugin reload, and the parent
   chooses the child's tool set.
3. **A child writes a throwaway session.** The parent's session log is not extended with the
   child's turns. This matches the read-only-log constraint agreed for skillify.
4. **Background agents run in-session only.** They are concurrent with your REPL and die with
   it. This is honest about what they are: concurrency, not persistence.
5. **Scheduled work uses the operating system.** cron, systemd timers, or Task Scheduler
   entries that invoke `picoagent -p "..." --json`. No daemon is written. The OS already solved
   restart-on-boot, retry and logging, and a daemon that executes shell commands with no human
   present is a security surface that needs its own design, not a side effect of this one.
6. **Rules from a repository are trust-gated**, reusing the existing `TrustStore` pattern.
7. **Tab completion is REPL completion**, not inline model-driven completion. picoagent is not
   an editor and this does not make it one.
8. **The subagent plugin ships enabled by default.** This is a departure from the stated design,
   where `plugins.enabled` defaults to empty and every capability is opted into. It is defensible
   because the plugin remains replaceable by name, which is the property that matters, but it is
   a change of philosophy and is recorded as one.

## A. Execution

### `subagent` (first, ships enabled by default)

An `agent` tool: the model calls it with a prompt, a child loop runs to completion, and only its
final text returns as the tool result. Thirty turns of grepping cost the parent two lines.

Built from what exists: a new `Runtime` sharing the parent's provider and tool registry, a
`Session` in a temp directory, and `AgentLoop.run`. The child's tool set is the parent's minus
`agent` itself, which is also the recursion guard: one level, by construction.

Bounds are not optional. A turn cap and a wall clock, both configurable, because a child that
livelocks would otherwise hang the parent silently. Existing evidence: a small model spent 21
tool calls achieving nothing during toolchain probing.

Risks: a child inherits the parent's tools, so it can write files and run shell commands with no
separate approval. Whether the child's tool calls are gated is a design question, not a detail.

### `background`

`/bg <prompt>` starts a child as an asyncio task and returns the prompt immediately. `/bg list`
and `/bg get <id>` inspect it. Results arrive as a notice.

Depends on `subagent` for the child machinery; it is a scheduler over the same primitive. Dies
with the REPL, by decision 4.

Risks: two agents writing files concurrently. The per-file lock in `tools.py` covers the same
file, not two files in a broken combination.

### `schedule`

`/schedule "0 9 * * 1-5" /skill:triage` writes an OS scheduler entry invoking
`picoagent -p "/skill:triage" --json`, appending to a run log. `/schedule list` and
`/schedule remove` manage them.

Three platform back ends behind one interface: cron, systemd user timers, Task Scheduler. The
plugin never edits a crontab without showing the exact line and getting approval.

Risks: an unattended run has no human to approve a tool call. Whatever permission posture a
scheduled run takes must be explicit and probably stricter than an interactive one.

### `automations`

A trigger bound to an action: on a schedule (delegating to `schedule`), on a session event
(subscribing to the event bus), or on a file change. The action is a skill invocation or a
prompt.

This is the least defined of the seven and should be specified last, once `subagent`,
`background` and `schedule` exist, because it is mostly composition of the three.

## B. Instructions

### `rules`

Instruction files applied automatically by glob, so guidance about a subsystem is attached when
that subsystem is in play, rather than always-on like a context file or manually invoked like a
skill.

**The security question is the design.** `context_files` and `skill_dirs` are `USER_ONLY`
precisely because their contents enter the system prompt, so a cloned repository cannot choose
them. A rules file that a repository supplies and that is applied automatically is that same
surface. The answer is the trust gate the plugin loader already uses: `TrustStore.status()`
returns new, trusted or changed, and `describe_change()` names what moved. Repository rules are
shown once, fingerprinted, and re-shown when they change.

User-level rules under the user directory need no gate; they are the user's own text.

## C. Interop

### `mcp`

An MCP client: connect to configured servers, discover their tools, and register each as a
picoagent tool through `api.register_tool`, translating schemas and results.

MCP is JSON-RPC 2.0 over stdio or HTTP, which is implementable with the standard library, so the
plugin can declare no dependencies. Whether to implement it or depend on an SDK is an open
question for its own spec.

Largest of the seven: transport handling, lifecycle, schema translation, error mapping, and
timeouts. Risks: a server's tools arrive with names that may collide with built-ins, and
registries replace on name, so namespacing is required rather than optional.

## D. Interface

### `complete`

Tab completion in the REPL for slash commands, skill names, model names and file paths.

`PlainFrontend._readline` calls `input()` in an executor. On POSIX, `input()` routes through GNU
readline when the `readline` module has been imported, and nothing imports it today. So the
plugin imports `readline`, installs a completer, and the existing frontend needs no change.

Risks: Windows has no `readline` in the standard library, so the plugin must degrade to doing
nothing rather than failing to load.

## Order

1. `subagent`: cheapest, unblocks two others, requested default-on
2. `complete`: small, self-contained, no design risk
3. `schedule`: needed before automations
4. `background`: depends on subagent
5. `rules`: needs its own security design
6. `mcp`: largest
7. `automations`: mostly composition, specify last

Each gets its own spec and its own implementation plan. This document is the map, not the plan.

## Open questions

* Are a subagent's tool calls gated the way a parent's are, or does the parent's approval cover
  the child? This decides whether background and scheduled agents are usable at all.
* What permission posture does an unattended scheduled run take, given no human is present?
* Does `mcp` implement JSON-RPC with the standard library or declare an SDK dependency?
* Does shipping one plugin enabled by default imply a supported set of default plugins, or is
  `subagent` a one-off?
