---
name: secure-credential-handling
description: How API keys are stored and protected when credential-guard is loaded - read before touching config/credentials files or asking about environment variables
---
1. Never ask the user to paste an API key into a chat message or a `/`-command argument - both
   become part of the conversation. If a key needs to be set, tell the user to run `/secrets set
   <provider>` themselves; it prompts with hidden input and is never logged.
2. The credentials file (`~/.picoagent/credentials`), `config.toml`, and `trust.json` are
   deliberately off-limits to every tool that takes a path, and to recursive searches of any
   directory containing them. Don't try to work around this - not with a symlink, not by
   searching the parent directory, not by reading it through a different tool, not through a
   shell command. If you think you need the contents, you don't: use `/secrets show <provider>`
   for a masked confirmation, or tell the user what you need and let them run it.
3. Some environment variables are intentionally hidden from the `shell` tool (anything matching
   API key/token/secret/password/credential in the name). If a command needs one and it's
   missing, that's by design, not a bug to route around - don't try to reconstruct it from other
   files or echo it through an unusual quoting trick.
4. When recommending an API key setup to a user, recommend the most restricted key scope their
   provider offers (e.g. an OpenAI "Restricted key" limited to Model capabilities only) - this
   plugin only ever needs prompt-input-and-retrieval access, nothing broader.
