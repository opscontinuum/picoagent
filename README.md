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

## What files picoagent reads

Relevant if you're deploying somewhere that has to account for every file an agent touches.
Two config keys control everything picoagent reads on its own initiative:

```toml
context_files = ["AGENTS.md", ".picoagent/AGENTS.md"]        # always-on instructions
skill_dirs    = ["skills", ".picoagent/skills", ".agents/skills"]
```

Those are the defaults, and they are the complete list. **No vendor-specific paths are read.**
Other harnesses' conventions (`CLAUDE.md`, `.claude/skills`) are deliberately *not* in the
defaults — see the comment above the lists in `picoagent/core/config.py`. Anything else the
agent reads, it reads because the model called the `read` tool and you can see that in the
session log.

Both keys are plain lists, and a value in `config.toml` **replaces** the default rather than
merging with it. So you can narrow or disable them:

```toml
# read nothing implicitly - only files the model explicitly opens with a tool
context_files = []
skill_dirs    = []

# or pin to exactly what you allow
context_files = ["AGENTS.md"]
skill_dirs    = ["/opt/approved-skills"]
```

To re-enable interop with another harness's files, add its paths back:

```toml
context_files = ["AGENTS.md", ".picoagent/AGENTS.md", "CLAUDE.md"]
skill_dirs    = ["skills", ".picoagent/skills", ".agents/skills", ".claude/skills"]
```

Both are read-only conventions — nothing else in picoagent changes either way.

Network reads are separately bounded: the core makes exactly one kind of outbound request,
`POST {base_url}/chat/completions` to whichever server you configure (plus `GET {base_url}/models`
when you run `/model list`). Point `base_url` at a local or on-prem endpoint and it never talks
to anything else. `plugin add` runs `git clone`/`git fetch` against whatever git host you name,
which can be an internal server.

## Keeping plugins up to date

```bash
picoagent upgrade check     # what is behind, changing nothing
picoagent upgrade           # fast-forward every outdated plugin
picoagent upgrade <name>    # just one
```

Everything goes through `git` itself - `ls-remote`, `fetch`, `merge --ff-only` - never a
host's API, so it works the same against GitHub, GitLab, Gitea, Bitbucket Server, a bare SSH
remote, or a `file://` path on a shared mount. No tokens, no host-specific code.

An upgrade **never approves itself**. Fast-forwarding a plugin changes its trust fingerprint,
so the next run reports it as `CHANGED` and shows you the incoming commits before you accept.
Upgrading and trusting are separate acts on purpose.

It also refuses rather than resolving: a checkout with uncommitted changes, or one that has
diverged from its remote, is left alone and reported. Losing an edit someone was mid-way
through, to apply an upgrade they hadn't asked for yet, is the worse outcome.

**picoagent itself is only reported on, never modified.** A git checkout, a pip install and a
distro package upgrade differently, and guessing wrong breaks the install - so it tells you how
far behind you are and prints the command for how you actually installed it.

```toml
[upgrade]
check_on_startup = false                          # opt-in; costs a round trip per plugin
app_repo = "git:git.internal.corp/mirrors/picoagent@main"   # enables the app check
```

### Internal git servers

Specs take any URL form, including SSH, which is the usual shape for an internal server:

```toml
[plugins]
enabled = [
  "git:git.internal.corp/team/permission-gate@v0.1.0",
  "git@git.internal.corp:team/some-plugin.git@main",
  "file:///srv/mirrors/picoagent-tools@main",
]
```

To retarget upstream specs at a mirror without editing each one - which is what an air-gapped
site actually needs - map a url prefix:

```toml
[plugins.rewrite]
"github.com/opscontinuum" = "git.internal.corp/mirrors/opscontinuum"
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

In `examples/plugins/`. All are standard-library only except `doc-pdf`, which is the only plugin in the repository that declares a `python_deps`.

| Plugin | Does |
|---|---|
| `permission-gate` | Holds protected paths behind a user prompt |
| `compaction` | Summarises the conversation when the context window fills |
| `credential-guard` | Keeps credential files out of tool reads and out of the prompt |
| `keyring-secrets` | Stores provider keys in the OS keyring instead of a config file |
| `grok-provider`, `vertex-provider` | Additional model providers |
| `es-doctor` | Elasticsearch administration and troubleshooting over the API - digs through Beats/Elastic Agent logs and correlates them with metrics and APM |
| `stig-runner` | Runs a DISA ASD STIG from a `.ckl` against a repository, with a human deciding every determination |
| `iscp-author` | Interview-driven FedRAMP ISCP, BIA and DR runbooks, with a provenance-checked renderer |
| `doc-ooxml` | Reads `.docx` and `.xlsx` as evidence: confirms a quotation resolves to a heading or a cell, tells a Word template's italic instructions from its content, and shows which sections are still empty. Standard library only |
| `doc-pdf` | Reads a PDF as evidence: confirms a quotation resolves to a page before it is cited. **Needs PyMuPDF (AGPL-3.0 or commercial)** |

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
| [docs/engineering/](docs/engineering/) | see the diagrams: modules, request lifecycle, data model, plugin trust flow |
| [docs/security/](docs/security/) | trust boundaries, and where a credential can and cannot travel |
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
