"""compaction - keep long sessions inside the context window, as a plugin.

The core only stores messages. This plugin decides when history is too big and
replaces the older part with a model-written summary (recorded as a ``compaction``
entry, so the full transcript stays on disk).

Three triggers:
* ``/compact [instructions]``  - on demand.
* ``context`` event            - proactively, when the estimated size passes ``threshold_tokens``.
* ``provider_error`` event     - reactively, when the provider rejects the request as too long;
                                 the loop retries once the summary is in place.

Configuration (``[plugins.compaction]``)::

    threshold_tokens = 120000
    keep_recent = 8              # newest messages kept verbatim
"""
from __future__ import annotations

from picoagent.core.types import Message

SUMMARY_SYSTEM = "You compress conversations."
SUMMARY_PROMPT = """Summarise the conversation so far for a fresh assistant instance that must continue the work.
Include: the user's goal, decisions made, files touched (with paths), current state, next steps, open questions.
Be concrete and terse. Output only the summary."""
OVERFLOW_MARKERS = ("too long", "context length", "context_length", "maximum context", "token limit")


def estimate_tokens(messages: list[Message]) -> int:
    """Cheap ~4 chars/token estimate; good enough to decide when to summarise."""
    total = 0
    for m in messages:
        total += len(m.text) // 4
        total += sum(len(r.content) // 4 for r in m.tool_results)
        total += sum(len(str(c.args)) // 4 for c in m.tool_calls)
    return total


def render_transcript(messages: list[Message], result_chars: int = 800) -> str:
    """Flatten messages into text the summariser can read; tool results are trimmed."""
    lines = []
    for m in messages:
        line = f"{m.role}: {m.text or ''}"
        line += "".join(f"\n[tool {c.name} {c.args}]" for c in m.tool_calls)
        line += "".join(f"\n[result] {r.content[:result_chars]}" for r in m.tool_results)
        lines.append(line)
    return "\n\n".join(lines)


class Compactor:
    def __init__(self, api):
        cfg = api.plugin_config()
        self.api = api
        self.threshold = int(cfg.get("threshold_tokens", 120_000))
        self.keep_recent = int(cfg.get("keep_recent", 8))

    async def summarise(self, instructions: str = "") -> str | None:
        """Summarise everything but the newest ``keep_recent`` messages. Returns the summary or ``None``."""
        rt = self.api.rt
        messages = rt.session.messages()
        if len(messages) <= self.keep_recent:
            return None
        older = messages[:-self.keep_recent]
        prompt = SUMMARY_PROMPT + (f"\nExtra instructions: {instructions}" if instructions else "")
        summary = await self._ask_model(prompt + "\n\n" + render_transcript(older))

        message_entries = [e for e in rt.session.branch() if e["kind"] == "message"]
        keep_from = message_entries[-self.keep_recent]["id"]
        rt.session.append_compaction(summary, keep_from, estimate_tokens(older))
        return summary

    async def _ask_model(self, prompt: str) -> str:
        rt = self.api.rt
        provider = rt.providers.get(rt.provider_name)
        text = ""
        async for chunk in provider.stream(system=SUMMARY_SYSTEM, messages=[Message(role="user", text=prompt)],
                                           tools=[], model=rt.model, max_tokens=2048, thinking="off"):
            if chunk.type == "text":
                text += chunk.text
        return text.strip()

    # ------------------------------------------------------------------ handlers
    async def on_context(self, event: dict, rt) -> dict | None:
        """Proactive: summarise before the request is sent if history is too large."""
        if estimate_tokens(event["messages"]) <= self.threshold:
            return None
        summary = await self.summarise()
        if not summary:
            return None
        await rt.frontend.emit("notice", {"text": f"(auto-compacted; summary {len(summary)} chars)"})
        return {"messages": rt.session.messages()}

    async def on_provider_error(self, event: dict, rt) -> dict | None:
        """Reactive: if the provider says the prompt is too long, summarise and ask the loop to retry."""
        if not any(marker in event["error"].lower() for marker in OVERFLOW_MARKERS):
            return None
        return {"retry": True} if await self.summarise() else None

    async def on_compact(self, argstr: str, rt) -> str:
        summary = await self.summarise(argstr)
        return "nothing to compact" if summary is None else f"compacted:\n{summary[:500]}"


def register(api):
    compactor = Compactor(api)
    api.on("context", compactor.on_context)
    api.on("provider_error", compactor.on_provider_error)
    api.register_command("compact", compactor.on_compact, "summarise older messages [custom instructions]")
