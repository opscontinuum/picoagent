# picoagent

A minimal-core coding agent harness in Python — **standard library only, no third-party dependencies**. **Core = chat + skills + tools** (`read`, `write`, `edit`, `shell` - the shell tool auto-detects the platform: bash/sh on Linux and macOS, PowerShell on Windows).
Everything else — permissions, compaction, MCP, subagents, plan mode, TUI, hooks, LSP, worktrees — is a plugin
installed from a git repo or local path, and any core piece (tools, provider, frontend, prompt sections) can be
overridden by a plugin registering the same name.

There is nothing to install. Clone it and run it:

```
git clone https://github.com/opscontinuum/picoagent && cd picoagent
python3 -m picoagent setup                            # asks what to point at, writes it down
python3 -m picoagent                                  # REPL
python3 -m picoagent -p "explain this repo"           # one-shot
python3 -m picoagent -p "fix the failing test" --json # JSONL event stream
python3 -m picoagent -e ../picoagent-plugins/permission-gate    # load a plugin for one run
python3 -m picoagent plugin add git:github.com/you/some-plugin@v0.1.0
python3 -m picoagent plugin list
```

`setup` asks which provider, what its endpoint and key are, and which model, then writes them into
`~/.picoagent/config.toml` and makes that file readable only by you. It asks the *provider* what it needs,
so a dialect a plugin registered appears in the list with its own settings — Vertex asks for a project and a
location, not for a bearer token. The last option in the list is *a new provider*: name it, pick its wire
format, and setup writes the `[providers.<name>]` table that rebuilds it. If you would rather not be asked, the three environment variables still
work and so does editing the file:

```
export PICOAGENT_BASE_URL=http://localhost:11434/v1   # any OpenAI-compatible server (Ollama, vLLM, OpenAI, gateway)
export PICOAGENT_API_KEY=...                           # optional
export PICOAGENT_MODEL=qwen2.5-coder:32b
```

Zero third-party dependencies means there is nothing for a package manager to resolve, so `python3 -m picoagent`
works against a bare checkout with no virtualenv and no install step. From another directory, point at the
checkout: `PYTHONPATH=/path/to/picoagent python3 -m picoagent`.

Installing is optional and buys exactly one thing — a `picoagent` command on your `$PATH` instead of
`python3 -m picoagent`:

```
python3 -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -e .                                      # optional: registers the `picoagent` command
```

Use the venv if you install. On Debian 12+, Ubuntu 23.04+, Fedora 38+ and Homebrew Python a bare `pip install -e .`
fails with `externally-managed-environment` (PEP 668) — but that error is a reason to skip the install, not a
reason to fight it. [docs/getting-started.md](docs/getting-started.md) covers it, the `pipx` route, and a
missing `python3 -m venv`.

The examples below are written as `picoagent`, which is the installed spelling. Without the install, every one of
them is `python3 -m picoagent` instead; nothing else about them changes.

Config (`~/.picoagent/config.toml`, then `.picoagent/config.toml` in the project):

