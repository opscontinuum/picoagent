# Testing and code conventions

## Running the tests

```bash
python -m unittest discover -s tests -v
```

Everything is standard-library `unittest`; no runner to install. The suite takes a few
seconds and needs no network: model calls go to `ScriptedProvider` or to the fake servers
in `picoagent/testing/fakes.py`.

## How the tests are organised

| File | Covers |
|---|---|
| `test_events.py` | ordering, patching, blocking, fault isolation |
| `test_tools.py` | read/write/edit/bash, truncation, the per-file lock |
| `test_skills_session_config.py` | SKILL.md parsing, session tree + compaction, config layering, command parsing |
| `test_loop_and_plugins.py` | the loop end-to-end with a scripted model; plugin loading and trust |
| `test_providers.py` | the real HTTP clients against fake OpenAI / Grok / Vertex servers, the `base_url` scheme check, where a redirect may take a credentialed request, and what a failed model call says to somebody who has not configured anything yet |
| `test_provider_endpoints.py` | `[providers.<name>]` as the one place any provider's endpoint lives: a plugin's dialect reading it, the deprecated `[plugins.<name>]` fallback still working and naming where to move to, a repository refused for every provider, and what a provider says it needs configured |
| `test_setup_wizard.py` | `picoagent setup`: what it asks, that it edits config.toml rather than rewriting it, that nothing is replaced unasked and no stored secret is shown whole, that it refuses without a terminal, and that the file it writes a key into is owner-only |
| `test_config_refusals.py` | config files that cannot be read - unparseable, not UTF-8, nested past the parser's stack - and `DEFAULTS` keys nothing reads |
| `test_unreadable_manifest.py` | a hostile `plugin.toml`, and `plugin list` / `add` / `trust` carrying on around it |
| `test_torn_state_files.py` | `trust.json` and the session log caught mid-write: the store is published by rename and reads as empty when it is damaged, a partial last line in the log is dropped and a hole in the middle is not |
| `test_session_log_permissions.py` | that the session log and the directories holding it are owner-only, read back off the filesystem with `stat` rather than by trusting a call, for a new session, a resumed one, logs written before the rule existed, and the path the CLI actually takes (skipped on Windows, where mode bits are not access control) |
| `test_session_shutdown_record.py` | that a session which ends by its own exit path says so in its last entry, that an interrupted one is named apart from a completed one, and - the half that keeps the record honest - that a session which never reached that path leaves no entry at all |
| `test_log_sanitisation.py` | escape sequences a plugin's exception writes through `log.exception`, and the formatter `main` installs |
| `test_command_injection.py` | that untrusted data reaches a subprocess as argv and never as shell text, and that only the two intended places hand a string to a shell |
| `test_suite_shape.py` | the suite's own invariants: nothing defined below a file's `__main__` block, where it would never run, and no test file calling `tempfile.mkdtemp` instead of the resolved `temp_dir()` |
| `test_vertex_mapping.py` | Gemini schema cleaning and message mapping |
| `test_untrusted_text.py` | who may mark a notice as a command's answer; escape sequences and runaway length in text picoagent did not write |
| `test_headless_run.py` | what a program driving `-p` reads: the exit code of a run whose model call failed, and which project's sessions `-r last` resumes |
| `test_ollama_e2e.py` | live end-to-end against a real Ollama server (opt-in, skipped by default) |

`tests/helpers.py` has the fixtures: `make_runtime`, `ScriptedProvider`, `CaptureFrontend`,
the `text()` / `call()` shorthands for scripting model turns, and `temp_dir()`.

**Use `temp_dir()`, never `tempfile.mkdtemp()` directly.** `mkdtemp` returns the path the way
`TMPDIR` spells it, symlinks and all; picoagent resolves the paths it reports, so an assertion
comparing a reported path against a raw `mkdtemp` string compares two spellings of one
directory. It passes wherever `TMPDIR` has no link in it and fails wherever it does, which on
macOS is everywhere (`/var` is a symlink to `/private/var`). `temp_dir()` resolves once, at the
point the directory is made. `test_suite_shape.py` fails the run if a test file goes back to
`mkdtemp`.

## Coverage statistics

```bash
python3 tools/coverage_report.py                      # the whole suite: ~46s against ~36s plain
python3 tools/coverage_report.py -p 'test_tools.py'   # one file, while you work on it
```

Runs the suite under the standard library's `trace` and prints, per module, how many of that
module's executable lines the run reached. Three tables, because they answer different
questions: `picoagent/` is the application, `examples/plugins/` is the two provider references
the docs teach with, and `picoagent/testing/` is the fake servers the suite runs against -
averaging the fakes into the application's number would flatter it. The plugin family that
used to be measured here reports its own coverage from its own repositories.

