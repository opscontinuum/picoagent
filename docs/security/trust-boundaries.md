# Trust boundaries

Where the lines are, and what crosses them. This describes shipped behaviour, not intent.

For who would attack these boundaries, what they would try, and what is still open, see
[threat-model.md](threat-model.md). This document is where the boundaries are; that one ranks
what pushes against them.

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
approve it), `upgrade` (redirects where picoagent upgrades itself from),
`confine_to_project` (a repo must not switch off its own confinement), and `shell_env` /
`shell_env_allow` (what a model-run command sees of your environment - `inherit` would hand
every exported credential to the first command, and a named variable goes into a tool result
and from there into the session log).

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

### Requiring a hash or a signature before a plugin may be installed

The fingerprint above answers *did this change since you approved it*. It cannot answer *is this
what the publisher published*, because the value it compares against was computed from the bytes
that arrived: a git host serving different code serves a different fingerprint with it, and the
approval prompt reads exactly as it does on a good day.

`~/.picoagent/plugin-pins.toml` is where a site writes the value the download cannot choose.

```toml
unpinned = "refuse"                 # or "allow". "refuse" is the default.
python_deps = "require-hashes"      # or "allow-unpinned". "require-hashes" is the default.

["https://github.com/opscontinuum/permission-gate"]
sha256 = "33033e47..."              # what the publisher published for this release
signed_by = ["A1B2C3D4E5F60718"]    # keys whose signature over the commit or tag is accepted
```

**Absent, it decides nothing** and installs behave as they always have. This is a capability a
site turns on, not an obligation on everyone who has a plugin. **Present, its defaults are the
strict ones**: a plugin with no entry is refused, and `python_deps` must be pinned with `==` and
carry a `--hash`, which pip is then given behind `--require-hashes`. A file that states a policy
whose default is "and anything unmentioned is fine" is the shape that has to be asked for.

Where it is checked, and why in more than one place:

| Path | Checked at |
|---|---|
| `picoagent plugin add <spec>` | `resolve_source`, after the clone and before the manifest is shown |
| a spec in `[plugins].enabled` | `resolve_source`, at every session start; a refusal is logged and the plugin is not discovered |
| `picoagent plugin trust <directory>` | `TrustStore.trust`, which never goes through `resolve_source` |

The last row is the reason the gate is not only on the fetch. Approval is what makes a plugin
loadable, so a policy binding only the clone would be one a user walks past by cloning the
repository by hand and then approving the result.

Three properties the file is worth having for:

* **Only your copy is read.** A repository's `.picoagent/config.toml` is merged into the running
  config before you have looked at anything in the clone. A policy that lived there would be a
  policy the code it admits gets to write, so this file is read from the user directory and
  nowhere else - there is no project-level spelling of it.
* **A missing verifier refuses.** `signed_by` shells out to `git verify-commit` and
  `git verify-tag`. No `git`, no GnuPG, a timeout, a bad signature, or a good signature from a
  key the file does not name all produce a refusal. A signature check that passes when nothing
  on the machine can check signatures reports a guarantee it never obtained.
* **An unparseable file refuses everything.** The same direction the trust store takes with its
  own damaged file, for the same reason: a control that fails open is one an attacker only has
  to break rather than defeat.

`sha256` is `pin_digest`: a `sha256sum`-style listing of every fingerprinted file, one
`<digest>  <path>` line each in `LC_ALL=C` path order, and the sha256 of that listing. It is
deliberately reproducible without picoagent, because a hash an administrator cannot compute is
not a hash they can check:

```sh
python3 -m picoagent.plugins.loader <plugin directory>
```

```sh
cd <plugin> && find . -type f \
    -not -path '*/.git/*' -not -path '*/__pycache__/*' -not -path '*/.hg/*' \
    -not -path '*/.svn/*' -not -path '*/.mypy_cache/*' -not -path '*/.pytest_cache/*' \
    -not -name '*.pyc' -not -name '*.pyo' \
  | sed 's|^\./||' | LC_ALL=C sort | tr '\n' '\0' | xargs -0 sha256sum | sha256sum
```

Note that this is **not** the number the trust store records. The trust fingerprint concatenates
file contents and hashes nothing else; `pin_digest` hashes names and boundaries too, so it sees
a rename, an added empty file, and a line moved from the end of one file to the start of the
next, none of which the trust fingerprint can distinguish. The trust fingerprint is not being
redefined to match, because doing so would invalidate every approval on every machine at once.

What the policy does not do, stated so it is not assumed:

* **It does not re-check what was already approved.** Writing the file today binds the next
  install; a plugin approved before it existed keeps loading. A site adopting pins has to
  `plugin untrust` and re-add what it already has.
* **It is not a certificate check.** An OpenPGP key is not a certificate issued by an approved
  CA, and the standard library has no public-key verification to build one with. What
  `signed_by` gives is git-native signing against keys the site put in its own keyring.
* **It does not cover `-e <path>`.** That flag loads a directory for one run, by explicit local
  flag, and installs nothing: no clone, no approval record, and nothing loads from it next
  session. It already bypasses the trust store and is documented as doing so.
* **A hostile repository can still deny you the plugin.** It cannot get new code approved
  against a pin, but serving something that fails the pin means the plugin does not install or
  load - and for a plugin declared `required`, that stops the session rather than passing
  quietly. Refusing is the intended direction; it is worth knowing it is reachable from
  outside.

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

### Who else on the machine can read those files

Both these files and `~/.picoagent/config.toml` - the documented place for
`[providers.<name>] api_key` - are written by hand, by the user, following the README. So they
were created under whatever the umask gives: `-rw-r--r--` on a standard install, confirmed by
execution, meaning every account on the host could read a key out of them while the credentials
file beside them was opened `0600` from the start. DISA ASD V6R4 records that as V-222587.

