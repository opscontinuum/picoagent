"""agents - delegation, background work and scheduling, as skills rather than code.

The four capabilities here need no new machinery. picoagent already ships ``shell``, and
``picoagent -p "<prompt>"`` is a complete non-interactive agent run, so delegating to a child,
detaching one, and scheduling one are all things the existing tools can already do. What was
missing was not capability but instruction: the model had no idea it could do any of it.

So this plugin registers no tools and patches nothing. It ships skills, which are the cheapest
extension picoagent has: prompt text loaded on demand, replaceable by anyone, and carrying no
runtime surface to go wrong. A capability that can be a skill should be a skill.

The one thing register() does is keep the plugin honest about that: if a future change adds a
tool here, it should be because a skill genuinely could not express it.
"""
from __future__ import annotations


def register(api) -> None:
    """No tools, no events, no commands. The skills in ``skills/`` are the whole plugin."""
