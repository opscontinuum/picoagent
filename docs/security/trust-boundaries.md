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
`plugins.enabled` - each added plugin still faces the trust prompt. When a repository tries to
set a `USER_ONLY` key, the session says so at startup rather than dropping it silently.

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
| A key typed into a slash command | Slash commands short-circuit before `session.append_message`, so they are never recorded |

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
`tool_call` guard exists.

**Owner-restriction is not encryption.** The credentials file is `0600` on POSIX and
ACL-restricted via `icacls` on Windows - the same trust model as `~/.netrc`. Anything running
as that user can read it. Where `icacls` cannot be confirmed, the CLI says so rather than
implying protection it did not achieve.

**Prompt injection is not solved.** Tool output is untrusted content that reaches the model as
context. The zeroth-law skill instructs treating it as data, but that is guidance to a model,
not an enforced control.
