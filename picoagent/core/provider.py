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
import contextlib
import json
import logging
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, AsyncIterator, Iterator, Protocol, runtime_checkable

from .text import safe_for_display
from .types import Message, StreamEvent, ToolCall, ToolSpec, new_id

log = logging.getLogger("picoagent.provider")


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

#: What the model is told about a tool call the log never recorded a result for. Everything it
#: says has to be true of every way that happens - a Ctrl-C during a long batch, a crash, a kill
#: - so it claims only what is known: the call was recorded, the result was not, and the effect
#: is undetermined. Saying "it failed" would invite the model to retry a command that may have
#: already run; saying "it succeeded" would invite it to build on work that may not exist.
INTERRUPTED_TOOL_RESULT = ("[picoagent: no result was recorded for this tool call - the session "
                           "ended before the tool batch finished. Whether it ran at all, and what "
                           "it changed, is unknown. Check the current state before retrying it.]")


def to_openai_messages(system: str, messages: list[Message]) -> list[dict]:
    """Map neutral messages to the OpenAI chat format (system first, tool results as ``role: tool``)."""
    out: list[dict] = [{"role": "system", "content": system}]
    for index, message in enumerate(messages):
        if message.role == "user":
            out.append(_user_message(message))
        elif message.role == "assistant":
            entry: dict[str, Any] = {"role": "assistant", "content": message.text or None}
            if message.tool_calls:
                entry["tool_calls"] = [{"id": c.id, "type": "function",
                                        "function": {"name": c.name, "arguments": json.dumps(c.args)}}
                                       for c in message.tool_calls]
            out.append(entry)
            out.extend(_stand_in_results(message, messages[index + 1:]))
        elif message.role == "tool":
            out.extend({"role": "tool", "tool_call_id": r.tool_call_id, "content": r.content or "(no output)"}
                       for r in message.tool_results)
    return out


def _stand_in_results(assistant: Message, later: list[Message]) -> list[dict]:
    """``role: tool`` entries for the calls in ``assistant`` that no later message answers.

    OpenAI, and every strict server that copies it, rejects an assistant message carrying
    ``tool_calls`` unless a ``role: tool`` message answering each one follows it - with a 400, on
    every subsequent turn, not just the one that produced the gap. The loop appends the assistant
    message when the model stops streaming and the results only once the whole batch has run, so a
    ``KeyboardInterrupt`` in between (a batch can be one long shell command) ends the process with
    the log in exactly that shape. Resuming it then wedges the session permanently, and what the
    user sees is an opaque provider error rather than anything naming the cause.

    The repair belongs here rather than in the log because this is the constraint's own layer: it
    is one dialect's rule about a request body, not a fact about what happened. The session file
    keeps recording what actually happened - a call with no result - which is what an append-only
    log with parent pointers is for, and no other reader has to know about this rule. A provider
    with a different dialect maps messages itself and answers for its own format.

    Answered ids are collected from every later message, not just the ``role: tool`` one that
    should immediately follow, because emitting a second entry for an id that is answered further
    down would trade this 400 for a duplicate-id one. One entry per *distinct* unanswered id, for
    the same reason: an assistant message repeating an id is already malformed - a model that
    reused one, or a plugin that rewrote the batch - and two stand-ins for it would be the
    duplicate-id 400 as well, produced by the code that exists to avoid it.
    """
    if not assistant.tool_calls:
        return []
    answered = {result.tool_call_id for message in later for result in message.tool_results}
    missing = list({call.id: call for call in assistant.tool_calls
                    if call.id not in answered}.values())
    if missing:
        # Not a warning: this renders the same history on every turn for the rest of the session,
        # so a warning would repeat until the user stopped reading it. `-v` shows it once per turn
        # to whoever is asking why the model is talking about an unknown outcome.
        log.info("answering %d interrupted tool call(s) for this request: %s",
                 len(missing), ", ".join(call.id for call in missing))
    return [{"role": "tool", "tool_call_id": call.id, "content": INTERRUPTED_TOOL_RESULT}
            for call in missing]


