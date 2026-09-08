# skillify: codify a completed task into a skill

**Status:** design approved, not implemented
**Date:** 2026-09-06
**Scope:** skill authoring only. Tool authoring is a separate sub-project with its own spec.

## The problem

picoagent ships 17 skills and no way to write one from inside the agent. A user who has just
walked the agent through a process they repeat every week has no path from "we did it" to "we
can do it again" except opening an editor and writing a `SKILL.md` by hand.

This is a plugin that closes that gap: `/skillify [name]` reads what just happened, drafts a
skill, exercises it, and writes it once a human is satisfied.

## Scope

**In:** one command that turns the current session into a reviewed, tested `SKILL.md`.

**Out, deliberately:**

* **Tool authoring.** Writing executable Python that the agent then calls is a different
  problem with a different risk profile. picoagent already treats plugin code as untrusted and
  gates it behind a `TrustStore`. That design deserves its own spec.
* **Recurrence detection.** Mining `Session.list()` for processes the user repeats is valuable
  and independent. It can be built later on top of this, reusing the same drafting engine.
* **Unattended execution.** picoagent has no scheduler, watcher or trigger concept. A skill is
  already an automation invoked by name; nothing here needs a cron.
* **A redaction engine.** `credential-guard` exists as a plugin. Skillify composes with it and
  does not grow its own scanner.

### Why one command and not two

`/skillify` and `/automate` would read the same input, emit the same artifact and write it to
the same place. The only difference is whether the draft generalises, and a skill that does not
generalise is a bad skill either way. One command. "Automate the thing I do all the time" is
what it is for.

## Constraints

1. **The session log is read-only.** No new entry kinds, no new `Message` fields, no change to
   tool-output truncation, and skillify writes nothing to the live session, not even a `custom`
   entry.
2. **A human approves every live model call**, before it runs, individually.
3. **A human approves every tool call made while a drafted skill is under test**, before it runs,
   individually.
4. **There is a test loop.** A drafted skill is exercised before it is accepted, and the pass
   condition is a human verdict.
5. **Standard library only**, per the project rule. The plugin declares no `python_deps`.

## What already exists

Nothing in core changes. Everything needed is on the plugin API or in the session format.

| Need | Existing mechanism |
|---|---|
| Read the transcript | `api.session`, `session.branch()`, `session.messages()` |
| Complete history despite compaction | Compaction summarises the model's context; the original entries stay on disk |
| Ask the human | `api.ui.ask("confirm", prompt)`, returns a bool |
| Gate a tool call | `api.on("tool_call", handler)`; return `{"block": True, "reason": ...}` to stop it |
| Register the result live | `api.register_skill(skill)` |
| Parse a draft | `parse_frontmatter`, `load_skill` in `picoagent/core/skills.py` |
| Parameterise a skill | `$ARGUMENTS` substitution, already supported |
| A model call | `rt.providers.get(rt.provider_name).stream(...)` |

Two properties of the session log make this cheaper than expected. It is append-only and
nothing prunes it, so the record is durable. And compaction does not destroy history, so a
skillify run sees the whole session even when the model itself no longer does.

Two limits of the log shape the design. Reasoning is never persisted, so the trace holds what
was done and not why; the draft must re-derive intent from actions. And tool output is
truncated to roughly 50 KB / 2000 lines before it is stored, so large reads appear clipped.
Neither is a blocker for codify-on-request. Both would matter more for recurrence detection.

## Architecture

A plugin at `examples/plugins/skillify/`, matching how every other capability here ships.
*(2026-09-08: the capability plugins have since moved to `opscontinuum/picoagent-plugins`; land skillify there instead.)*

```
examples/plugins/skillify/
  plugin.toml
  skillify.py        register(), the command handler, the tool_call gate
  trace.py           extract(entries) -> list[Step]        pure
  draft.py           build_prompt(steps) -> str            pure
                     one model call, gated
  validate.py        is_loadable(text) -> (bool, reason)   pure
  write.py           render + write + register
```

The seam is deliberate: the parts most likely to break silently (extraction, prompt building,
validation) are pure functions testable with no model and no network. The impure parts (one
model call, one file write, the gates) are thin.

