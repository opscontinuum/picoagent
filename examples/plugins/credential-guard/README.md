# credential-guard

Stdlib-only credential storage and leak prevention for picoagent. No third-party dependencies.

## What it does

* **Storage**: `/secrets set <provider>` prompts for a key with hidden input (`getpass`, never
  echoed, never a command argument) and writes it to `~/.picoagent/credentials` at `0600`
  permissions - not encrypted, the same trust model as `~/.netrc` or `~/.aws/credentials` (OS
  file permissions, not a hand-rolled cipher). `/secrets show|delete|list` manage it; `show`
  only ever prints a masked last-4-characters form.
* **Leak prevention**: the environment **allowlist** this plugin invented is core's now.
  `tools.SHELL_ENV_ALLOWLIST` is what the built-in `shell` tool passes by default, whether or
  not anything is installed, because a control that only exists in an opt-in plugin is absent
  for everyone who has not installed it (DISA V-222444). It's an allowlist rather than a
  denylist of secret-shaped names because a denylist can't be complete - `OPENROUTER_KEY`,
  `GH_PAT`, `PRIVATE_KEY`, `AWS_ACCESS_KEY_ID` and `DATABASE_URL` all sail past one. Add names
  your commands need with `shell_env_allow` in your own config.

  What this plugin still adds is the **narrowing**: its replacement `shell` tool applies
  `extra_deny_patterns` on top, which only ever refuses more, so a site that knows the shape of
  its own secret variable names can refuse one the allowlist would have passed - including one
  the user named in `shell_env_allow` or `extra_allow_env` by mistake.
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
sees, and the files that hold it are refused by the tool layer. The first of those no longer
depends on this plugin loading - core strips the environment either way - so an untrusted or
disabled plugin costs you the deny patterns and the whole tool-layer file guard, not the
allowlist.

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
