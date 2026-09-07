# Trust boundaries

Where the lines are, and what crosses them. This describes shipped behaviour, not intent.

## The boundaries

```mermaid
graph TB
    subgraph trusted["Trusted: runs as the user, no containment"]
        core["picoagent core"]
        plug["loaded plugins<br/>approved at load time"]
        cfgfile["config.toml, credentials<br/>owner-restricted files"]
    end

    subgraph semi["Semi-trusted: the user's own project"]
        repo["repository files"]
        agents["AGENTS.md<br/>always-on instructions"]
    end

    subgraph untrusted["Untrusted: treat as data, never as instructions"]
        model["model output<br/>including tool call arguments"]
        toolout["tool results<br/>file contents, command output"]
        remote["plugin sources<br/>fetched from a git host"]
    end

    model -->|"tool calls are<br/>requests, not orders"| core
    core -->|"tool_call event:<br/>block or rewrite"| plug
    toolout -->|"appended to session,<br/>replayed as prompt"| model
    repo --> toolout
    remote -->|"trust check<br/>before import"| plug
    cfgfile -->|"key becomes an HTTP header,<br/>never a Message"| core

    classDef danger stroke-width:2px
    class untrusted danger
```

The fingerprint covers **every file in the plugin directory**, not just the entry module.
It used to cover only `plugin.toml` and the entry, which was a hole rather than a
simplification: the entry imports its siblings, so rewriting `helper.py` changed what
executed while the plugin still reported *trusted*. Every multi-module plugin was affected.
Skills are covered too - they are not executed, but they are injected into the model's prompt,
and text that steers the model is part of what was approved.

The load-time trust decision is the only boundary around plugin code. There is no sandbox: an
approved plugin can do anything the user can. That is why the approval flow shows what changed
rather than silently re-approving, and why declining leaves the plugin unloaded.

## What a repository's config may decide

`<project>/.picoagent/config.toml` is read before you have looked at anything in a repository
you cloned. It is content, not a decision you made, and the trust diagram above puts repository
files in the semi-trusted band - so it may set taste and may not set anything that decides where
a credential goes or what enters the prompt.

Two paths were open before `USER_ONLY` existed, both confirmed by execution:

| A repo set | What happened |
|---|---|
| `providers.openai.base_url` | The endpoint moved to the repo author's host, and the API key from *your* config followed it as an `Authorization` header on the first turn |
| `context_files = ["~/.picoagent/credentials"]` | `find_context_files` joins with `/`, which discards the left side for an absolute path, so the credentials file was read into the system prompt |

Also closed: `skill_dirs` (prompt text), `plugins.rewrite` (redirects a plugin clone before you
approve it), `upgrade` (redirects where picoagent upgrades itself from), and
`confine_to_project` (a repo must not switch off its own confinement).

A repo may still set `model`, `max_tokens`, `thinking`, `parallel_tools` and add to
`plugins.enabled` - each added plugin still faces the trust prompt, and installs under the
repository's own `.picoagent/plugins/` rather than yours. When a repository tries to set a
`USER_ONLY` key, the session says so at startup rather than dropping it silently. The warning is
a `notice` handed to the frontend, so it is on screen in the REPL and in a `-p` run, and a
`--json` run carries the refused names as `ignored_project_keys` beside the sentence.

A repository's config that cannot be read at all is announced the same way, and for a stronger
reason: nothing it asked for happened, and the file can be open in the editor with settings in it
the session has never seen. Reading one never raises, whatever the bytes are. Naming the failures
one at a time was tried and lost twice - `tomllib` decodes the file itself, so a `.picoagent/config.toml`
that is not UTF-8 (what a Windows editor writes when someone re-saves it, and what a repository
would commit on purpose) raised `UnicodeDecodeError`, and the parser is recursive descent, so
`v = [[[[...` raised `RecursionError` - and each escaped as a traceback that let a cloned
repository deny the user their own tool. The catch is now broad, with `MemoryError` re-raised
because that is a fact about the machine and not about the file. The asymmetry is unchanged: the
repository's file is dropped with the sentence, the user's own file stops the session.

The same door was open on `plugin.toml`, which is also a file a clone brings. Session startup was
safe - the loader catches around every manifest read, so one broken manifest costs its own plugin
and nothing else - but `picoagent plugin list` caught nothing and died on a manifest that was not
UTF-8, and `plugin add` and `plugin trust` caught two exception types out of the several a hostile
file can produce. `Manifest.load` now fails as one type, `ManifestError`, and the listing marks
the unreadable directory `UNREADABLE` and carries on with the plugins around it: a repository must
not be able to stop the user seeing, adding or trusting the ones they do have.

