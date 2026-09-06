"""agents - delegation, background work and scheduling, mostly as skills rather than code.

Most of what this plugin offers needs no new machinery. picoagent already ships ``shell``, and
``picoagent -p "<prompt>"`` is a complete non-interactive agent run, so detaching a run and
scheduling one are things the existing tools can already do. What was missing was not capability
but instruction: the model had no idea it could do any of it. Skills are the cheapest extension
picoagent has - prompt text loaded on demand, replaceable by anyone, no runtime surface to go
wrong - so those capabilities stay skills. A capability that can be a skill should be a skill.

Delegation is the exception, and register() now registers one tool for it. Two things a skill
could not do:

* **Reliability of the call.** The skill asks the model to write a correct ``picoagent -p '...'``
  command, quoting and all. Small models get that wrong, and a malformed command buys nothing.
  A two-parameter schema is much harder to call incorrectly.
* **A turn cap.** Core's loop runs until the model stops calling tools; there is no cap. From
  ``shell`` the only stop is a process timeout, which kills the child and discards its output.
  Running the child in-process lets the parent end it at a turn count and keep what it said.

The skills stay: ``delegate`` still documents the subprocess route, which is the right one when
the child must outlive the parent's turn or run somewhere else.
"""
from __future__ import annotations

from agent_tool import AgentTool


def register(api) -> None:
    """One tool. The skills in ``skills/`` are the rest of the plugin."""
    api.register_tool(AgentTool(api))
