# picoagent

A minimal-core coding agent harness in Python — **standard library only, no third-party dependencies**. **Core = chat + skills + tools** (`read`, `write`, `edit`, `shell` - the shell tool auto-detects the platform: bash/sh on Linux and macOS, PowerShell on Windows).
Everything else — permissions, compaction, MCP, subagents, plan mode, TUI, hooks, LSP, worktrees — is a plugin
installed from a git repo or local path, and any core piece (tools, provider, frontend, prompt sections) can be
overridden by a plugin registering the same name.

```
pip install -e .
export PICOAGENT_BASE_URL=http://localhost:11434/v1   # any OpenAI-compatible server (Ollama, vLLM, OpenAI, gateway)
export PICOAGENT_API_KEY=...                           # optional
export PICOAGENT_MODEL=qwen2.5-coder:32b
picoagent                                  # REPL
picoagent -p "explain this repo"           # one-shot
picoagent -p "fix the failing test" --json # JSONL event stream
picoagent -e examples/plugins/permission-gate -e examples/plugins/compaction
picoagent plugin add git:github.com/you/some-plugin@v0.1.0
picoagent plugin list
```

Config (`~/.picoagent/config.toml`, then `.picoagent/config.toml` in the project):

```toml
model = "qwen2.5-coder:32b"
[providers.openai]
base_url = "http://localhost:11434/v1"
api_key = ""
[plugins]
enabled = ["git:github.com/you/permission-gate@v0.1.0", "./tools/my-local-plugin"]
[plugins.permission-gate]
mode = "ask"
```

Writing a plugin — a repo with `plugin.toml` + a module exposing `register(api)`:

```toml
name = "hello"
entry = "hello:register"
skills = ["skills"]        # optional SKILL.md dirs
python_deps = []           # optional pip deps
```
```python
def register(api):
    api.on("tool_call", lambda ev, rt: {"block": True, "reason": "nope"} if ev["name"] == "shell" and "rm -rf" in ev["args"].get("command", "") else None)
    api.register_command("hello", lambda args, rt: _hi(args), "say hi")
```

## Plugin trust (and what happens when a plugin changes)

Plugin code runs with your full privileges, so nothing loads until you've approved it. The
approval is a hash of `plugin.toml` plus the entry module — **so any edit to a plugin, including
your own, invalidates it and the plugin silently stops loading until you re-approve.** That is
deliberate: it's the same signal whether you edited the file yourself or an upgrade changed it
underneath you, and only you can tell those apart.

picoagent tells you which case you're in rather than making you guess:

```bash
picoagent plugin list          # trusted | CHANGED (approved before, but not this code) | UNTRUSTED (never approved)
picoagent plugin trust <path>  # shows what moved, then asks
```

Re-approving a **changed** plugin shows what you're actually accepting — which file moved, and
for a git checkout, the commits that arrived since you last approved:

```
*** This plugin CHANGED since you approved it. ***
Its code runs with your privileges, so review what moved before accepting:
  demo.py: modified
  commit b1d0389b4a4f -> df7158df6817
    df7158d add a tool_call hook

Accept the change and re-trust this plugin? [y/N]
```

Declining leaves it untrusted and unloaded. `plugin add` on an already-installed plugin goes
through the same review, since that's an upgrade. `-y`/`--yes` skips the prompt for scripting —
which means accepting the code unseen. At startup, any plugin that didn't load says so on stderr
rather than vanishing quietly.

`-e <path>` bypasses all of this, trusting the directory for that run only — handy while
developing a plugin, when re-approving after every edit would be noise.

## Example plugins

See `examples/plugins/`: `permission-gate`, `compaction`, `grok-provider`, `vertex-provider`, and `es-doctor` (an Elasticsearch diagnostics plugin that digs through Beats/Elastic Agent logs and correlates them with metrics and APM - a worked example of a domain-specific plugin).

## Companion plugin repos

* [picoagent-tools](https://github.com/opscontinuum/picoagent-tools) - stdlib-only tools: `mermaid_reference` and `mermaid_lint` for writing Mermaid diagrams that actually render.
* [picoagent-skills](https://github.com/opscontinuum/picoagent-skills) - curated, code-free `SKILL.md` packs, starting with `docs-and-diagrams`.

## Documentation

| Read this | If you want to |
|---|---|
| [docs/getting-started.md](docs/getting-started.md) | install, configure a model, run your first session |
| [docs/architecture.md](docs/architecture.md) | understand the loop, sessions, registries and trust |
| [docs/plugin-authoring.md](docs/plugin-authoring.md) | write tools, providers, commands, frontends |
| [docs/events-reference.md](docs/events-reference.md) | know every lifecycle event and what handlers may return |
| [docs/testing-and-conventions.md](docs/testing-and-conventions.md) | run/extend the tests, follow the code style |
| `harness-research-and-design.md` (separate file) | see the Claude Code / Codex / Pi / OpenCode research this distils |

## Tests

```bash
python -m unittest discover -s tests -v      # 85 tests, no network, ~3s
```

## Providers

`/model` shows the current model, `/model list` asks the provider what it offers (via
`GET /models`), and `/model <name>` switches. Listing is optional on the `Provider` protocol -
a provider that can't enumerate says so rather than pretending.

Only one provider ships in core: the OpenAI-compatible chat/completions client. The others are plugins:

| Provider | Wire format | How |
|---|---|---|
| OpenAI (and any OpenAI-compatible server) | `/v1/chat/completions` SSE | built-in, `--provider openai` |
| xAI Grok | same as OpenAI (`https://api.x.ai/v1`) | `examples/plugins/grok-provider`, `--provider grok` |
| Google Vertex AI (Gemini) | **different**: `:streamGenerateContent?alt=sse`, `contents`/`parts`/`functionCall` | `examples/plugins/vertex-provider`, `--provider vertex` |

## Fakes & tests

`picoagent/testing/fakes.py` has stdlib fake servers for all three dialects (they validate paths, auth
headers, request shape, and Gemini schema restrictions, then play a scripted text → tool call → reply):

```
python -m picoagent.testing.fakes --dialect openai --port 8765   # or grok | vertex
PICOAGENT_BASE_URL=http://127.0.0.1:8765/v1 picoagent -p "hello"
python -m unittest discover -s tests -v                          # full loop against each fake
```
