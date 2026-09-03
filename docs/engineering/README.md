# Engineering diagrams

Structural reference for picoagent: what the pieces are, how a request moves through them,
what the data looks like, and how plugins get loaded. Narrative lives in
[../architecture.md](../architecture.md); these are the diagrams that narrative refers to.

| Document | Answers |
|---|---|
| [system-overview.md](system-overview.md) | What are the modules, which way do dependencies point, and how does an override win? |
| [request-lifecycle.md](request-lifecycle.md) | What happens between typing a prompt and getting an answer, and where can a plugin intervene? |
| [data-model.md](data-model.md) | What are the core types, and why is a session a tree rather than a list? |
| [continuity-tooling.md](continuity-tooling.md) | What are the continuity plugins for, how do ISCP/ITSCP/COOP differ, and where is this going? |
| [plugin-lifecycle.md](plugin-lifecycle.md) | How does a plugin get discovered, trusted, loaded, and what happens when its code changes? |

Diagrams are [Mermaid](https://mermaid.js.org/) in fenced blocks, which GitHub renders
directly. They are checked with `mermaid_lint` from
[picoagent-tools](https://github.com/opscontinuum/picoagent-tools) - the same tool this
project ships - so a diagram that stops parsing is caught rather than silently rendering as
a grey box.

Keep them honest: a diagram that disagrees with the code is worse than no diagram. If you
change the loop, the registries, or the trust flow, the matching diagram changes in the same
commit.
