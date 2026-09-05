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
| `test_providers.py` | the real HTTP clients against fake OpenAI / Grok / Vertex servers |
| `test_vertex_mapping.py` | Gemini schema cleaning and message mapping |
| `test_example_plugins.py` | permission-gate and compaction behaviour |
| `test_es_doctor_plugin.py` | the Elasticsearch plugin against `picoagent/testing/fake_es.py` (canned Beats/APM incident) |
| `test_ollama_e2e.py` | live end-to-end against a real Ollama server (opt-in, skipped by default) |

`tests/helpers.py` has the fixtures: `make_runtime`, `ScriptedProvider`, `CaptureFrontend`,
and the `text()` / `call()` shorthands for scripting model turns.

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