**There is no threshold and no gate.** The suite is what fails a build; this reports a number.
A threshold picked on a Tuesday becomes an obstacle on a Thursday, and the statistic's job is to
show *which* modules a release exercised, not to be defended.

**Why the standard library and not `coverage.py`.** `coverage.py` is the better tool and it is a
development dependency, not a runtime one, so taking it would not have broken the promise in
`pyproject.toml`. It would have broken a smaller one that matters more here: this number is
evidence, and evidence that needs a package index to reproduce has a footnote on it on exactly
the machines that ask for evidence - an air-gapped or accredited host has no index. `trace`
ships with CPython, so a clone and a Python reproduce the figure. If you want branch coverage,
or a diff-annotated HTML report, install `coverage.py` in your own environment and use it; just
do not let it become something the tests need.

### The statistic, per release

Recorded here at each release rather than only in a terminal that scrolled away, so the trend
is visible and a reader can see which modules a given release exercised. Add a row when you cut
one; do not edit an old row, because it describes a release that shipped.

| Release | Date | Suite | Application | Application + shipped plugins |
|---|---|---|---|---|
| 0.1.0 (development) | 2026-09-07 | 972 tests, `OK (skipped=5)` | 91.6% | 93.4% |

The current breakdown, after the plugin extraction:

| | Covered / executable lines | |
|---|---|---|
| `picoagent/` - the application | 2779 / 3024 | **91.9%** |
| `examples/plugins/` - the provider references | 176 / 188 | 93.6% |
| `picoagent/testing/` - fake servers | 116 / 136 | 85.3% |
| Application + provider references | 2955 / 3212 | **92.0%** |

Python 3.12.3; the three skips are the opt-in live Ollama file. The least-covered modules,
which is the part of the report worth reading:

| Module | Coverage | Why |
|---|---|---|
| `picoagent/frontends/plain.py` | 51.8% | the interactive REPL: its read-and-dispatch loop needs a terminal, so most tests drive the loop directly instead |
| `picoagent/cli.py` | 84.1% | argument handling and startup wiring, parts of which only run from a real command line |
| `picoagent/frontends/base.py` | 0% | the `Frontend` protocol. Nothing imports it - it is a written contract, and the frontends satisfy it structurally |
| `picoagent/__main__.py` | 0% | only ever runs in a **child process**, which `trace` cannot follow |

Read the numbers with three caveats. Line coverage is not branch coverage: a line that ran is
not a line whose every outcome was tested. A child process is not counted, which is the
`__main__.py` row above. And the tool reports how often it lost the trace
function and re-armed it - CPython removes a trace function that raises, and
`test_config_refusals` deliberately recurses past the parser's stack, so a handful of lines
around that point go uncounted. The script says how many times that happened rather than
quietly rounding up.

## Running the live Ollama tests

`tests/test_ollama_e2e.py` is the one file here that talks to a real model. Everything else drives
`ScriptedProvider` or a fake HTTP server, which proves the loop's logic but never the wire. These
tests run picoagent's own `OpenAICompatProvider` against a running Ollama, with the built-in tools
writing into a temp directory, so they catch the class of bug a fake cannot have: a request body the
server rejects, tool-call fragments reassembled wrongly, an SSE frame shape we never modelled.

They are off unless you switch them on:

```bash
export PICOAGENT_E2E_OLLAMA=1
export PICOAGENT_E2E_OLLAMA_URL=http://localhost:11434
export PICOAGENT_E2E_OLLAMA_MODEL=llama3.2:3b
python -m unittest discover -s tests -v
```

| Variable | Default | Purpose |
|---|---|---|
| `PICOAGENT_E2E_OLLAMA` | unset | the switch; unset, `0` or `false` skips every test in the file |
| `PICOAGENT_E2E_OLLAMA_URL` | `http://localhost:11434` | Ollama's root, not the `/v1` path |
| `PICOAGENT_E2E_OLLAMA_MODEL` | `llama3.2:3b` | must emit *structured* tool calls (see below) |
| `PICOAGENT_E2E_TIMEOUT` | `180` | wall clock for one agent run, in seconds |
| `PICOAGENT_E2E_MAX_TURNS` | `8` | bound on model/tool round trips in one run |

Opt-in rather than autodetect, so `discover` stays offline and fast by default and CI is unaffected
on a machine that happens to be running Ollama.

**The assertions are on side effects and protocol, never on the model's prose.** A live model words
things differently every run, so asserting on wording buys flakiness and proves nothing. What is
deterministic is what the tools did: a file exists on disk, a tool result carries a token the tool
itself read, a `shell` call ran. A green run means the wiring works. It is not a quality benchmark,
and a model too weak to emit tool calls fails these tests correctly.

Three things can be missing, and each skips with its own fix rather than one flat "not available":
the switch is off, the server is unreachable, or the model is not pulled. Skip means the
infrastructure is absent. Failure means it was present and picoagent or the model misbehaved.