def _user_message(message: Message) -> dict:
    if not message.images:
        return {"role": "user", "content": message.text or "(empty)"}
    content = [{"type": "image_url", "image_url": {"url": f"data:{img['media_type']};base64,{img['data']}"}}
               for img in message.images]
    if message.text:
        content.append({"type": "text", "text": message.text})
    return {"role": "user", "content": content}


#: The only schemes a model endpoint may use. ``urlopen`` also speaks ``file:``, ``ftp:`` and
#: ``data:``, and it resolves them without complaint: a ``base_url`` of ``file:///etc`` makes this
#: client open local paths and hand their contents back as if a server had sent them. ``providers``
#: is in ``config.USER_ONLY``, so a cloned repository cannot set the URL - but an environment
#: variable, a typo, and a plugin passing a value out of its own ``[plugins.<name>]`` table all
#: arrive at the same argument, and none of them is checked anywhere else.
#:
#: This is a check on the *scheme* and deliberately not on the host. ``http://127.0.0.1:11434`` is
#: Ollama, and a local model server is the case this client is most used for, so loopback,
#: link-local and private addresses are all allowed on purpose: an SSRF filter here would refuse
#: the headline configuration. What keeps a repository from choosing the host is
#: ``config.USER_ONLY``, not this function. See docs/security/trust-boundaries.md.
HTTP_SCHEMES = ("http", "https")


def check_base_url(url: str) -> str | None:
    """Why ``url`` cannot be a model endpoint, or ``None`` when it can.

    A returned string rather than a raise, because a wrong ``base_url`` is a config file's
    mistake and not a bug, and the two callers owe the user different things: ``stream`` an
    ``error`` event like any other provider failure, ``list_models`` a raised message that
    ``/model list`` already knows how to print.
    """
    scheme = urllib.parse.urlsplit(url).scheme.lower()
    if scheme in HTTP_SCHEMES:
        return None
    found = f"its scheme is '{scheme}'" if scheme else "it names no scheme"
    return (f"refusing to use {safe_for_display(url)} as a model endpoint: "
            f"base_url must be http or https, {found}")


class RedirectRefused(Exception):
    """A redirect that would take a credentialed request off the origin the user configured."""


def _origin(url: str) -> tuple[str, str, int | None] | None:
    """``(scheme, host, port)`` for ``url``, or ``None`` when it has no usable one.

    Port is filled in from the scheme when the URL omits it, so ``https://h`` and ``https://h:443``
    are the same origin and not two. A ``Location`` header is written by whoever is answering, so
    a port that is not a number is a shape this has to have an answer for: ``None``, which the
    caller reads as "not the same origin as anything".
    """
    parts = urllib.parse.urlsplit(url)
    try:
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    return scheme, (parts.hostname or "").lower(), port or {"http": 80, "https": 443}.get(scheme)