### `plugins.enabled` is concatenated, and a spec carries its layer

The concatenation is deliberate: user list first, then the repository's, so a repository can
suggest a plugin. The trust prompt was described here as the control on that. It was not
sufficient, because resolving a spec is not a read. `discover` resolves every enabled
spec at startup, `_clone_or_update` runs `git checkout` in the directory it resolves to, and
every spec - whoever wrote it - resolved into the *user's* plugin directory.

| A repo set | What happened |
|---|---|
| `enabled = ["file://<path>/gate@v0"]`, naming a different ref of a plugin the user already trusts | `git checkout` ran inside the user's own shared checkout and moved it to the repo's ref. The fingerprint stopped matching, so the plugin the user approved loaded as `changed` and was dropped. One line of committed config switched off a security plugin, and the only signal was a line on stderr that a `-p` or `--json` run does not surface |

Confirmed by execution, and now pinned by `tests/test_plugin_enabled_provenance.py`.

The fix is that a spec carries the layer that wrote it as far as the directory it may write:

* `enabled_by_layer` recovers the provenance the merge discards. `load_config` builds the list
  user-first, so the repository's specs are its tail; a config assembled any other way is
  attributed by membership, which errs towards calling a spec the repository's. What the
  repository asked for is recorded on the config (`_project_enabled`) by the read that built
  the list, not re-derived by opening `.picoagent/config.toml` again. That second read was its
  own hole: `load_config` catches everything a hostile file can raise, and the loader's copy
  caught two exception types, so a config that is not UTF-8 or is nested past the parser's stack
  ended the session in a traceback from inside `discover` - the exact outcome the broad catch
  exists to prevent, arrived at from the other side. Two reads can also disagree, because the
  file may change between them.
* A repository's config that would not parse is therefore **not** a provenance failure. It was
  dropped by `load_config`, so nothing of the repository's is in the concatenated list and every
  spec in it is the user's own; the session runs on the user's settings, with the notice that
  says the file was dropped. Refusing instead let one broken committed file stop the tool for
  every user who had a plugin of their own. `enabled_by_layer` still raises
  `PluginProvenanceError` for a config that carries no record of the layer at all - one an
  embedder assembled by hand - because an empty repository list puts every spec in the *user*
  layer, and that is the layer with the off-limits check switched off.
* An entry that is not a string names no plugin, so `enabled_by_layer` drops it and logs which
  files to check. It used to be carried: the repository's list dropped such entries and the
  merged list kept them, so the two no longer lined up, the membership rule handed the stray
  value to the *user* layer, and `resolve_source` matched a bool against the git-spec pattern
  and raised a `TypeError` nothing caught. `enabled = [true]` committed to a repository ended
  every session opened in it. A malformed config is reported; the specs around it still place.
* A repository's spec clones into `<project>/.picoagent/plugins/`. The user's plugin directory
  is off limits to it, compared after resolving symlinks so a `.picoagent/plugins` symlink
  committed in the repository does not get there either, and a checkout directory name that is
  not a single path component (`..`) is refused outright.
* A **path** spec from the repository faces that same off-limits check, for a reason that is not
  about writing. Resolving a spec tags the directory with the layer that asked for it, and
  `discover` keeps the first tag a directory gets, so `enabled = ["/home/you/.picoagent/plugins/gate"]`
  committed to a repository took over the user's own plugin as project-layer code: the stop that
  refuses to start a session when a `required` plugin has changed does not fire at the project
  layer, and the user was told their own edited plugin was "offered by this repository", at a
  path the repository chose. A repository does not get to write the layer tag for a directory
  the user's plugin directory owns.
* The user's own specs are unchanged. `picoagent plugin add` is the user acting, so an `add` on
  an installed plugin is still an upgrade and still moves the checkout.

The distinction is **who owns this checkout**, not *is this spec new*. A repository can still
suggest a plugin the user has never seen: it is cloned, discovered, and reported as `new` until
the user approves it. What it can no longer do is write into a checkout the user owns.

### When a plugin the user approved does not load

A security plugin that quietly does not load looks exactly like one that loaded and found
nothing, so the skip has to be legible. `LoadReport` carries a `Notice` per skipped plugin -
the wording, and whether it is `urgent`, meaning a plugin the user approved is not running:

