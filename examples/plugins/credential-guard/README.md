# credential-guard

Stdlib-only credential storage and leak prevention for picoagent. No third-party dependencies.

## What it does

* **Storage**: `/secrets set <provider>` prompts for a key with hidden input (`getpass`, never
  echoed, never a command argument) and writes it to `~/.picoagent/credentials` at `0600`
  permissions - not encrypted, the same trust model as `~/.netrc` or `~/.aws/credentials` (OS
  file permissions, not a hand-rolled cipher). `/secrets show|delete|list` manage it; `show`
  only ever prints a masked last-4-characters form.
* **Leak prevention**: replaces the built-in `shell` tool (auto-detected: bash/sh on Linux and
  macOS, PowerShell on Windows) with one that passes only an **allowlist** of environment
  variables to the command. The built-in `shell` tool passes the *entire* environment, so
  `env`/`echo $VAR` (or `$env:VAR` on Windows) would otherwise leak a key straight into the
  session log and the next prompt. It's an allowlist rather than a denylist of secret-shaped
  names because a denylist can't be complete - `OPENROUTER_KEY`, `GH_PAT`, `PRIVATE_KEY`,
  `AWS_ACCESS_KEY_ID` and `DATABASE_URL` all sail past one. Add names your commands need with
  `extra_allow_env`.
* **File protection**: blocks *any* tool call whose path argument names a protected file - the
  credentials store, `config.toml` (which can hold `api_key`), or `trust.json` - including via
  a symlink or hardlink alias, and blocks recursive tools like `grep_search` from being pointed
  at a directory that contains one. The check applies to every tool, not a list of known ones,
  so a tool added later doesn't inherit a bypass.

## What this does not protect against

Be clear-eyed about the boundary. The `cat`/`Get-Content` check on shell commands is a **speed
bump, not a control**: the shell runs as you, so anything you can read, the agent can read.
`python -c "open(path).read()"`, `sed`, `xxd`, a relative path, a path built from a variable, or
copying the file first all defeat it. Treat it as catching careless behavior, not a determined
attempt.

The real protection is the two things above it: the key isn't in the environment the command
sees, and the files that hold it are refused by the tool layer. And all of it is void if this
plugin isn't loaded and trusted - an untrusted plugin doesn't load, and then the built-in
unsanitized `shell` tool is what runs.

## Scope

Provider re-wiring only covers the built-in `openai`-named provider (the one picoagent core
always registers). Other provider plugins (grok-provider, vertex-provider, ...) manage their own
key resolution; this plugin doesn't override them. The env/file leak protections apply
regardless of provider.

## Use

```bash
picoagent -e path/to/examples/plugins/credential-guard
/secrets set openai
```

or add it to `.picoagent/config.toml`:

```toml
[plugins]
enabled = ["./examples/plugins/credential-guard"]
```

## Tests

From the picoagent repo root:

```bash
python -m unittest discover -s tests -v
```