class _SameOriginRedirects(urllib.request.HTTPRedirectHandler):
    """Follows a redirect only while it stays on the origin the request started from.

    ``urllib`` re-sends every header that is not about the body to whatever ``Location`` names, so
    a gateway answering ``/models`` with a 302 to another host was handed ``Authorization: Bearer
    <key>`` - demonstrated against a second local server, which received the key in full.
    ``requests`` and ``curl`` both strip the header on a cross-origin redirect. urllib does not,
    and it will also follow a redirect to ``ftp:``, so the scheme check has to be applied to the
    URL actually fetched rather than only to the one the user configured.

    Origin is scheme, host and port, all three. Host alone would let a redirect downgrade an
    ``https`` endpoint to ``http`` and put the key on the wire in clear; port alone would treat two
    services on one machine as one. An ``http`` endpoint redirecting to ``https`` on the same host
    is refused too, which is the one legitimate case this costs - the fix is to configure the
    ``https`` URL, which is what anyone sending a key over the first URL should have done anyway.

    Refused rather than followed with the header dropped, which is where this differs from
    ``requests``. Dropping the credential is the whole answer for an ordinary API client, but the
    body of a ``/chat/completions`` request is the user's conversation and its tool results, so a
    stripped-header request still delivers to the new host the thing worth stealing. A redirect
    that a model gateway cannot express within its own origin is not a request this client makes.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        refusal = self._refusal(req, newurl)
        if refusal is None:
            return super().redirect_request(req, fp, code, msg, headers, newurl)
        # `fp` is the live 302, and urllib reads and closes it only after this method *returns*.
        # Refusing is the one path that never returns, so it is the one path that has to close it;
        # otherwise the connection is left to the garbage collector and turns up later as
        # `ResourceWarning: unclosed <socket.socket ...>` in whatever code was running by then.
        with contextlib.suppress(Exception):
            # Suppressed rather than reported: a socket that objects on the way down would raise
            # in place of the refusal, and the caller would read a security answer as an I/O
            # failure. Nothing this close can say changes what the request may not do.
            fp.close()
        raise RedirectRefused(refusal)

    @staticmethod
    def _refusal(req, newurl: str) -> str | None:
        """Why this redirect must not be followed, or ``None`` when it may be."""
        scheme = urllib.parse.urlsplit(newurl).scheme.lower()
        if scheme not in HTTP_SCHEMES:
            return (f"refusing to follow a redirect to {safe_for_display(newurl)}: "
                    f"a model endpoint must be http or https, its scheme is '{scheme}'")
        origin, target = _origin(req.full_url), _origin(newurl)
        if origin is None or target is None or origin != target:
            return (f"refusing to follow a redirect from {safe_for_display(req.full_url)} to "
                    f"{safe_for_display(newurl)}: the request body and the Authorization header "
                    "on it would go to a host you did not configure. Set base_url to the "
                    "endpoint you mean")
        return None


#: The opener every request in this module goes through, so the redirect rule cannot be bypassed by
#: forgetting it at one call site. ``build_opener`` drops its own ``HTTPRedirectHandler`` in favour
#: of the subclass above. Its other default handlers (``file:``, ``ftp:``, ``data:``) stay, and are
#: unreachable: every URL this module fetches is scheme-checked, the configured one by
#: :func:`check_base_url` and a redirect target by the handler.
_OPENER = urllib.request.build_opener(_SameOriginRedirects)


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
        refusal = check_base_url(self._base)
        if refusal:
            yield StreamEvent("error", error=refusal)
            return
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
        refusal = check_base_url(self._base)
        if refusal:
            raise RuntimeError(refusal)
        headers = dict(self._headers)
        if self._key:
            headers["Authorization"] = f"Bearer {self._key}"
        request = urllib.request.Request(f"{self._base}/models", headers=headers, method="GET")
        try:
            with _OPENER.open(request, timeout=30) as response:
                payload = json.loads(response.read().decode(errors="replace"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(safe_for_display(
                self._scrub(f"HTTP {exc.code}: {exc.read().decode(errors='replace')[:300]}"))) from None
        except Exception as exc:  # noqa: BLE001 - surface transport failures the same way
            raise RuntimeError(safe_for_display(self._scrub(f"{type(exc).__name__}: {exc}"))) from None
        entries = payload.get("data") if isinstance(payload, dict) else None
        return sorted(str(e["id"]) for e in (entries or []) if isinstance(e, dict) and e.get("id"))

    def _scrub(self, text: str) -> str:
        """Never let the key itself appear in an error we surface.

        Error text reaches the terminal and the ``--json`` event stream, so a gateway that
        echoes the ``Authorization`` header back in a 401 body would otherwise print the key.

        It runs *before* :func:`~picoagent.core.text.safe_for_display` at every call site, and
        that order is load-bearing: bounding first could cut a key in half and leave the front
        of it as text no later pass recognises.
        """
        return text.replace(self._key, "[redacted]") if self._key else text

    def _read_sse(self, request, queue: asyncio.Queue, loop: asyncio.AbstractEventLoop) -> None:
        """Thread body: push each parsed chunk, an Exception on failure, then ``None`` as the sentinel."""
        put = lambda item: loop.call_soon_threadsafe(queue.put_nowait, item)  # noqa: E731
        try:
            with _OPENER.open(request, timeout=600) as response:
                for chunk in parse_sse(response):
                    put(chunk)
        except urllib.error.HTTPError as exc:
            put(RuntimeError(safe_for_display(
                self._scrub(f"HTTP {exc.code}: {exc.read().decode(errors='replace')[:500]}"))))
        except Exception as exc:  # noqa: BLE001 - surface anything as a provider error
            put(RuntimeError(safe_for_display(self._scrub(f"{type(exc).__name__}: {exc}"))))
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
