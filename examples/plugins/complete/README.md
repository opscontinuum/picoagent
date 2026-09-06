# complete

Tab completion in the picoagent REPL. Standard library only, no third-party dependencies.

## What it does

Press Tab and one of four things happens, chosen from the shape of the line you are typing:

| You typed | You get |
|---|---|
| `/mo` | `/model` and any other command registered right now |
| `/` | every command, plus every skill as `/skill:<name>` |
| `/skill:es-` | `/skill:es-admin`, `/skill:es-doctor`, ... |
| `/model gpt` | cached model ids for the active provider, and the literal `list` |
| `partial` | `partial-notes.md` and anything else in the runtime cwd starting with `partial` |
| `sub/fi` | `sub/file.txt`; directories come back with a trailing `/` so you can keep descending |

Commands and skills are read from `rt.commands` and `rt.skills` at the moment you press Tab,
not from a list baked in here. Register a command from another plugin and it completes on the
next keystroke.

## How it works

`PlainFrontend._readline` calls the builtin `input()`. On POSIX, `input()` routes through GNU
readline as soon as the `readline` module has been imported into the process. Nothing in
picoagent imports it, so the `import readline` at the top of `complete.py` is the whole
mechanism: it turns Tab from a literal tab character in your prompt into a completion request.
The frontend is untouched.

Two details make the routing possible. The completer looks at `readline.get_line_buffer()`
rather than only the word under the cursor, because `/model gpt` and a bare `gpt` want
different answers. And the word delimiters are set to whitespace only; readline's defaults
split on `/`, `:` and `-`, which would hand the completer `model` out of `/model` and
`es-doctor` out of `/skill:es-doctor`.

## Model ids and the network

The only way to learn what a provider offers is `provider.list_models()`, which is an HTTP
round trip. **Tab never makes that call.** It reads a cache file at
`~/.picoagent/complete-models.json`, keyed by provider, plus whatever model is currently
selected.

The cache is refreshed at most once per session, on the `agent_settled` event, and the fetch
runs as a background task so it cannot hold the next prompt hostage to an HTTP timeout.
`agent_settled` is the chosen moment on purpose: the agent has finished a turn, so the
provider has demonstrably been reached already and the refresh is a follow-up request rather
than a first one. A provider without `list_models` (it is optional on the Provider protocol)
is skipped, and a failed fetch leaves the cache as it was.

Until the cache has been filled, `/model <TAB>` offers `list` and your current model. Run
`/model list` once, or let one turn finish, and the rest appear.

Set `refresh_models = false` if you want the plugin never to make that call at all. Completion
then works from the cache you already have plus the current model.

## Paths

A bare word completes against the filesystem relative to the runtime cwd, and a word that
names nothing on disk completes to nothing, which is what keeps Tab quiet while you are typing
prose. An empty word returns nothing rather than dumping a directory listing into a chat
prompt. Dotfiles appear once you have typed the dot. Set `paths = false` to switch this off
and leave bare words alone entirely.

## Platform notes

**Windows.** There is no `readline` in the Windows standard library. The `ImportError` is
caught, the plugin registers successfully, and Tab does nothing. Nothing else about the
session changes.

**macOS.** The `readline` module on macOS is often backed by libedit rather than GNU readline,
and the two do not share an init-file syntax. GNU takes `tab: complete`; libedit takes
`bind ^I rl_complete`, and silently leaves Tab unbound if you hand it the GNU form. The plugin
detects the backend through `readline.__doc__`, which is the only thing that reliably tells
them apart at runtime, and emits the matching binding.

That branch has not been exercised on real libedit hardware. It is covered by a unit test that
drives the detection through a substituted `readline.__doc__`, so the choice of binding string
is pinned, but the claim "libedit then completes correctly" rests on libedit's documented
behavior rather than on a run. If you are on macOS and Tab does nothing, that is the first
place to look, and `python3 -c "import readline; print(readline.__doc__)"` will say which
backend you have.

Installing the GNU-backed `gnureadline` package is the other way out on macOS, and it needs no
change here: it registers itself as `readline`, so the import picks it up and the GNU branch
takes over.

## Failure behavior

readline discards any exception raised inside a completer and then shows nothing at all, which
is a miserable thing to diagnose from the other side of a prompt. So the completer body is
guarded and fails closed to "no completions", which looks identical to the user and leaves a
debug log line behind.

## Use

```bash
picoagent -e path/to/examples/plugins/complete
```

or add it to `.picoagent/config.toml`:

```toml
[plugins]
enabled = ["./examples/plugins/complete"]

[plugins.complete]
refresh_models = true   # false: never call list_models, complete from the cache only
paths = true            # false: leave bare words alone
```

## Tests

```bash
python3 -m unittest discover -s tests -q
```

`tests/test_complete_plugin.py` covers candidate generation for each source, the per-provider
model cache, the once-per-session refresh, both Tab-binding branches, the no-readline path
(driven through the real loader with `readline` blocked in `sys.modules`, so the `ImportError`
is genuine), and that garbage input and a raising line buffer both degrade to no completions.
