# Security documentation

Security posture, boundaries, and review artifacts for picoagent.

| Document | Covers |
|---|---|
| [trust-boundaries.md](trust-boundaries.md) | What picoagent trusts, what it doesn't, and where a secret can and cannot travel |
| [threat-model.md](threat-model.md) | Assets, adversaries, and 25 ranked threats across six surfaces - each with the countermeasure taken, the mitigations available, and which was chosen. Section 8 lists what is still open |

## Planned

Not yet written. Listed so the gaps are visible rather than implied:

* **Credential handling** - the full key lifecycle, and the findings from the credential-guard
  review with their resolutions.
* **Supply chain** - the full picture: what an internal mirror changes, and the provenance of
  picoagent's own releases rather than only its plugins'. The plugin half is written:
  [trust-boundaries.md](trust-boundaries.md#requiring-a-hash-or-a-signature-before-a-plugin-may-be-installed)
  covers the trust fingerprint and the `plugin-pins.toml` verification policy, and threat-model
  T9 records what that policy does and does not close.
* **Deployment guidance** - running in an air-gapped or accredited environment: what reads the
  filesystem, what reaches the network, and how to constrain both.

## Standing constraints

These hold across the codebase and are worth stating once:

* **Standard library only in core.** No third-party packages, so the dependency attack surface
  of the harness itself is the Python standard library. Plugins may declare `python_deps`; that
  is a decision the plugin's user makes explicitly.
* **No outbound calls except the configured model endpoint.** The core makes exactly one kind
  of request, `POST {base_url}/chat/completions`, plus `GET {base_url}/models` for `/model
  list`. `plugin add` additionally runs `git` against whatever host the spec names.
* **A tool result is untrusted input.** Tool output is appended to the session and replayed to
  the model as prompt context. Anything a tool prints is in the conversation from then on -
  which is both the leak path to defend and a prompt-injection surface to treat as data.
* **Plugin code runs with the user's privileges.** There is no sandbox. The boundary is the
  trust decision at load time, not containment at run time.
* **A site can require verified plugins, and none does by default.** Writing
  `~/.picoagent/plugin-pins.toml` makes a published hash or an accepted signing key a
  precondition of installing a plugin and hash-pins its `python_deps`; absent the file, nothing
  changes. Off by default is a real gap, not a shipped control - see threat-model T9.
