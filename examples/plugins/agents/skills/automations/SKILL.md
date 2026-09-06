---
name: automations
description: Automate a task, run it every night, or act when a file changes: a trigger bound to an agent run
---
# Binding a trigger to an agent run

An automation is two halves: a trigger that says when, and an action that says what. picoagent
has neither. It starts, answers one prompt, exits; nothing in it waits, watches or fires. What it
has is three ways to run an agent and one place a trigger can come from, and an automation is
those pieces wired together.

This skill is the wiring, and only the wiring. `delegate`, `background-run` and `schedule-run`
cover the halves; read whichever one the trigger sends you to rather than expecting it here.

## 1. Pick the trigger, and know who owns it

| Trigger | Who fires it | What it costs |
|---|---|---|
| a time ("every night at 03:00", "weekdays at 08:00") | the operating system | a schedule entry |
| a session event ("after every turn", "before any shell call") | picoagent's event bus | a plugin, so Python |
| a file or repository change | nobody, there is no watcher | a scheduled poll, or an OS file-watch tool |

### A time

This is `schedule-run`, whole. It covers building a command that survives having no terminal,
making the model and the endpoint resolve with no shell profile, cron, systemd user timers,
Windows Task Scheduler, where the output goes, and the sequence for installing a schedule with
the user's approval. None of that changes because the schedule is part of an automation. Read it
and follow it.

Composition adds one thing: once the run needs to remember what it already did (section 4), the
thing you schedule is a wrapper script, not a bare `picoagent -p` line. The scheduler runs the
wrapper; the wrapper runs picoagent.

### A session event

"After every agent turn, run the tests" is not a time and not a file change. It is a rule about a
running session, and picoagent's event bus is what carries it.

**A skill cannot arrange this, and you should say so rather than improvise.** A skill is text that
gets loaded when it is invoked. It has no way to register a handler and nothing calls it when
something happens. A rule that fires on an event needs a plugin: a `register(api)` function that
subscribes, which is about ten lines of Python.

```python
# ops-automation/ops_automation.py
def register(api):
    api.on("agent_settled", after_each_prompt)

async def after_each_prompt(event, rt):
    ...                      # the action goes here
```

With a `plugin.toml` beside it (see docs/plugin-authoring.md), `picoagent -e ./ops-automation`
loads it for one run, and `[plugins] enabled` in config.toml loads it every time.

Which event carries the rule:

| The rule, in English | The event |
|---|---|
| after each model round, tool calls included | turn_end, which fires many times in one prompt |
| once the model has stopped calling tools | agent_end |
| once the run is finished and follow-ups have drained | agent_settled, the usual reading of "after every turn" |
| before a tool runs, to rewrite its arguments or refuse it | tool_call, returning `{"block": True, "reason": "..."}` |
| when the session opens or closes | session_start, session_end |
| when the user submits text, before commands and skills are handled | input |

docs/events-reference.md has the full list with payloads and what each handler may return.

Three properties of handlers decide whether an event-triggered automation is a good idea:

- **A handler that raises is logged and skipped.** The session carries on. A broken automation is
  a silent one, so catch your own failures and say something.
- **It runs inside the user's turn.** Keep it cheap. If the action needs a model, either put the
  work back into the running agent with `api.send_message("...", deliver_as="follow_up")`, or
  start a detached run (`background-run`) and leave the answer in a file.
- **An action bound to turn_end runs on every turn.** A model call in that handler roughly doubles
  the cost of every turn in the session, forever, and the user did not ask for it at the time.

### A file or repository change

picoagent has no watcher, no daemon and nothing that polls. This is the operating system's job,
and there are two ways to give it that job.

**A scheduled poll.** Nothing to install: the trigger is a scheduled run that compares state
against a marker and exits early when nothing moved.

```sh
newrev=$(git -C /srv/site rev-parse HEAD)
lastrev=$(cat "$state" 2>/dev/null || true)
[ "$newrev" = "$lastrev" ] && exit 0
```

For a tree with no version control, the marker file's own timestamp is the comparison:
`find docs -type f -newer "$state" | head -1` prints nothing when nothing changed, as long as the
marker is touched on every run. A poll cannot miss a change, because it compares state rather than
listening for events, and it costs one cheap command per interval. Default to it.

**An OS file-watch tool**: `inotifywait` from inotify-tools on Linux, `fswatch` on macOS. Faster
to react, with three traps that a scheduled poll does not have:

- **One save is several events.** Editors write, rename and touch; a `git checkout` or a `git pull`
  fires hundreds. Without a debounce that is hundreds of agent runs.