```toml
model = "qwen2.5-coder:32b"
temperature = 0.0          # optional; omit to use whatever the server defaults to
[providers.openai]                                    # one table per provider; the table IS the provider
base_url = "http://localhost:11434/v1"
api_key = ""
[providers.grok]                                      # another OpenAI-compatible endpoint, no plugin needed
base_url = "https://api.x.ai/v1"
api_key = "xai-..."
[providers.vertex]                                    # the other dialect core ships
dialect = "vertex"
project = "my-project"
location = "us-central1"
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

## What a command the model runs can see

The `shell` tool runs commands as you, but it does **not** hand them your whole environment. It
passes an allowlist — `PATH`, `HOME`, `USER`, `SHELL`, `PWD`, `TMPDIR`, the locale and timezone
variables, the Windows equivalents, and the Python interpreter's own `VIRTUAL_ENV`, `PYTHONPATH`
and `PYTHONHOME` — and drops everything else. `npm test` and `cargo build` still work, because a
default toolchain install needs only `PATH` and `HOME`; what `env` does not return is your API
keys. A relocated toolchain (a custom `GOPATH`, a `CARGO_HOME` moved off its default) is the
case for `shell_env_allow` below.

It matters because tool output is not ephemeral: every result is written to the session log and
sent back to the model as context on the next turn. A key that reaches a command reaches both.

An allowlist rather than a list of secret-looking names, because no such list is complete —
`OPENROUTER_KEY`, `GH_PAT`, `PRIVATE_KEY` and `DATABASE_URL` all get past one. If a command
genuinely needs a variable, name it:

```toml
shell_env_allow = ["ACME_BUILD_FLAG"]   # add one variable
shell_env = "inherit"                   # or pass everything, if that is what you want
```

Both are yours to set: a repository's `.picoagent/config.toml` cannot set either, so cloning
somebody's project cannot widen what the first command sees.

**File permissions.** `~/.picoagent/config.toml` and `~/.picoagent/endpoints/*.toml` can hold an
`api_key`, and a file you create by hand is world-readable under the usual umask. picoagent
narrows those to your account when it reads them, tells you on stderr that it did, and never
widens anything or touches a repository's own config. (POSIX only — on Windows, mode bits are
not access control; use `icacls` on `%USERPROFILE%\.picoagent`.)

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
picoagent plugin list            # trusted | CHANGED (approved before, but not this code) | UNTRUSTED (never approved)
picoagent plugin trust <path>    # shows what moved, then asks
picoagent plugin untrust <path>  # takes the approval back; also accepts the plugin's name
```

`list` also names any approval that no longer has a plugin directory behind it, because that is
the record you are most likely to want gone and the one nothing else shows you. `untrust` takes a
name as well as a path for the same reason: an approval outlives the directory it covers, so a
plugin you deleted still has a record, code that later lands in that directory reads as one you
vetted once rather than one you have never seen, and a plugin you approved as `required` stops
your sessions until you put it back or withdraw it. `untrust` reads the trust store without
loading anything, so it works even then. Withdrawing leaves the plugin's files alone; only the
decision is taken back.

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

`examples/plugins/` holds the two provider references the docs teach with: `grok-provider`
(an OpenAI-compatible endpoint in 12 lines) and `vertex-provider` (a different wire dialect,
implemented whole). Both are now **redundant as configuration** - Grok is a `[providers.grok]`
table and the Gemini dialect ships in `picoagent/core/vertex.py` - and both still load and work
if you have them enabled. They are kept as the two ends of the plugin surface: the smallest
`register(api)` there is, and a foreign wire format implemented end to end. Everything that used
to live beside them moved to its own repository when the examples outgrew the harness - see below.

## Companion plugin repos

* [picoagent-plugins](https://github.com/opscontinuum/picoagent-plugins) - the capability plugins the core deliberately does not ship: `mcp`, `permission-gate`, `credential-guard`, `rules`, `agents`, `compaction`, `complete`. Clone and enable by path.
* [es-doctor](https://github.com/opscontinuum/es-doctor) - Elasticsearch diagnostics and administration: digs through Beats/Elastic Agent logs, correlates them with metrics and APM, and inspects the cluster itself. The worked example of a domain-specific plugin.
* [stig-runner](https://github.com/opscontinuum/stig-runner) - runs a DISA ASD STIG from a CKL file against a repository, human-gated.
* [iscp-author](https://github.com/opscontinuum/iscp-author) - generates a FedRAMP-compliant ISCP, DRP and BIA with a provenance-checked renderer.
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
python3 -m unittest discover -s tests        # 811 tests, no network, ~45s
```

## Providers

`/model` shows the current model, `/model list` asks the provider what it offers (via
`GET /models`), and `/model <name>` switches. Listing is optional on the `Provider` protocol -
a provider that can't enumerate says so rather than pretending.

**Two wire dialects ship in core, and a provider is a config table.** A dialect is code - a
request body, a streaming format, a mapping. An endpoint is a URL. Core ships both dialects that
matter today, so adding a provider is a table with a name on it, not a plugin:

```toml
[providers.grok]                          # no dialect key = OpenAI-compatible
base_url = "https://api.x.ai/v1"
api_key  = "xai-..."

[providers.local]                         # the same dialect, your own server
base_url = "http://localhost:11434/v1"

[providers.milgemini]                     # the other dialect, a host that is not Google's
dialect  = "vertex"
base_url = "https://genai.example"
project  = "my-project"
location = "us-gov-west1"
```

That is three providers, selectable with `--provider grok|local|milgemini`, with nothing
installed. `picoagent setup` writes these tables for you, including inventing a new provider:
the last option in its list is *a new provider*, which asks for a name and a wire format.

| Dialect | `dialect =` | Wire format | Covers |
|---|---|---|---|
| OpenAI-compatible | omitted, or `"openai"` | `/v1/chat/completions` SSE | OpenAI, xAI Grok, Ollama, vLLM, llama.cpp, LM Studio, OpenRouter, Azure, most corporate gateways |
| Gemini / Vertex | `"vertex"` | `:streamGenerateContent?alt=sse`, `contents`/`parts`/`functionCall` | Vertex AI, and any host serving the same API (government deployments, proxies) |

A dialect picoagent does not have is refused by name rather than guessed at: an unknown
`dialect` registers nothing and says which ones exist, because speaking the wrong wire format to
an endpoint you configured on purpose is the worst available outcome.

The provider seam stays open for a **third** dialect - Anthropic's messages API, Bedrock - which
is real code and therefore a plugin: `api.register_provider(obj)` with any object implementing
the `Provider` protocol, and a plugin's registration replaces a config table of the same name.
See [docs/plugin-authoring.md](docs/plugin-authoring.md#writing-a-provider).

Upgrading from the example provider plugins: `grok-provider` becomes the `[providers.grok]`
table above, and `vertex-provider` becomes `dialect = "vertex"` added to your existing
`[providers.vertex]` table. Both plugins still load and still work if you leave them enabled.

## Fakes & tests

`picoagent/testing/fakes.py` has stdlib fake servers for both wire dialects, plus a `grok` variant of
the OpenAI one that enforces xAI's own expectations (they validate paths, auth headers, request shape,
and Gemini schema restrictions, then play a scripted text → tool call → reply):

```
python -m picoagent.testing.fakes --dialect openai --port 8765   # or grok | vertex
PICOAGENT_BASE_URL=http://127.0.0.1:8765/v1 picoagent -p "hello"
python -m unittest discover -s tests -v                          # full loop against each fake
```