`load_config` narrows them as it reads them - `config.toml`, every `endpoints/*.toml`, and the
`endpoints` directory around them, whose entries are the names of every service this user holds
a credential for. The rule is the session log's, for the same reasons:

* **One way.** Group and world bits are cleared and the owner's own are left exactly as they
  were, so this removes the access the finding is about and nothing else, and a file somebody
  deliberately made read-only stays read-only.
* **Before the read, not after.** The window in which the file is still world-readable is the
  finding.
* **Said out loud.** Each narrowed path is printed on stderr. A mode a user set by hand can be
  set again in one command; a key another account has already read cannot be taken back, so the
  narrowing happens - but silently rewriting somebody's file modes is not on.
* **Not fatal.** A path this user cannot `chmod` - someone else's file, a read-only mount - is
  logged and skipped. Refusing to start over a mode picoagent could not set is the worse answer.
* **The user's directory only.** A repository's `<project>/.picoagent/config.toml` is untouched.
  It is the repository's file, `providers` is `USER_ONLY` so it must not hold a credential
  anyway, and rewriting modes inside somebody's checkout is a surprise.

POSIX only, and stated rather than implied: on Windows `os.chmod` sets a read-only flag and
nothing else, because NTFS uses ACLs, so `harden_user_files` returns early there instead of
reporting a protection it did not obtain. `icacls` is the hand fix, and T23 in the threat model
carries the residual.

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
    env["environment variables"] -.->|blocked: core's allowlist<br/>on every shell command| sub["shell subprocess"]

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
| `env` or `echo $VAR` in a shell command | The built-in shell tool passes an allowlist of variables, not a denylist of secret-shaped names - by default, with nothing installed. See below |
| A gateway echoes the key back in a 401 body | Provider error text is scrubbed before it reaches the terminal or the `--json` stream |
| A gateway answers the model request with a 302 to another host | The redirect is refused. `urllib` re-sends every header that is not about the body, so the key arrived at the second host in full - confirmed by execution against a local server. Same origin means the same scheme, host and port; a redirect that stays inside one is followed |
| A key typed into a slash command | Slash commands short-circuit before `session.append_message`, so they are never recorded |

### What a model-run command sees of your environment

The shell tool used to hand every command `{**os.environ, "PICOAGENT": "1"}`. `env` is one of
the first things a model runs when it wants to know where it is, and the answer came back as a
tool result - which is appended to the session log and replayed as prompt context on the next
turn. One command put a key in a file on disk and in the next request to the model. Recorded as
DISA V-222444, and as T20 in the threat model.

The allowlist that closes it is `tools.SHELL_ENV_ALLOWLIST`, applied by `tools.shell_env` in the
built-in tool, with nothing installed and nothing configured. It was `credential-guard`'s, and
moving it into core is the whole of the fix: a control that lives only in an opt-in plugin is
absent for everyone who has not opted in, which was the shipped default.

An allowlist and not a denylist of secret-shaped names, because the site that invented the
variable name is the site whose key leaks: `OPENROUTER_KEY`, `GH_PAT`, `PRIVATE_KEY`,
`AWS_ACCESS_KEY_ID` and `DATABASE_URL` all sail past one. What is on it is what an ordinary
build needs - `PATH`, `HOME`, `USER`, `SHELL`, `PWD`, `TMPDIR`, `DISPLAY`, the locale and
timezone variables, the Windows equivalents, and the toolchain locations (`VIRTUAL_ENV`,
`PYTHONPATH`, `CARGO_HOME`, `JAVA_HOME`, `GOPATH`, `NVM_DIR` and the rest). Every one of those
is a path, a locale, a terminal setting or an identity the command could ask the operating
system for anyway. That is the property to preserve when adding to it.

Deliberately absent, each because the value is a credential or carries one: `PICOAGENT_API_KEY`
and `OPENAI_API_KEY` (the documented way to supply the model key), `SSH_AUTH_SOCK` (a live
agent socket), `HTTP_PROXY` and `HTTPS_PROXY` (routinely `http://user:pass@proxy`), and every
`AWS_`/`GH_`/`GITHUB_`/`NPM_` variable.

An allowlist too tight is a tool that cannot do its job, so there are two ways out, and both are
`USER_ONLY` - a cloned repository may not decide what a command sees, which is exactly the hole
`[plugins.credential-guard] extra_allow_env` was closed for:

```toml
shell_env_allow = ["ACME_BUILD_FLAG"]   # one more variable, named by you
shell_env = "inherit"                   # the old behaviour, whole, by name
```

Anything that is not the exact string `inherit` means the allowlist, so a typo or a value of the
wrong type fails closed; a `shell_env_allow` that is not a list of strings names no variable and
adds none.

What still gets through, stated so it is not assumed. The allowlist passes what it passes:
`PATH` and `HOME` name directories, `USER` names an account, and a build that prints its own
configuration prints it. A command that opens a credential *file* still reads it - that is the
tool-layer guard's surface, above, and the shell file check there is a speed bump. And the tool
description tells the model the environment is trimmed, because a variable that is simply
absent looks like one set to the empty string, and an agent that does not know the difference
reports a broken build instead of saying what it could not see.

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

**A plugin's controls are void if the plugin is not loaded**, and it is worth being exact about
which ones those now are. The environment allowlist is core's and holds either way. What
credential-guard supplies on top is the `tool_call` guard over every protected path, the
`/secrets` store, and `extra_deny_patterns` - the narrowing that refuses a variable the
allowlist would have passed. Untrusted or disabled, those are the controls that are absent,
which is still worth knowing at the moment it happens: a skip is reported the way it is above,
on stderr and as a `plugin_skipped` event.

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
