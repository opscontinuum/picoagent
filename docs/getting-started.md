# Getting started

picoagent is a coding agent you run in a terminal. It reads your repo, edits files and runs
commands, driven by whatever model you point it at. The core is small on purpose; most
behaviour comes from plugins you choose.

## Requirements

* Python 3.11 or newer. Nothing else - the package has no third-party dependencies.
* A model server that speaks the OpenAI chat-completions API. That includes OpenAI itself,
  a local Ollama / vLLM / llama.cpp / LM Studio, OpenRouter, or a company gateway.
  (Gemini on Vertex AI and xAI Grok are covered by the example plugins.)

## Install

```bash
git clone <your fork> picoagent && cd picoagent
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .            # registers the `picoagent` command
```

With the venv active, `picoagent` is on your PATH. Without activating it, run
`.venv/bin/picoagent` instead. There is nothing to download here: the package has no
third-party dependencies, so the venv stays small and the install finishes in a second.

### If pip says `externally-managed-environment`

Debian 12+, Ubuntu 23.04+, Fedora 38+ and Homebrew Python all mark the system Python as
externally managed (PEP 668), so pip refuses to install into it:

```
error: externally-managed-environment
```

The venv above is the fix, and it is why these instructions start with one.

### If `python3 -m venv` fails

On Debian and Ubuntu the venv module ships in a separate package, so creating the venv
can fail before you ever get to pip:

```bash
sudo apt install python3-venv    # or python3-full, which the pip error suggests
```

If the error names a versioned package (`python3.12-venv`, say), install that one, then
create the venv again.

### Or install it with pipx

If you want the `picoagent` command on your PATH permanently, without activating a venv
every time, use pipx. It creates and manages the venv for you, and picoagent is a good
fit for it: an application with a console-script entry point and no dependencies.

```bash
sudo apt install pipx    # or: brew install pipx
pipx ensurepath          # puts pipx's bin directory on your PATH; start a new shell after this
pipx install -e .        # from the repo root; -e keeps it editable
```

Older pipx versions want `--force` to reinstall over an existing copy:
`pipx install -e . --force`.

### Last resort: `--break-system-packages`

`pip install -e . --break-system-packages` overrides the PEP 668 refusal. It writes into
the Python your operating system depends on, where it can shadow or overwrite
distro-installed packages and break system tools that rely on them. A venv or pipx costs
one extra line and carries none of that risk.

## Point it at a model

Three environment variables are enough for a first run:

```bash
export PICOAGENT_BASE_URL=http://localhost:11434/v1   # e.g. Ollama
export PICOAGENT_MODEL=qwen2.5-coder:32b
export PICOAGENT_API_KEY=                             # empty is fine for local servers
```

Or write the same thing once in `~/.picoagent/config.toml`:

```toml
model = "qwen2.5-coder:32b"

[providers.openai]
base_url = "http://localhost:11434/v1"
api_key = ""
```

## First session

```bash
cd your-project
picoagent
```

You get a prompt. Type what you want done. Useful things to know:

| You type | What happens |
|---|---|
| `explain the auth module` | the model reads files and answers |
| `/help` | lists commands and skills |
| `/skill:deploy staging` | expands the `deploy` skill and runs it with `staging` as `$ARGUMENTS` |
| `!git status` | runs a shell command for *you*; the model doesn't see it |
| `/model gpt-4.1` | switches model for the rest of the session |
| `/new` | starts a fresh session file |
| `/exit` or Ctrl-D | leaves |

Sessions are saved as JSONL under `~/.picoagent/sessions/<project>/`. Resume the last one with
`picoagent -r`.

## Scripting

```bash
picoagent -p "fix the failing test in tests/test_api.py"      # prints the final answer
picoagent -p "list the public functions in src/" --json       # one JSON event per line
echo "summarise this" | picoagent -p -                         # prompt from stdin
```

`--json` gives you every tool call and result, which is handy for CI logs.

## Adding behaviour

Out of the box the agent will run *any* shell command without asking. Most people want at
least the permission gate:

```bash
picoagent -e examples/plugins/permission-gate          # try it for one run
picoagent plugin add ./examples/plugins/permission-gate  # install + trust it
```

Then list it in your config so it loads every time:

```toml
[plugins]
enabled = ["./examples/plugins/permission-gate", "./examples/plugins/compaction"]
```

Plugins can also come straight from git: `picoagent plugin add git:github.com/you/repo@v1.0.0`.
See [plugin-authoring.md](plugin-authoring.md) to write your own and
[architecture.md](architecture.md) for how it all fits together.

## Where things live

```
~/.picoagent/
  config.toml      user-wide settings
  trust.json       plugins you've approved (hash-pinned)
  plugins/         plugins installed with `plugin add`
  skills/          your personal SKILL.md folders
  sessions/        one folder per project, JSONL files inside
<project>/.picoagent/
  config.toml      team settings, safe to commit
  plugins/         project plugins (installed with `plugin add --project`)
  skills/          project skills (also read from skills/, .agents/skills)
<project>/AGENTS.md   always-on instructions for the model
```
