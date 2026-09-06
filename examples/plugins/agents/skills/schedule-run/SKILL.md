---
name: schedule-run
description: Schedule an agent to run unattended later: cron, systemd user timers, Windows Task Scheduler
---
# Scheduling an unattended run

picoagent is a short-lived process. It starts, answers one prompt, exits. There is no daemon
and no scheduler anywhere in the codebase, so "run this every night" means asking the operating
system to invoke `picoagent -p` later. Your job is to build a command that survives having no
terminal and no shell profile, show it to the user, and install it only after they say yes.

Work through the steps in order. Do not skip step 1: a schedule that installs cleanly and then
fails silently at 03:00 is worse than no schedule.

## 1. Build the command, then run it by hand

Three flags matter for an unattended run:

| Flag | Why it matters here |
|---|---|
| `-p "<prompt>"` | the whole run: one prompt, printed answer, exit. `-p -` reads the prompt from stdin |
| `-C <dir>` | the project directory. A scheduled process starts wherever the scheduler put it, which is rarely your repo |
| `--json` | one JSON object per line covering tool calls, results and errors. Use it when a program parses the run, not when a person reads it |

Use an absolute path to the binary. `command -v picoagent` (or `where picoagent` on Windows)
gives it; a pipx install usually lands in `~/.local/bin/picoagent`, a venv install in
`<venv>/bin/picoagent`. `PATH` under cron is typically `/usr/bin:/bin` and nothing else, so a
bare `picoagent` is the most common reason a scheduled run never happens.

Prove the command works with almost no environment before scheduling anything:

```bash
env -i HOME="$HOME" PATH=/usr/bin:/bin \
  /home/me/.local/bin/picoagent -C /srv/reports -p "list the files you would change"
```

If that fails, fix it in step 2. Scheduling it will not fix it.

Leave `-r` off. Without it each run opens a fresh session under
`~/.picoagent/sessions/<project-path>/`, which is what you want for a job that repeats.

## 2. Make the model and the endpoint resolve with no interactive shell

The run has no session to inherit settings from, so everything has to come off disk or out of
the scheduler's own environment.

| Setting | Where it is read from |
|---|---|
| user config | `~/.picoagent/config.toml`, or `$PICOAGENT_HOME/config.toml` when `PICOAGENT_HOME` is set |
| project config | `<project>/.picoagent/config.toml`, merged on top, but it may not set `providers`, `context_files`, `skill_dirs`, `confine_to_project`, `plugins.rewrite` or `upgrade` |
| endpoint and key | `[providers.openai] base_url` / `api_key` in the user config, else `PICOAGENT_BASE_URL` or `OPENAI_BASE_URL`, else `PICOAGENT_API_KEY` or `OPENAI_API_KEY`, else `https://api.openai.com/v1` |
| per-service credentials | `~/.picoagent/endpoints/*.toml`, one file per service, read from the user directory only |
| model | `model` in config, `-m`, or `PICOAGENT_MODEL` |

Two rules follow:

1. **Prefer the config file to environment variables.** A key set in `~/.picoagent/config.toml`
   is still there at 03:00; a key exported from `.bashrc` or `.zshrc` is not, because a
   scheduled run sources no profile. If you need environment variables, set them in the
   crontab, the systemd unit or the `.cmd` wrapper, where the run can see them.
2. **If your profile sets `PICOAGENT_HOME`, set it in the schedule too.** Otherwise the run
   reads `~/.picoagent` and picks up different settings, a different model, and possibly no API
   key at all.

Check that the account the job runs as can read those files. A job running as another user, or
as `SYSTEM`, gets a different home directory and finds nothing.

Plugins the job needs must load without a prompt: enable them in the user config's
`[plugins] enabled` list, or pass `-e /absolute/path/to/plugin` to trust one for that run. An
untrusted plugin is skipped with a note on stderr and the run continues without it.

## 3. cron