## Components

### `trace.extract(entries) -> list[Step]`

Pure. Walks the active branch and yields ordered steps: user prompts, assistant tool calls with
their arguments, and outcomes. Oversized tool results are elided, which is honest since the log
already truncated them. No model, no I/O, no network.

`Step` is a dataclass carrying `kind` (`prompt` | `call` | `result`), `name`, `args` and `text`.

### `draft.build_prompt(steps) -> str`

Pure. Renders the trace into a drafting prompt that instructs the model to:

* generalise, lifting branch names, paths and literals into `$ARGUMENTS`;
* write a one-line `description` tight enough to earn its place in every future system prompt,
  since descriptions are injected every turn while bodies are not;
* omit anything that looks like a credential.

That last instruction is weak by itself and is not the control. See Security.

### The drafting call

One call through the active provider. It happens outside the live session, so the drafting
prompt never enters the user's history. It is gated (Gate 1 below).

**One budget of 3 drafting calls per `/skillify` invocation.** Invalid-draft retries and
refinements after a failed test draw from the same budget, because both are the same operation:
another live call to produce another draft. Two budgets would let a run alternate between them
and spend without limit. When the budget is exhausted the command stops cleanly and reports how
many attempts were made.

### `validate.is_loadable(text) -> (bool, reason)`

Pure. Frontmatter parses, `load_skill` returns a `Skill`, `name` matches `[\w.-]+`, description
non-empty. A draft never reaches the human unless it would actually load. Validation runs
**before** any gate that asks the human to judge content, so nobody is asked to approve
something that could not have worked.

### The tool-call gate

One handler registered at load time, inert unless a test run is active:

```python
async def on_tool_call(self, event, rt):
    if not self.testing:
        return None
    ok = await self.api.ui.ask("confirm",
        f"[skillify test] run {event['name']}?\n  {event['args']}")
    return None if ok else {"block": True, "reason": "declined during skillify test"}
```

A flag rather than dynamic registration, because the plugin API exposes `on` and no `off`. It
composes with `permission-gate`: both handlers run, either can block. The prompt shows the tool
name and its full arguments, because approving `shell` without seeing the command is theatre.

### `write.render_and_write(...)`

Renders frontmatter plus body, writes `.picoagent/skills/<name>/SKILL.md` relative to
`api.cwd`, and calls `api.register_skill` so the skill is usable in the same session.

## Data flow

```
/skillify [name]
  |
  +- extract(entries)                      pure, free
  +- print the trace                       the human sees what is about to be sent
  +- GATE 1  "send N steps to <provider>/<model>?"
  +- provider.stream(...)                  live call
  +- validate                              pure; invalid -> show why
  |    \- GATE 2  "retry? (attempt k of 3)"  -> back to the drafting call
  |
  +- GATE 3  "test this skill now?"        offered, not forced
  |    \- TEST RUN in a throwaway Session, real cwd
  |         every tool call -> its own approval
  |    \- GATE 4  "did that do what you wanted?"
  |         no  -> refine -> back to the drafting call (same 3-call budget)
  |         yes -> continue
  |
  +- print the rendered file and its destination path
  +- GATE 5  "write to .picoagent/skills/<name>/SKILL.md?"
  \- write + register
```

### Where the test run happens

In a **throwaway `Session`**, not the live one. A test run would otherwise append its whole
conversation to the user's session log, and the log is read-only by constraint 1.

Working directory is the **real `cwd`**. A process tested somewhere else is not tested.

### Skillify cannot run headless

Under `-p` the frontend answers `False` to `ask`, so the command refuses at Gate 1 before
spending anything. This is intended, and the refusal message says so rather than failing
silently.

## Error handling

| Condition | Behaviour |
|---|---|
| Empty or trivial session | Refuse before the model call. Do not spend tokens to say "nothing here" |
| Human declines Gate 1 | Nothing is sent. Zero provider calls |
| Provider error | Report it, write nothing |
| Draft is not loadable | Reject, show the reason, offer a gated retry from the shared 3-call budget |
| Human declines the test run | Skill may still be written, unverified, and the confirm text says so |
| A tool call is declined mid-test | That call is blocked with a reason; the run continues so the human sees the consequence |
| Human's verdict is "no" | Refine and re-draft from the shared budget; each is its own approval |
| Name collides with an existing skill | Ask before overwrite, never silently clobber |
| Name contains path separators | Reject. Constrain to `[\w.-]+` |