| The skip | What the user is told | Urgent |
|---|---|---|
| `new` | not trusted yet, here is the command to review it | no |
| `changed`, files edited in place | CHANGED since you approved it and NOT LOADED, whatever it enforces is off | yes |
| `changed`, checkout on a different commit | MOVED to a revision you have not approved (`old -> new`), you did not edit it, check `[plugins].enabled` in both configs | yes |
| `shadowed`, a repository offers a name the user's own loaded copy already provides | the repository's copy did not load and yours is the one running | no |
| `missing`, a plugin approved as required is no longer running from the directory it was approved in | you approved this as REQUIRED, nothing loaded from that directory, and here is the one command that clears the record | yes |

An approval covers the **directory** the code was read from, not the name written inside it.
Keyed by name, an approval was only as durable as a field the replacing code also gets to
write: changing `name` in the replaced `plugin.toml` made the same directory read as `new`
rather than `changed`, so a replacement was announced as ordinary first-run chatter instead of
"code you approved has been replaced", and the enforcement below never fired. `fast_forward`
does that move without anyone touching the machine. Two checkouts sharing a name get an approval
each. A record written before approvals named a directory has none to match on, so the first
directory that asks for it takes it over: an upgrade sends nobody back to the trust prompt, and
the record stops answering for a second directory that later claims the same name.

`changed` splits on the commit in the trust record: nobody arrives at a different commit by
editing a file, so "something moved your checkout" and "you edited this yourself" are separable
and no longer share one message. `shadowed` exists so the repository's rejected copy does not
raise a false alarm about the user's copy that is loading perfectly well, and it is only used
when that user-owned copy really did load.

Both channels carry the notice, because the person and the program reading a session are not the
same reader:

* **stderr**, in every run mode, is the loader's own wording printed verbatim. An urgent line is
  prefixed `picoagent: !!`; a routine one is not, so the line worth stopping for is not the same
  shape as "you have not approved this yet". The CLI prints it, once. The loader keeps its own
  copy at `info`, where `--verbose` finds it, and only raises that to `warning`/`error` when the
  runtime has no frontend at all: a library embedder driving the loop directly has nothing that
  will ever print the text, so it goes where the default handler will show it. Whoever wires a
  frontend owns presentation and reads the wording off `LoadReport`.
* **the event stream** gets one `plugin_skipped` event per skip, carrying `name`, `reason`,
  `root`, `urgent` and the same text. A `--json` consumer reads stdout and never saw stderr, so
  it branches on `urgent` rather than on a sentence. The REPL and a plain `-p` run render only
  the events they know about, so they ignore it and nobody is told twice.

### Taking an approval back

An approval that can be given and not withdrawn is not a decision the user holds. `picoagent
plugin untrust <directory-or-name>` removes one record from `trust.json` and says which record it
removed and from which file. It refuses, without exit code 0, when nothing matches, and refuses
when a name covers two checkouts rather than guessing which the user meant - the copy it could
guess wrong about is an approval they still want.

It matches the record, not a plugin on disk, and that is the point of it. `plugin trust` resolves
its argument by reading `plugin.toml`, which cannot reach the case that matters: a record outlives
its directory, going on standing for that path so that whatever arrives there next reads as code
the user once vetted rather than as code they have never seen, and after a deletion there is no
manifest left to resolve. A directory is compared as the
store writes it, resolved, so a symlinked spelling still matches; the name a record was filed under
is accepted as well, because after a deletion the name is what the user still has. For the same
reason `plugin list` names every approval the directory walk cannot show: one whose directory is
gone, marked `MISSING`, and one pointing outside both plugin directories, marked `outside`. A
record nobody can see is a record nobody can withdraw.

Withdrawing is not uninstalling. The plugin's files are untouched; it reverts to `UNTRUSTED` and
does not load until it is approved again.

### `[plugins.<name>]` is layered, not merged

`USER_ONLY` names whole settings, and for a while it named none of the `[plugins.<name>]`
tables. Those tables deep-merged from the repository's config and reached plugins through
`api.plugin_config()` as one dictionary with no record of who wrote which key. Four plugins
took a decision from it that a repository must not make - each confirmed by execution:

| A repo set | What happened |
|---|---|
| `[plugins.grok-provider] base_url` | Only the URL. The `api_key` beside it was still the user's, and went to the repo author's host on the first turn. Same for `[plugins.es-doctor] url` and the vertex provider's OAuth token |
| `[plugins.mcp.servers.x] command` | Spawned at session start, before the first prompt and outside the trust store that gates every other way a repository gets code to run. The child inherited the user's whole environment too; it now starts from a minimal allowlist, and a server entry's own `pass_env` names by hand anything else it may see |
| `[plugins.permission-gate] mode = "yolo"` | The confirmation prompt the user installed the plugin for, switched off by the repository it was meant to guard against |
| `[plugins.credential-guard] extra_allow_env` | Named variables the shell tool may expose. `DATABASE_URL` is not secret-shaped, so nothing else stopped it |