One line, five time fields, then the command. Redirect both streams or the output goes to a
mail spool nobody reads.

```
17 3 * * 1-5 /home/me/.local/bin/picoagent -C /srv/reports -p "Summarise yesterday's errors into report.md" >> /home/me/logs/picoagent-$(date +\%Y-\%m).log 2>&1
```

Two things about that line are easy to get wrong:

- **Escape every `%`.** In a crontab an unescaped `%` becomes a newline, and everything after
  the first one is fed to the command as standard input. `date +\%Y-\%m` is correct;
  `date +%Y-%m` truncates the command.
- **Set what you need at the top of the crontab**, not in a profile. `SHELL=/bin/bash`,
  `PATH=...` and any `PICOAGENT_*` variables go as `NAME=value` lines above the schedule lines.

Add a line without destroying the existing crontab:

```bash
crontab -l > /tmp/crontab.bak 2>/dev/null || :
cp /tmp/crontab.bak /tmp/crontab.new
printf '%s\n' '17 3 * * 1-5 /home/me/.local/bin/picoagent ...' >> /tmp/crontab.new
crontab /tmp/crontab.new    # replaces the whole crontab with the edited copy
crontab -l                  # read it back and confirm
```

`crontab -l` lists and `crontab -e` edits. `crontab -r` deletes the user's **entire** crontab
with no confirmation; to remove one job, edit the file and reinstall it, and keep
`/tmp/crontab.bak` until the user has confirmed the result.

## 4. systemd user timers

Better than cron when you want the run tied to a unit, a log in the journal, and a catch-up
after the machine was off. Two files under `~/.config/systemd/user/`.

`picoagent-nightly.service`:

```ini
[Unit]
Description=Nightly picoagent report

[Service]
Type=oneshot
WorkingDirectory=/srv/reports
Environment=PICOAGENT_HOME=/home/me/.picoagent
ExecStart=/home/me/.local/bin/picoagent -C /srv/reports -p "Summarise yesterday's errors into report.md"
StandardOutput=append:/home/me/logs/picoagent-nightly.log
StandardError=append:/home/me/logs/picoagent-nightly.log
```

`picoagent-nightly.timer`:

```ini
[Unit]
Description=Run the nightly picoagent report

[Timer]
OnCalendar=*-*-* 03:17:00
Persistent=true

[Install]
WantedBy=timers.target
```

`ExecStart` needs the absolute binary path; systemd does not search a login `PATH`.
`Persistent=true` runs a missed job once the machine comes back. `append:` needs systemd 240 or
newer; on older systems drop those two lines and read the output with `journalctl`.

Check the schedule before installing anything. This costs nothing and changes nothing:

```bash
systemd-analyze calendar "*-*-* 03:17:00"    # prints the normalised form and the next elapse
```

Install, test, inspect:

```bash
systemctl --user daemon-reload
systemctl --user enable --now picoagent-nightly.timer
systemctl --user list-timers picoagent-nightly.timer
systemctl --user start picoagent-nightly.service    # run it once now, to test
journalctl --user -u picoagent-nightly.service -n 50
```

A user timer only runs while the user has a session, unless lingering is on:
`loginctl enable-linger "$USER"`. Say so before the user finds out by way of a job that never
fires on a headless box.

Remove:

```bash
systemctl --user disable --now picoagent-nightly.timer
rm ~/.config/systemd/user/picoagent-nightly.timer ~/.config/systemd/user/picoagent-nightly.service
systemctl --user daemon-reload
```

## 5. Windows Task Scheduler

A task runs exactly one program, and the `schtasks /create` parameters include no flag for the
task's working directory, so put the run in a `.cmd` wrapper. That also keeps the quoting out
of `/tr`, whose path is limited to 262 characters.

`C:\reports\picoagent-nightly.cmd`:

```bat
@echo off
cd /d C:\reports
"C:\Users\me\.local\bin\picoagent.exe" -C C:\reports -p "Summarise yesterday's errors into report.md" >> C:\reports\logs\picoagent.log 2>&1
```

Create, test, inspect, remove:

```bat
schtasks /create /tn "picoagent nightly" /tr "C:\reports\picoagent-nightly.cmd" /sc daily /st 03:17 /rl LIMITED /np
schtasks /run    /tn "picoagent nightly"
schtasks /query  /tn "picoagent nightly" /fo LIST /v
schtasks /delete /tn "picoagent nightly" /f
```

- `/rl LIMITED` runs with standard-user privileges and is the default. Use `/rl HIGHEST` only
  when the user has said the job needs it.
- `/np` stores no password and runs the task non-interactively, with local resources only.
  Without it, `schtasks` prompts for the account's password.
- `/f` on `/create` overwrites an existing task of the same name and suppresses the warning.
  Leave it off unless replacing a task is the intent.
- `schtasks` does not verify that the program exists, so a typo produces a task that is created
  successfully and never runs. `/run` straight after creating is the check.
- Task names containing spaces need quotes, and cannot exceed 238 characters.

## 6. Capture the output, because nobody is watching

Every example above appends rather than truncates, and sends stderr to the same file. Do the
same in anything you propose.

- A person reads the log: plain `-p` output, appended, one file per month so it does not grow
  without bound.
- A program reads the log: add `--json`. Each line is one event object (assistant text deltas,
  tool calls, tool results, errors), which is what to parse for "did the run do anything" and
  "did it fail", rather than scraping prose.
- The session transcript is written either way, under
  `~/.picoagent/sessions/<project path with slashes replaced>/<timestamp>.jsonl`. Point the user
  at it when a run needs reconstructing.
- Create the log directory before the first run. A redirect into a directory that does not exist
  fails, and it fails where nobody sees it.

## 7. Never install a schedule silently

In this order, every time:

1. Print the exact line, unit file, or `.cmd` content that will be installed. Not a paraphrase.
2. Say what it will do, how often, as which user, and where the output goes.
3. Ask for approval and wait for it.
4. Back up the existing schedule (`crontab -l > backup`, or
   `schtasks /query /tn "..." /xml > backup.xml`), then install.
5. Read the schedule back (`crontab -l`, `systemctl --user list-timers`, `schtasks /query`) and
   show the user the result.

A crontab or a timer directory is shared state that the user's other jobs live in. Rewriting it
from memory instead of from `crontab -l` loses those entries.

## 8. The risk that decides how much a scheduled run may do

**Nobody is present to approve a tool call.** picoagent ships `read`, `write`, `edit` and
`shell`, and none of them asks for confirmation on its own. Scheduled at 03:00, the model can
edit files and run shell commands with the full privileges of the account the job runs as, and
the first anyone hears about it is the log.

The headless frontend answers every confirmation with "no", so a plugin that does ask, such as
`permission-gate`, blocks rather than proceeds. That is the behaviour you want, and it is worth
arranging deliberately:

- Load `permission-gate` in the scheduled run. `mode = "readonly"` refuses `write`, `edit` and
  `shell` outright; `mode = "ask"` blocks its dangerous-command patterns, because there is
  nobody to say yes. Add the paths the job must never touch to `protected`.
- Set `confine_to_project = true` so `read`, `write` and `edit` refuse anything outside the
  project directory. This one goes in **your** config: a repository is not allowed to set it, so
  a repository cannot switch it off either.
- Run the job as an account that owns only what the job needs, not as your daily user, and not
  as root or `SYSTEM`.
- Write a narrow prompt. "Summarise yesterday's errors into report.md" is a job for an
  unattended run. "Fix whatever is broken and push it" is not.
- Prefer a run that reports over one that commits, deploys, or deletes. A person reads the
  report in the morning and decides.

State this risk to the user before installing anything, and name what the job will be allowed to
do. $ARGUMENTS