## Security

**Credential egress is the main risk, and it is not the file.** Session traces routinely contain
shell output, environment variables and file contents. The leak is that this material gets
**sent to a model**, which for a remote provider is the disclosure whether or not a file is ever
written. Gate 1 is therefore the real control: the trace is printed, then the human approves the
send. Asking the model to redact is a courtesy, not a mitigation.

**The written file lands in the repo.** `.picoagent/skills/` is committed. Gate 5 shows the
rendered bytes and the destination path, so the human approves content rather than a description
of content.

**Path traversal.** The skill name reaches a filesystem path. Constraining it to `[\w.-]+`, the
same class the `/skill:` invocation regex already uses, means an unwritable name is also an
uninvokable one.

**Testing executes for real.** There is no dry-run and there cannot be: exercising a deploy
skill deploys. Per-call approval is the entire mitigation. This is why the test run is offered
rather than forced, so a user can decline for a skill that is destructive by nature.

**A skill's description enters every future system prompt.** Generated skills accumulate context
cost and are a prompt-injection surface inside the user's own repo. The human gate and a tight
description instruction are the controls; skill sprawl is a known cost of the feature.

## Testing

Standard library `unittest`, using `ScriptedProvider` and `CaptureFrontend` from
`tests/helpers.py`. Free, offline, no network.

**Pure units**

1. `extract()` returns the exact expected `Step` list for a known session.
2. `extract()` elides oversized tool results.
3. `is_loadable()` accepts a well-formed draft and rejects: no frontmatter, missing name,
   missing description, bad name characters.

**Command behaviour**

4. Happy path: valid draft, all gates approved, file exists at the expected path, frontmatter
   parses, skill is registered in `rt.skills`.
5. Declined at Gate 5: no file on disk.
6. Invalid draft every time: nothing written.
7. Hostile name `../evil`: refused, and nothing exists outside the skills directory.
8. Collision, declined: the original file is untouched.
9. Empty session: assert `ScriptedProvider.calls == []`. No model call is spent.

**Approval invariants**

10. Declined at Gate 1: assert `ScriptedProvider.calls == []`. Session contents never leave the
    machine without consent. This is the most important test in the file.
11. The budget is bounded: a junk draft every time produces exactly 3 provider calls, then a
    clean give-up naming the attempt count.
12. Every retry is gated: count `ask` invocations against provider calls; they match.
13. Headless frontend: zero calls, clear message.

**Test-loop invariants**

14. Every tool call during a test run is gated: a scripted skill making 3 calls produces 3
    `ask` calls.
15. Declining a call blocks it: assert the tool never ran and no side effect landed on disk.
16. The handler is inert outside a test run: a normal turn produces zero extra `ask` calls.
17. The test run does not touch the live session: assert the real session's entry count is
    unchanged. This pins constraint 1.
18. Refinement is bounded and shares the retry budget: two invalid drafts followed by a
    "no" verdict exhausts the budget, and the command stops without a fourth call.

## Success criteria

* `/skillify` turns a completed session into a `SKILL.md` that loads, is registered live, and
  can be invoked as `/skill:<name>` in the same session.
* No model call and no tool call happens without an individual human approval.
* The live session log is byte-identical before and after a skillify run, including a run that
  exercised a skill.
* The full suite stays offline and free; no test here requires a live model.

## Deferred, in the order they would make sense

1. **Tool authoring.** Needs a trust story first: write to disk and require `plugin trust`
   before the tool can run, or register live and bypass the gate. Only the first is defensible.
2. **Recurrence detection.** Mine `Session.list()` for repeated work and propose codifying it.
   Reuses this drafting engine. Would benefit from persisted reasoning, which the log does not
   have, so it must infer intent from action sequences.
3. **A second entry point** named `/automate`, if the single command turns out to read wrong to
   users in practice.