The fix is a seam in `picoagent.core.config`, not four patches. A repository's plugin tables are
lifted out of the merge into `_project_plugin_config` and handed to plugins as a `PluginConfig`:

* Reading it like a dict returns the **user layer** - defaults, `~/.picoagent/config.toml`, CLI
  flags. A plugin author who does nothing gets that.
* `.source(key)` says `user`, `project`, `both` or `None`. Nothing merges without a name on it.
* `.from_project(key)` and `.with_project(*keys)` are the only ways a repository's value is
  read, and each call is a decision the plugin answers for.
* `api.warn_about_project_config(*accepted)` names what the plugin refused, at session start,
  the same way `_ignored_project_keys` names a refused `USER_ONLY` key.

The line the shipped plugins draw: a repository may **tighten** and may set what is only taste.
It may not choose a destination, a command, or a permission.

| Plugin | Taken from a repository | Refused |
|---|---|---|
| es-doctor | `logs_index`, `metrics_index`, `traces_index` | `url`, `api_key`, `username`, `password`, `verify_tls`, `ca_cert`, `allow_destructive` |
| mcp | `timeout`, `startup_timeout` | `servers` |
| permission-gate | `protected` (added to the user's list, never replacing it) | `mode`, `dangerous` |
| credential-guard | `extra_deny_patterns` (added; it only refuses more) | `extra_allow_env` |
| rules | `dirs` (a place to look, never a decision - see below) | `max_rules_per_turn` |
| iscp-author | nothing | `answers`, `output` |
| stig-runner | nothing | `interactive` |
| grok-provider, vertex-provider | nothing | everything |

`rules` is the plugin that takes a directory from a repository, and it is worth saying why that
is not a hole. It had already spotted this surface and defended against it by trusting files
rather than directories: any directory that is not `~/.picoagent/rules/` is treated as
repository-supplied whatever named it, and every file found in one is fingerprinted, shown to
the user, and approved before a byte of it reaches the prompt - the same gate `TrustStore` puts
around plugin code, and it refuses outright in a run with no frontend to ask (`picoagent -p`).
So it asks for the repository's `dirs` by name through `from_project` and keeps reading it. A
repository saying "our rules live in `docs/rules`" buys nothing on its own, because the decision
is still taken one file at a time by the person at the keyboard. What a repository may not
choose is how much of the user's context window a turn spends, so `max_rules_per_turn` stays in
the user layer, and a repository that sets it is told at session start that it did nothing. The
hardcoded `.picoagent/rules/` path is unchanged, and `dirs` in the user's own config still works.

What this does **not** cover: a plugin that reads `api.config["plugins"]` itself is reading the
merged config, which no longer carries a repository's plugin tables, so it now sees the user
layer - but a plugin reading `api.config["_project_plugin_config"]` directly gets the repository's
values with no ceremony at all. There is no sandbox around plugin code, so this is a seam that
makes the safe thing the default and the unsafe thing explicit, not an enforced boundary. The
boundary around plugin code remains the load-time trust decision.

### What `allow_destructive` refuses

`allow_destructive` is in the table above because a repository must not be able to switch it
on. That is only worth anything if the gate holds when it is off, and for a while it did not.
It was a denylist: method `DELETE`, plus seven substrings of a path. Elasticsearch has hundreds
of endpoints that change something, so everything nobody had enumerated went through with
`allow_destructive = false` - `PUT /_cluster/settings` to stop allocation across the cluster,
`POST /<index>/_bulk` carrying delete actions, `PUT /_scripts/<id>` to store a script,
`POST /_aliases` to change what every read resolves to, `_restore` over live indices, and
`POST /<index>/_doc` to write forged evidence into the logs somebody is reading. This plugin is
fed logs and traces, which is data an attacker writes, so the realistic route to any of those is
an instruction injected into a document and followed by the model.

The gate is now a list of what is permitted, so an endpoint nobody has thought about is refused
rather than allowed:

* `GET` and `HEAD` reach the cluster. Nothing else does.
* Except four `POST` paths that only read, because Elasticsearch takes the query in a request
  body and a body on a GET does not survive every proxy in front of a cluster:
  `<index>/_search`, `<index>/_count`, `_cluster/allocation/explain`, and
  `_index_template/_simulate_index/<index>`. Whole-path matches, not substrings, with `%2f` and
  `..` refused before the list is consulted.
* The gate lives in `ESClient.request`, which every module in the plugin goes through, so it
  covers the tools in `es_admin.py` and the next tool somebody writes as well as `es_request`.
  The `tool_call` guard still blocks `es_request` first, so the model reads a refusal that names
  the setting and nothing is dispatched, but it is not what makes the gate hold.
* `ESClient.request_after_confirmation` is the only bypass, and it exists for the two writes a
  person is shown in full and agrees to before they happen: `es_slowlog enable|disable` (three
  named threshold keys) and `es_snapshots verify` (a test blob per node). Grep for the name to
  see every call that takes it, and what you get is only the confirmed writes: with no
  interactive session `es_snapshots verify` is skipped outright, and `es_slowlog` is too unless
  `allow_destructive` is set, in which case it writes through the ordinary gated `request` -
  authorised by the setting rather than confirmed by a person, which is what happened.

What this does not cover: a call that is genuinely a read but is not on the list is refused too,
so a person who needs one either uses `es_search` or sets `allow_destructive` - a false refusal
rather than a false permit. And with `allow_destructive = true` the gate is not a gate; it is
the user saying this cluster is one the model may change.

## Where an endpoint's key lives

Each external service gets its own file under `~/.picoagent/endpoints/`, named for the service:

```toml
# ~/.picoagent/endpoints/github.toml
base_url = "https://github.example.mil"
api_key  = "ghp_..."
```

One file per endpoint, one key per endpoint. A git host, an artifact repository and the model
gateway never share a credential, and none of them sits in a file a project could try to
override. These are read from the user directory only - a project's `endpoints/` is ignored.

## Where a credential may travel

The rule the design holds to: **an API key must never reach the prompt or the session log.**
Both are the same problem, because tool results are persisted to the session and replayed to
the model on the next turn.

```mermaid
flowchart LR
    store["credentials file<br/>or config.toml<br/>owner-restricted"] --> prov["provider instance<br/>holds the key"]
    prov --> hdr["Authorization header<br/>on the model request"]

    prov -.->|blocked: scrubbed<br/>from error text| errs["terminal and --json"]
    store -.->|blocked: tool_call guard<br/>on any path argument| tools["read, grep_search,<br/>structured_data, shell"]
    env["environment variables"] -.->|blocked: allowlist<br/>strips secret-shaped names| sub["shell subprocess"]

    tools --> res["tool result"]
    sub --> res
    res --> sess["session log"]
    sess --> prompt["next prompt"]

    classDef ok stroke-width:2px
    class hdr ok
```

Solid arrows are the intended path: the key is read once at load, lives on the provider
instance, and becomes an `Authorization` header. It never enters a `Message`, so it cannot
reach the session or a later prompt by that route.

Dotted arrows are paths that were closed deliberately, each because it was reachable:

| Path | Control |
|---|---|
| A tool reads the credentials file or `config.toml` | `tool_call` guard blocks any tool whose path argument names a protected file, by inode identity, and blocks recursive tools pointed at a containing directory |
| `env` or `echo $VAR` in a shell command | The shell tool passes an allowlist of variables, not a denylist of secret-shaped names |
| A gateway echoes the key back in a 401 body | Provider error text is scrubbed before it reaches the terminal or the `--json` stream |
| A gateway answers the model request with a 302 to another host | The redirect is refused. `urllib` re-sends every header that is not about the body, so the key arrived at the second host in full - confirmed by execution against a local server. Same origin means the same scheme, host and port; a redirect that stays inside one is followed |
| A key typed into a slash command | Slash commands short-circuit before `session.append_message`, so they are never recorded |

### The endpoint check is on the scheme, and not on the host

`provider.HTTP_SCHEMES` refuses a `base_url` that is not `http` or `https`, because `urlopen`
resolves `file:`, `ftp:` and `data:` without complaint and a `file:///etc` endpoint turns the model
client into a file reader. It does **not** refuse an address: loopback, link-local and private
ranges all pass, so `http://127.0.0.1:11434` reaches a local Ollama and `http://10.0.0.5:8000`
reaches a vLLM on the network. That is a decision rather than an omission. A local model server is
the configuration this client is most used for, so an SSRF filter here would refuse the headline
case in order to defend against a URL the user typed into their own config. What keeps a
*repository* from choosing the host is `providers` being in `USER_ONLY`, above; a plugin handed a
URL out of its own `[plugins.<name>]` table is answered by `PluginConfig`, not by this check.

What is checked at every hop is the scheme and the origin of the URL actually fetched, which is why
the redirect row in the table above exists: the configured URL being `https://gateway.example` says
nothing about where a 302 from it leads.

## Confining file access

`read`, `write` and `edit` take a path from the model. By default any path resolves -
absolute ones and `..` traversal included - so the agent can reach anything the user can.

That is deliberate rather than an oversight. A coding agent legitimately edits sibling
repositories, files under `~/.config`, and things outside whatever directory it happened to
start in; confining it by default would break ordinary work. The boundary is the toolset, not
the path string.

Deployments that need the harder rule can turn it on:

```toml
confine_to_project = true   # read/write/edit refuse anything outside the project directory
```

With it on, a path resolving outside `cwd` comes back as a refused tool result rather than an
exception, so the model can see why and adjust. It is off by default because switching it on
is a real behavioural change, and on where an environment requires it.

This does not confine the `shell` tool, which runs commands as the user and can reach any file
the user can. Restricting that means not granting `shell` (`api.set_active_tools`) or gating it
with `permission-gate`.

## Known limits

Stated plainly, because a control that is oversold is worse than one that is absent.

**The shell file guard is a speed bump, not a boundary.** It matches `cat`-style commands
naming a protected path. `sed`, `xxd`, `python -c`, a relative path, or copying the file first
all defeat it. A shell running as you can read anything you can read; the real controls are the
environment allowlist and the tool-layer refusal.

**Every control is void if the plugin is not loaded.** credential-guard supplies the guards.
Untrusted or disabled, the built-in shell tool passes the entire environment through and no
`tool_call` guard exists. This is why a skip is reported the way it is above, on stderr and as a
`plugin_skipped` event.

A plugin can say that reporting is not enough. `required = true` in its `plugin.toml` is read
before any of its code runs, so unlike `api.declare_required` - which is a statement made from
inside `register()`, and so cannot cover a plugin that never reaches `register()` - it covers
the two ways a plugin goes missing before that: the trust check refused it, or its entry module
would not import. Both set the same field: the loader seeds the runtime declaration from the
manifest, so a manifest requirement already makes a failing `register()` fatal.

What it stops, and what it does not:

| Situation | Outcome |
|---|---|
| A required plugin the user owns is `changed` - approved once, now different code | The session does not start. `RequiredPluginUntrusted` names the plugin, what changed, and the `plugin trust` command |
| The same replacement, with a different `name` in its `plugin.toml` | The session does not start. The approval covers the directory, so renaming is a change like any other |
| A required plugin's entry module raises on import - a dependency gone after a venv rebuild, an entry attribute that no longer exists | The session does not start. `RequiredPluginFailed` names what the import reported. It used to be an ordinary skip: not urgent, "run with --verbose", the control absent, `required` in hand and never read |
| A required plugin's `register()` raises | The session does not start. `RequiredPluginFailed`, as before |
| A required plugin in the user's own `~/.picoagent/plugins` is no longer there, or no longer loads at all | The session does not start. `RequiredPluginMissing` names the directory it was approved in and the single command that clears the record. Deleting approved code must not be the quiet way around a check that replacing it trips |
| A replacement deletes the `required` line along with the code | Announced as urgent and not loaded, session continues. The declaration is read from the plugin in front of you; see Known limits below |
| A required plugin is `new` - never approved | Announced as urgent, session continues. Install-then-run is the ordinary first-run path, and making it fatal means the plugin can never be trusted from a session that will no longer open |
| A required plugin the *repository* owns is refused, or its copy disappears | Announced, session continues. `required` is a line a cloned repository wrote, and honouring it there would hand any repository a switch that stops the user's session on demand |

A plugin that is gone cannot declare anything, so the requirement is also recorded in
`trust.json` when the user approves, with the reason they were shown. That record is read back for
one question and no other: **is a plugin the user approved as required absent altogether?** A
plugin that is present is asked directly, so the store and the manifest can never disagree about
code that exists. Only records naming a directory inside the user's own `~/.picoagent/plugins`
count - a bare name cannot tell "the plugin is gone" from "this project does not use it", and a
repository's copy is not the user's decision to enforce.

An absence is a stop rather than a notice, because that is what the flag means: `required` is a
plugin saying a session without it should not run, and the user approved that statement. It is
defensible only because it is recoverable without a session. `picoagent plugin untrust <name>`
builds a trust store from the config and stops there - it imports no plugin and builds no
runtime - so the record doing the stopping is always one command away from gone, and the refusal
names that command instead of telling anyone to hand-edit a security file. An earlier version of
this check told them exactly that, which is what made it a lockout rather than a refusal.

**That property is load-bearing.** If `plugin untrust` or `plugin list` ever grew a plugin load,
this stop would become a wedge again. Both are deliberately runtime-free, and
`tests/test_plugin_untrust.py` drives them through `cli.plugin_command` with no runtime in sight.

Reading the store is part of that property, because both commands open it before they do
anything else. `trust.json` used to be overwritten in place, so a crash or a full disk between
the first byte and the last left a file that parsed nowhere, and every session *and* both
recovery commands then died on a `JSONDecodeError` - the recovery path broken by the same
accident, leaving hand-editing the security file this design exists to avoid. Two halves now:
the store is written to a temp file in the same directory and renamed over the old one, so a
reader sees all of a write or none of it; and a store that cannot be read is treated as
**empty**, said out loud at `error`, never as "everything is still approved". Empty is the
fail-closed direction - every plugin reads as `new` and nothing loads until it is approved
again - and it is also what keeps the session startable while the approvals are given back.
`tests/test_torn_state_files.py` pins both halves.

A user who wants the plugin but not the requirement has two ways out that do not involve this
check: drop `required = true` from the plugin's own `plugin.toml`, or withdraw the approval that
carries it. Every row above that stops a session is recoverable from the CLI. `picoagent plugin
trust` shows what changed and then asks; `picoagent plugin untrust` takes the decision back and
accepts the plugin's name as well as its directory, because a record can outlive what it names.

A caller that needs a control the loader treats as optional still has to read `LoadReport` and
decide for itself.

**`required` is a statement by the plugin in front of you, except for absence.** Three things it
does not catch, stated because a control that is oversold is worse than one that is absent:

* A replacement that deletes the `required` line along with the code is not enforced as required.
  It is still refused as code the user never approved, announced as urgent, and not loaded, so
  what is missing is the stop rather than the refusal - but the stop is missing.
* The reason a user reads at a refusal comes from the manifest whenever the plugin is present, so
  a replacement chooses that sentence. It only reaches a user whose session is being stopped.
* A plugin renamed *and* replaced in one move, where the user's only approval is a record written
  before approvals named a directory, reads as `new` rather than `changed` and its absence is not
  enforced: the old record is filed under the old name, and nothing looks that name up again. It
  ends at the first re-approval, which writes a directory.

**A plugin's import namespace does not cover `importlib.import_module`.** Everything a plugin
imports with the `import` statement is loaded inside that plugin's own package: files beside the
entry module, modules in subdirectories of the plugin, absolute or relative spellings, and the
imports those modules make in turn. `importlib.import_module` is not an `import` statement. It
calls the interpreter's import machinery directly and never consults the hook this uses, so it
resolves against `sys.path` and can bind an installed distribution that no fingerprint covers -
the crossing the namespace exists to prevent. A hook installed on each module's builtins cannot
answer it, and the alternatives (`sys.meta_path`) are process-wide and would have to guess which
plugin is asking. Plugin authors are told to use the `import` statement instead
([plugin-authoring.md](../plugin-authoring.md#more-than-one-file)), and
`tests/test_plugin_isolation.py` pins the behaviour so the limit stays visible.

**Owner-restriction is not encryption.** The credentials file is `0600` on POSIX and
ACL-restricted via `icacls` on Windows - the same trust model as `~/.netrc`. Anything running
as that user can read it. Where `icacls` cannot be confirmed, the CLI says so rather than
implying protection it did not achieve.

**Prompt injection is not solved.** Tool output is untrusted content that reaches the model as
context. The zeroth-law skill instructs treating it as data, but that is guidance to a model,
not an enforced control.

## What a frontend may be told, and by whom

Two claims travel with a `notice`, and neither is worth anything unless the frontend can tell
who made it.

**Only the command dispatcher can put a notice on stdout.** In a `-p` run stdout is the answer
and stderr is everything else, and the split is decided by `source` on the notice payload. The
key used to be the plain string `"command"`, which any plugin could write into a payload of its
own - `api.ui.emit`, `rt.frontend.emit` from any event handler, either one - and so put its own
sentence inside the bytes a script captured and parsed as the model's reply. The dispatcher now
stamps `picoagent.core.commands.COMMAND_SOURCE`, a single `str` instance, and `PrintFrontend`
drops any `source` that is not that object before it chooses a stream or writes a `--json`
record. It still reads as `"command"`, so a frontend plugin comparing with `==` is unaffected;
what changed is that comparing with `is` now means something. This does not contain a plugin
that imports the name deliberately - nothing here does, and the load-time trust decision remains
the only boundary around plugin code - it ends the forgery that costs one extra dictionary key.

**Notice and error text is stripped before a terminal sees it.** That text comes from plugins,
tool results, a repository's config file and remote MCP servers, and a terminal obeys some of
what it can contain: `picoagent.core.text.strip_terminal_controls` removes ANSI CSI and OSC
sequences and the rest of C0/C1, keeping tabs and newlines so a multi-line command answer still
arrives whole. `--json` is deliberately left verbatim: `json.dumps` escapes everything a
terminal would obey, so those bytes are inert, and stripping would edit a record a program
parses rather than a display. A consumer that echoes a field to its own terminal owns that step.

Alongside it, an exception whose `__str__` a plugin wrote is rendered through
`describe_exception`, which strips the same sequences from both the message and the class name
(`type()` accepts any string as a name) and bounds the result at 2000 characters. Reading that
name is itself a call into the raiser's code, because a metaclass can make `__name__` a
property, so the name and the message are each read inside their own guard, that guard catches
`BaseException` (the raiser chooses what to raise, and a `KeyboardInterrupt` from a property is
not the user asking to stop), and the two halves are joined with `"".join` rather than an
f-string so no `__format__` a raiser wrote runs after the guards have closed. This matters
because every caller of `describe_exception` is a catch whose job is that a failure does not end
the session: `_invoke` for every tool call, `_run_command` for every slash command. An exception
escaping the report is a session that dies with a traceback in `-p` and in the REPL alike.
Logging is covered by a formatter rather than by those call sites: `log.exception` renders the
traceback itself, and a traceback's last line is the class name and `str(exc)` whatever the caller
does to its own arguments. `cli.SafeLogFormatter` runs the same strip over the rendered exception
and over the message, and `main` installs it on the handler it configures, so the three places
that log a plugin's exception on purpose - a failing event handler, a failing slash command, a
failing tool - no longer put escape sequences on stderr.

**No string a frontend writes can crash the write.** A `str` in Python is not always text. A
lone surrogate is a `str` that no codec can encode, UTF-8 included, so writing it raises
`UnicodeEncodeError`. Three ways one arrives, only the first of them hostile: a plugin
constructs it; `json.loads` builds it from a `"\ud800"` in a remote MCP server's reply; or a path
is decoded off the filesystem, because `os.fsdecode` maps every byte a directory name holds that
UTF-8 cannot explain to exactly such a surrogate, so `str(Path("/tmp/plug\xffin"))` is one and a
tool that lists a directory can put it in front of a person. It survives every layer above: `strip_terminal_controls`
keeps everything above U+009F by design, and the report builders interpolate with `{exc}` rather
than `{exc!r}`. The same crash arrives without an attacker on an ASCII-only console or a Windows
code page, where most of Unicode is unencodable. `picoagent.core.text.safe_for_stream` closes it
by round-tripping through the stream's *own* codec with `backslashreplace`, and it runs at the
write rather than in the stripper: the stripper does not know which stream the text is bound
for, and half of what a frontend writes - the model's deltas, a tool result's preview, a
question's prompt - never passes through it. `repr` would have answered this and the
terminal-control question together, at the price of quoting the string and escaping every
newline in it, and the notice channel exists to deliver a multi-line command answer as lines.

**A plugin can still write into the `-p` answer.** `PrintFrontend` writes `assistant_delta`
straight to stdout unstripped, and any plugin may emit that event, so it can add text to the
bytes a script captures and can leave ANSI in them. This is a documented limit rather than a
hole to plug, for the same reason the model's own deltas are unstripped: an escape sequence can
be split across two chunks, so a per-chunk filter is defeated by an attacker sending two emits,
and a filter that only stops the careless is worse than a stated boundary because it invites
reliance. A plugin is in-process code with the user's privileges - `sys.stdout.write` is open to
it directly - so the load-time trust decision is the boundary here, as it is everywhere else in
plugin land. The forgery that *is* blocked is the one a filter can actually block: a `source`
key claiming a notice is a command's answer, where the claim is a field a program reads. What is
not part of the limit is the crash: a delta no codec can encode used to end the session, and it
goes through `safe_for_stream` like every other write.

Two writes are still outside that guarantee, both in `cli.py`, both before a frontend exists:
`_report_skipped` writes the loader's notice text to stderr, and `build_runtime_or_refuse`
writes a plugin-load exception's message the same way. Neither is stripped and neither is
encoding-guarded. The notice text interpolates the skipped plugin's directory, so the
filesystem route above reaches it: a plugin directory whose name is not valid UTF-8 turns
"plugin X was not loaded" into a traceback. It ends the process at startup rather than mid
session, which is the mildest place for it, but it is the same defect.