### Choosing the model

The default is deliberately small. These tests check wiring, not model quality, so the right model
is the cheapest one that drives the loop reliably. Four were measured by running this suite three
times each:

| Model | Size | Clean rounds | Round time |
|---|---|---|---|
| `llama3.2:3b` | 2.0 GB | 9/9 | 3-7s |
| `qwen3:4b` | 2.5 GB | 3/3 | 42-56s |
| `qwen2.5-coder:7b` | 4.7 GB | 0/3 | n/a |
| `devstral:24b` | 14.3 GB | 3/3 | 20-24s |

`qwen3:4b` is reliable but spends most of its time generating reasoning tokens, which buys nothing
here. `devstral:24b` works and is four times slower than `llama3.2:3b` for the same signal.

`qwen2.5-coder:7b` is the interesting one, and the reason the table says "must emit *structured*
tool calls" rather than "must support tools". Ollama reports a `tools` capability for it, and it
does understand the task: asked to read a file, it produces exactly the right call. It just
produces it as text in the message body:

```
{"name": "read", "arguments": {"path": "secret.txt"}}
```

The OpenAI `tool_calls` field stays empty, so nothing can execute it. A capability flag is a claim
about the model, not a guarantee about the wire format its template produces. If a model fails
every tool test while the others pass, print the assistant text before assuming picoagent is at
fault.

Sampling is pinned with `runtime.temperature = 0.0`, the same `temperature` setting a user sets in
`config.toml` or with `--temperature`. At the server's default temperature the same prompt makes the
model call a tool on one run and answer from memory on the next, measured at four runs in five for
one prompt. Temperature is not part of what these tests exercise, so pinning it removes variance
without changing anything under test.

Temperature 0 narrows the variance but does not remove it, so the prompts matter too. The
observed failure is the model deciding it cannot use tools at all ("I don't have the capability to
access or read files directly") and answering anyway. A prompt that leaves a plausible non-tool
answer available invites that. Asking a question the model cannot answer without the file does not,
which measured 5/5 against 4/5 for the same test on `devstral:24b`. Phrase new tests the same way.

These are live-model tests, so treat a single failure as a model behaviour and a repeated one as a
regression. Broken wiring fails every run; a model declining to call a tool does not.

`AgentLoop._turns` runs until the model stops calling tools, bounded only by `rt.abort`. A scripted
provider always runs out of script, so the rest of the suite needs no cap. A live model can keep
calling tools, which would hang a run rather than fail it, so `PICOAGENT_E2E_MAX_TURNS` and
`PICOAGENT_E2E_TIMEOUT` impose the bound from the test side instead of changing core.

Running from WSL against Ollama on a Windows host, `localhost` is the WSL instance and not the host.
Use the gateway address from `ip route show default`, and start Ollama with `OLLAMA_HOST=0.0.0.0` so
it listens beyond loopback.

## Running the live MCP tests

The MCP plugin and both its test files - the fake-server suite and the opt-in live run
against the protocol's reference server - live in
[opscontinuum/picoagent-plugins](https://github.com/opscontinuum/picoagent-plugins);
its README carries the run instructions that used to sit here.

## TDD workflow we follow

1. Write the test that describes the behaviour (it should fail).
2. Make it pass with the simplest change.
3. Refactor with the suite green.

Two concrete cases from this codebase: the bash-timeout test surfaced a leaked subprocess
transport, which led to `ShellTool`'s `_kill_tree`; the Vertex schema test was written before
`clean_schema` was rewritten to be readable.

## Code conventions

* **One job per module, one job per function.** If a function needs a comment to separate
  its phases, split it (see `AgentLoop._prepare` / `_turns` / `_execute_tools`).
* **Protocols, not base classes.** Tools, providers and frontends are structural; any object
  with the right methods works, which keeps plugins decoupled from core internals.
* **Registries replace on name.** That single rule is the whole override mechanism.
* **Expected failures are values, not exceptions.** Tools return `ToolResult(is_error=True)`;
  providers yield `StreamEvent("error")`. Only bugs raise.
* **Plugin code is untrusted.** Every place that calls into a plugin catches and logs.
* **Docstrings say why, comments say what's not obvious.** Every public module, class and
  method has a docstring written for the next reader, not for a linter.
* **Standard library only** in the package. Plugins may declare `python_deps`.
* Names: `snake_case` functions, `CamelCase` classes, `UPPER_CASE` constants, no
  single-letter names outside comprehensions. Type hints on all public signatures.
* Line length 110; `from __future__ import annotations` at the top of every module.

## Adding a feature

Ask first: does this need to be in core? Almost always the answer is "no, it's a plugin",
and the design doc's feature matrix lists which bucket each known feature belongs to. If it
is core, add the event or registry hook that lets plugins build on it, test it, then use it.
