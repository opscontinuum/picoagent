"""Model providers.

The core ships exactly one: an OpenAI-compatible ``/chat/completions`` client written
with ``urllib`` (no third-party packages). That single dialect covers OpenAI, xAI Grok,
Ollama, vLLM, llama.cpp, LM Studio, OpenRouter, Azure and most corporate gateways.
Providers with their own wire format (Vertex/Gemini, Bedrock) are plugins
that implement the same :class:`Provider` protocol.

Streaming design: ``urllib`` is blocking, so the HTTP read runs in a daemon thread
that pushes parsed SSE chunks onto an ``asyncio.Queue``; the async generator drains it.
Tool-call arguments arrive in fragments and are reassembled per ``index``.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import urllib.error
import urllib.request
from typing import Any, AsyncIterator, Iterator, Protocol, runtime_checkable

from .types import Message, StreamEvent, ToolCall, ToolSpec, new_id


@runtime_checkable
class Provider(Protocol):
    """Anything with a ``name`` and an async ``stream`` generator is a provider.

    ``list_models`` is *optional*: a provider that can enumerate what the server offers
    implements it, and callers check with ``hasattr`` rather than requiring it. Not every
    backend has an equivalent of ``GET /models``, and a provider shouldn't have to fake one.
    """
    name: str

    async def stream(self, *, system: str, messages: list[Message], tools: list[ToolSpec],
                     model: str, max_tokens: int, thinking: str,
                     temperature: float | None = None) -> AsyncIterator[StreamEvent]: ...


# --------------------------------------------------------------------------- mapping

def to_openai_messages(system: str, messages: list[Message]) -> list[dict]:
    """Map neutral messages to the OpenAI chat format (system first, tool results as ``role: tool``)."""
    out: list[dict] = [{"role": "system", "content": system}]
    for message in messages:
        if message.role == "user":
            out.append(_user_message(message))
        elif message.role == "assistant":
            entry: dict[str, Any] = {"role": "assistant", "content": message.text or None}
            if message.tool_calls:
                entry["tool_calls"] = [{"id": c.id, "type": "function",
                                        "function": {"name": c.name, "arguments": json.dumps(c.args)}}
                                       for c in message.tool_calls]
            out.append(entry)
        elif message.role == "tool":
            out.extend({"role": "tool", "tool_call_id": r.tool_call_id, "content": r.content or "(no output)"}
                       for r in message.tool_results)
    return out


def _user_message(message: Message) -> dict:
    if not message.images:
        return {"role": "user", "content": message.text or "(empty)"}
    content = [{"type": "image_url", "image_url": {"url": f"data:{img['media_type']};base64,{img['data']}"}}
               for img in message.images]
    if message.text:
        content.append({"type": "text", "text": message.text})
    return {"role": "user", "content": content}


def parse_sse(response) -> Iterator[dict]:
    """Yield the JSON payload of each ``data:`` line until ``[DONE]`` or EOF."""
    for raw in response:
        line = raw.decode("utf-8", errors="replace").strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            return
        try:
            yield json.loads(data)
        except json.JSONDecodeError:
            continue


# --------------------------------------------------------------------------- provider

class OpenAICompatProvider:
    """Streams from ``POST {base_url}/chat/completions``.

    Settings resolve from constructor args, then ``PICOAGENT_BASE_URL`` / ``PICOAGENT_API_KEY``,
    then ``OPENAI_BASE_URL`` / ``OPENAI_API_KEY``, then ``https://api.openai.com/v1``.
    Pass ``name`` to register the same client under another identity (e.g. ``grok``).
    """
    name = "openai"

    def __init__(self, base_url: str | None = None, api_key: str | None = None,
                 extra_headers: dict[str, str] | None = None, name: str | None = None):
        self._base = (base_url or os.environ.get("PICOAGENT_BASE_URL") or os.environ.get("OPENAI_BASE_URL")
                      or "https://api.openai.com/v1").rstrip("/")
        self._key = api_key or os.environ.get("PICOAGENT_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
        self._headers = extra_headers or {}
        if name:
            self.name = name

    def _request(self, system, messages, tools, model, max_tokens, thinking,
                 temperature=None) -> urllib.request.Request:
        body: dict[str, Any] = {"model": model, "messages": to_openai_messages(system, messages),
                                "max_tokens": max_tokens, "stream": True,
                                "stream_options": {"include_usage": True}}
        if tools:
            body["tools"] = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                                "parameters": t.parameters}} for t in tools]
        if temperature is not None:
            body["temperature"] = temperature        # `is not None`: 0.0 is a setting, not an absence
        if thinking in ("low", "medium", "high"):
            body["reasoning_effort"] = thinking      # servers that don't support it ignore the field
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream", **self._headers}
        if self._key:
            headers["Authorization"] = f"Bearer {self._key}"
        return urllib.request.Request(f"{self._base}/chat/completions", data=json.dumps(body).encode(),
                                      headers=headers, method="POST")

    async def stream(self, *, system, messages, tools, model, max_tokens, thinking="off",
                     temperature=None):
        request = self._request(system, messages, tools, model, max_tokens, thinking, temperature)
        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()
        threading.Thread(target=self._read_sse, args=(request, queue, loop), daemon=True).start()

        pending_calls: dict[int, dict] = {}     # tool_call index -> {id, name, args-so-far}
        usage: dict[str, int] = {}
        while (chunk := await queue.get()) is not None:
            if isinstance(chunk, Exception):
                yield StreamEvent("error", error=str(chunk))
                return
            if chunk.get("usage"):
                usage = {"input": chunk["usage"].get("prompt_tokens", 0),
                         "output": chunk["usage"].get("completion_tokens", 0)}
            for choice in chunk.get("choices", []):
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    yield StreamEvent("text", text=delta["content"])
                reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                if reasoning:
                    yield StreamEvent("thinking", text=reasoning)
                self._accumulate_tool_calls(delta, pending_calls)

        for index in sorted(pending_calls):
            yield StreamEvent("tool_call", tool_call=self._finish_tool_call(pending_calls[index]))
        yield StreamEvent("done", usage=usage)

    async def list_models(self) -> list[str]:
        """Model ids the server offers, from ``GET {base_url}/models``.

        urllib is blocking, so the request runs in the default executor rather than stalling
        the event loop. Raises on a transport or HTTP error - the caller (``/model list``)
        turns that into a readable message, since "the server is unreachable" is worth showing
        rather than silently rendering as an empty list.
        """
        return await asyncio.get_running_loop().run_in_executor(None, self._fetch_models)

    def _fetch_models(self) -> list[str]:
        headers = dict(self._headers)
        if self._key:
            headers["Authorization"] = f"Bearer {self._key}"
        request = urllib.request.Request(f"{self._base}/models", headers=headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode(errors="replace"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(self._scrub(f"HTTP {exc.code}: {exc.read().decode(errors='replace')[:300]}")) from None
        except Exception as exc:  # noqa: BLE001 - surface transport failures the same way
            raise RuntimeError(self._scrub(f"{type(exc).__name__}: {exc}")) from None
        entries = payload.get("data") if isinstance(payload, dict) else None
        return sorted(str(e["id"]) for e in (entries or []) if isinstance(e, dict) and e.get("id"))

    def _scrub(self, text: str) -> str:
        """Never let the key itself appear in an error we surface.

        Error text reaches the terminal and the ``--json`` event stream, so a gateway that
        echoes the ``Authorization`` header back in a 401 body would otherwise print the key.
        """
        return text.replace(self._key, "[redacted]") if self._key else text

    def _read_sse(self, request, queue: asyncio.Queue, loop: asyncio.AbstractEventLoop) -> None:
        """Thread body: push each parsed chunk, an Exception on failure, then ``None`` as the sentinel."""
        put = lambda item: loop.call_soon_threadsafe(queue.put_nowait, item)  # noqa: E731
        try:
            with urllib.request.urlopen(request, timeout=600) as response:
                for chunk in parse_sse(response):
                    put(chunk)
        except urllib.error.HTTPError as exc:
            put(RuntimeError(self._scrub(f"HTTP {exc.code}: {exc.read().decode(errors='replace')[:500]}")))
        except Exception as exc:  # noqa: BLE001 - surface anything as a provider error
            put(RuntimeError(self._scrub(f"{type(exc).__name__}: {exc}")))
        put(None)

    @staticmethod
    def _accumulate_tool_calls(delta: dict, pending: dict[int, dict]) -> None:
        """Merge streamed tool-call fragments (id/name/arguments arrive piecemeal)."""
        for fragment in delta.get("tool_calls") or []:
            slot = pending.setdefault(fragment.get("index", 0), {"id": "", "name": "", "args": ""})
            slot["id"] = fragment.get("id") or slot["id"]
            function = fragment.get("function") or {}
            slot["name"] += function.get("name") or ""
            slot["args"] += function.get("arguments") or ""

    @staticmethod
    def _finish_tool_call(slot: dict) -> ToolCall:
        try:
            args = json.loads(slot["args"] or "{}")
        except json.JSONDecodeError:
            args = {"_raw": slot["args"]}
        return ToolCall(slot["id"] or new_id(), slot["name"], args)


class ProviderRegistry:
    """Providers by name. Registering an existing name replaces it."""

    def __init__(self) -> None:
        self._providers: dict[str, Provider] = {}

    def register(self, provider: Provider) -> None:
        self._providers[provider.name] = provider

    def get(self, name: str) -> Provider:
        if name not in self._providers:
            raise KeyError(f"provider '{name}' not registered (available: {list(self._providers)})")
        return self._providers[name]

    def names(self) -> list[str]:
        return list(self._providers)