- **The agent's own writes fire the watcher again.** An automation that edits files inside the
  tree it watches retriggers itself, and each run gives it something new to react to. Write the
  output outside the watched tree, or exclude the output path in the watch.
- **A watcher is a process, and processes die.** Nothing restarts it after a reboot, a logout or
  a crash, and it fails by going quiet. A schedule comes back; a hand-started watcher does not.

## 2. The action

Whatever fires it, the action is a prompt or a skill invocation, run one of three ways.

| Runner | Where it runs | Reach for it when |
|---|---|---|
| the `agent` tool | in this process, capped at a turn count, answer returned as the tool result | you want the answer inside this turn |
| `background-run` | a detached process writing to a log you read later | it must not block, and you are still here to collect it |
| `schedule-run` | started by the operating system, no session around it | nobody is present |

The trigger usually picks the runner for you: an event handler is inside a live session so it has
the first two, and a schedule or a poll has only the third.

**A skill invocation is a valid action.** `picoagent -p` goes through the same input handling the
REPL uses, so a prompt of the form `/skill:<name> <args>` expands to that skill's body with
`$ARGUMENTS` replaced:

```
picoagent -C /srv/site -p "/skill:changelog-draft abc1234..def5678"
```

The run has to be able to find the skill: under one of the project's `skill_dirs`, under
`~/.picoagent/skills`, or shipped by a plugin that run has enabled. A scheduled process reads no
shell profile and may not read the home directory you expect, so prove the exact command works by
hand before it goes into a schedule.

**Keep the action one job with one output.** Nobody reads an automation's prompt after the day it
is written, and nobody is there to answer a question about it. Ambiguity you would clear up in
conversation gets acted on instead.

## 3. One automation, end to end

The job: every morning, summarise the commits that landed since the last time this ran, into a
changelog draft a person reviews. Trigger is a time, action is a prompt, state is the last commit
already summarised.

**The wrapper script**, `/srv/site/ops/changelog-draft.sh`. It owns the state, and picoagent owns
only the part that needs judgement:

```sh
#!/bin/sh
# Draft changelog entries for commits since the last successful run. Installed in cron.
set -e
project=/srv/site
state=$HOME/.picoagent-automations/site-changelog.rev
log=$HOME/logs/site-changelog.log
mkdir -p "$(dirname "$state")" "$(dirname "$log")"

newrev=$(git -C "$project" rev-parse HEAD)
lastrev=$(cat "$state" 2>/dev/null || true)

if [ -z "$lastrev" ]; then          # first run: record where we are, summarise nothing
  printf '%s\n' "$newrev" > "$state.tmp" && mv "$state.tmp" "$state"
  echo "$(date -Is) first run, marker set to $newrev" >> "$log"
  exit 0
fi
if [ "$lastrev" = "$newrev" ]; then
  echo "$(date -Is) nothing new" >> "$log"
  exit 0
fi

if /home/me/.local/bin/picoagent -C "$project" -p "Run: git log --no-merges $lastrev..$newrev
Write one section for docs/changelog-draft.md describing what changed, grouped by area, in the
style of the sections already in that file. Put it under a heading with today's date, at the top.
Change no other file, and commit nothing." >> "$log" 2>&1
then
  printf '%s\n' "$newrev" > "$state.tmp" && mv "$state.tmp" "$state"
  echo "$(date -Is) summarised $lastrev..$newrev" >> "$log"
else
  echo "$(date -Is) FAILED, marker left at $lastrev" >> "$log"
fi
```

The marker moves only on a clean exit, so a failed run leaves the range for the next one to retry
rather than losing a day of commits.

**The schedule** is one cron line pointing at the wrapper, installed the way `schedule-run`
section 7 says: print it, say what it does and as whom, get approval, back the crontab up,
install, read it back.

```
23 6 * * 1-5 /srv/site/ops/changelog-draft.sh >> /home/me/logs/site-changelog.log 2>&1
```

**Where output goes.** Three places, and the user should know all three: the draft itself in
`/srv/site/docs/changelog-draft.md` as an uncommitted edit for a person to review, the run log at
`~/logs/site-changelog.log`, and the full transcript under
`~/.picoagent/sessions/<project path with slashes replaced>/<timestamp>.jsonl` when a run needs
reconstructing.

**How the user checks it worked.** Before the schedule exists, run the wrapper by hand with almost
no environment, which is the closest thing to what cron will do:

```sh
env -i HOME="$HOME" PATH=/usr/bin:/bin sh /srv/site/ops/changelog-draft.sh
cat "$HOME/.picoagent-automations/site-changelog.rev"     # should now be HEAD
sh /srv/site/ops/changelog-draft.sh && tail -2 "$HOME/logs/site-changelog.log"   # "nothing new"
```

Then land a commit and run it a third time: the log says `summarised`, `git -C /srv/site diff
docs/changelog-draft.md` shows the new section, and the marker has moved to the new HEAD. Once the
schedule is installed, the morning check is those same two things: the last line of the log, and
the marker against `git rev-parse HEAD`.

## 4. State between runs

picoagent remembers nothing for you between runs. Each run opens a fresh session, so run N+1 has
no idea what run N did. An automation that cannot tell what it already handled either repeats
itself every night or skips whatever happened while it was failing.

The marker file is that memory. It is an ordinary file you create and maintain; nothing in
picoagent knows about it.

1. **The wrapper owns it, not the model.** The model may or may not do what the prompt asked, and
   a run that half-failed must not record success. Read the marker before the run, pass what the
   model needs in the prompt, write the marker after the run, in the script.
2. **Advance it only after a clean exit**, and after whatever the run was supposed to produce
   actually exists. A marker that moves on failure turns one bad night into permanently skipped
   work.
3. **Write it atomically**: to `$state.tmp`, then `mv`. A crash halfway through a write otherwise
   leaves a marker that reads as garbage or as empty.
4. **Keep it outside the repository** unless the whole team is meant to share it. A marker inside
   the tree gets reverted by a checkout, removed by `git clean -xdf`, and committed by accident.
5. **Keep it to one boring line**: a commit SHA, an ISO timestamp, a count. Not prose, and not
   something the model writes.
6. **Decide what a missing marker means, and write that branch first.** Almost always it means
   "first run: record the current state and do nothing". The other reading, an empty marker as
   "process everything", is how an automation's first night sends four thousand summaries.

Two things that look like state and are not. Resuming a session with `-r` is not a memory store:
the whole transcript is resent on every turn, so cost grows without bound and last week's context
steers tonight's run. And a plugin's `api.append_entry` is per-session, so an unattended run that
starts fresh each night cannot read what an earlier one wrote.

## 5. When an automation is the wrong answer

If a plain script does the job, write the plain script. It is cheaper, it does the same thing
every time, it fails loudly instead of plausibly, and it cannot be talked into anything.
`git log --since=yesterday --oneline >> report.md` in cron is not an automation, and it does not
need a model.

An agent earns its cost when the input is unstructured and the task needs judgement: reading
free-text errors and grouping them, deciding which commits are worth mentioning and in what words,
summarising a document that changed. Everything else in the run should be script.

The test: write down exactly what a correct run outputs. If you can, that description is a
program, so write it as one. If you cannot, because the right output depends on what the input
turns out to say, an agent is the right tool for that part of it.

An automation is also wrong when the result has to be correct every time (an agent varies run to
run, and nobody is checking tonight's), when the action is destructive, when the trigger fires
often enough that the cost per fire matters, and when nobody will read the output. An automation
nobody reads is money spent to fill a log.

## 6. The risk, inherited and multiplied

`schedule-run` section 8 states the inherited risk and its mitigations: nobody is present to
approve a tool call, the headless frontend answers every confirmation with "no", so load
`permission-gate` in `readonly` or `ask` mode, set `confine_to_project = true` in your own config,
run as an account that owns only what the job needs, write a narrow prompt, and prefer a run that
reports over one that commits or deletes. Apply that list here rather than a shorter version of
it.

What an automation adds is repetition and unattended input.

- **A bad automation is a bad decision repeated on a schedule.** The first bad run is the cheap
  one, and it is the one nobody reads. Review the log for the first week rather than at the first
  complaint, and keep the action to one that produces something a person approves.
- **The trigger chooses the input, and you do not choose the trigger.** A file-change or poll
  automation runs on whatever arrived: a pulled commit, a log line, a document somebody else
  wrote. Never build the prompt out of that content. Pass identifiers (a commit range, a path)
  and let the run read the content as data, inside a prompt you wrote. Text reaching the model as
  instructions, from a source neither of you controls, is the whole of a prompt injection.
- **Silence is not success.** A schedule that stopped firing, a watcher that died and a marker
  that stopped moving all look identical from outside: nothing happens. That is why the wrapper
  logs a line on every path, including "nothing new", and why the check is the log's last
  timestamp rather than the absence of complaints.

Say all of this to the user before installing anything, along with what the automation will be
allowed to do and how they turn it off. $ARGUMENTS
