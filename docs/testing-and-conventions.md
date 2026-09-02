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

`tests/helpers.py` has the fixtures: `make_runtime`, `ScriptedProvider`, `CaptureFrontend`,
and the `text()` / `call()` shorthands for scripting model turns.

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
