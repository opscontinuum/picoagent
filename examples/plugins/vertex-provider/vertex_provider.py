"""Google Vertex AI (Gemini) provider plugin - standard library only.

Gemini does **not** speak the OpenAI wire format, so this plugin maps picoagent's
neutral messages to Gemini's ``contents`` / ``parts`` / ``functionCall`` /
``functionResponse`` structures and streams from::

    POST https://{location}-aiplatform.googleapis.com/v1/projects/{project}/locations/{location}
         /publishers/google/models/{model}:streamGenerateContent?alt=sse

Authentication is an OAuth bearer token: ``GOOGLE_OAUTH_ACCESS_TOKEN`` if set,
otherwise ``gcloud auth print-access-token``.

Configuration (``[plugins.vertex-provider]`` **in your own config.toml**, or env vars)::

    project  = "my-gcp-project"        # GOOGLE_CLOUD_PROJECT
    location = "us-central1"           # GOOGLE_CLOUD_LOCATION
    base_url = "http://127.0.0.1:8766" # VERTEX_BASE_URL - override for fakes / proxies

Every one of these decides where an OAuth bearer token is sent, so all four are read from the
user layer of the config only - ``api.plugin_config()`` does not carry a repository's values.
A cloned repository setting ``base_url`` would receive a live Google access token, minted from
your ``gcloud`` login, on the first turn. See ``docs/security/trust-boundaries.md``. A redirect
cannot move the token either: the request goes through core's opener, which refuses a redirect
that leaves the origin ``base_url`` names rather than following it to a host you did not choose.

Run with:  picoagent --provider vertex -m gemini-2.5-pro
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import threading
import urllib.error
import urllib.request
from typing import Any

# Core's opener, rather than a plain ``urlopen`` or a copy of core's redirect handler. urllib
# re-sends the ``Authorization`` header to whatever a ``Location`` names, so a 302 from a gateway
# would hand a live Google OAuth token, and the conversation in the request body, to a host the
# user never configured; the handler behind this opener refuses such a redirect instead of
# following it with the header dropped, because the body is worth stealing on its own.
#
# The leading underscore says core promises nothing about the name, and this plugin takes that
# trade knowingly: a copied security control goes stale silently, while a rename in core breaks
# this import loudly at plugin load, where a human reads it. Core should export the opener (or a
# builder for one) as public API and say in docs/plugin-authoring.md that a plugin sending a
# credential over HTTP is expected to use it; until it does, this is the honest import.
from picoagent.core.provider import _OPENER as SAME_ORIGIN_OPENER
# The sentence core tells a model about an interrupted call, reused rather than reworded. What a
# provider owes its own wire is the *shape* of the answer; what the model is told happened is the
# same fact on every wire, and two wordings would be two chances to say something untrue.
from picoagent.core.provider import INTERRUPTED_TOOL_RESULT
from picoagent.core.types import Message, StreamEvent, ToolCall, ToolResult, new_id

log = logging.getLogger("vertex_provider")

# Gemini's function-declaration schema is an OpenAPI subset; anything else is rejected with 400.
GEMINI_SCHEMA_KEYS = frozenset({"type", "description", "properties", "required", "items",
                                "enum", "nullable", "format", "anyOf"})


def clean_schema(schema: Any) -> Any:
    """Recursively drop JSON-schema keys Gemini does not accept (``additionalProperties``, ``default``...)."""
    if isinstance(schema, list):
        return [clean_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    cleaned: dict = {}
    for key, value in schema.items():
        if key not in GEMINI_SCHEMA_KEYS:
            continue
        if key == "properties":      # property names are user-defined; clean their schemas, not the names
            cleaned[key] = {name: clean_schema(sub) for name, sub in value.items()}
        else:
            cleaned[key] = clean_schema(value)
    return cleaned


def to_gemini_contents(messages: list[Message], call_names: dict[str, str]) -> list[dict]:
    """Map neutral messages to Gemini ``contents``.

    Gemini has no tool-call ids: a ``functionResponse`` is matched by *name*, so we
    remember ``call_names[tool_call_id] = tool_name`` while walking assistant messages.

    The response turn is built from the *calls*, pulling each result forward to sit beside the
    call it answers, rather than emitted where the ``role: tool`` message happens to fall. Gemini
    checks a count, not a set of ids - see :func:`_response_turn` - and a count only comes out
    even if both halves are assembled in one place. A result nothing called (a plugin rewriting
    history can leave one) still gets its own turn below, because dropping it would lose what a
    tool reported.
    """
    contents: list[dict] = []
    paired: set[str] = set()
    for index, message in enumerate(messages):
        if message.role == "user":
            parts = [{"inlineData": {"mimeType": img["media_type"], "data": img["data"]}} for img in message.images]
            parts.append({"text": message.text or "(empty)"})
            contents.append({"role": "user", "parts": parts})
        elif message.role == "assistant":
            parts = [{"text": message.text}] if message.text else []
            for call in message.tool_calls:
                call_names[call.id] = call.name
                parts.append({"functionCall": {"name": call.name, "args": call.args}})
            contents.append({"role": "model", "parts": parts or [{"text": "…"}]})
            responses = _response_turn(message, messages[index + 1:], paired)
            if responses:
                contents.append({"role": "user", "parts": responses})
        elif message.role == "tool":
            orphans = [result for result in message.tool_results if result.tool_call_id not in paired]
            if orphans:
                contents.append({"role": "user", "parts": [
                    _response_part(call_names.get(result.tool_call_id, "tool"), result)
                    for result in orphans]})
    return contents


def _response_turn(assistant: Message, later: list[Message], paired: set[str]) -> list[dict]:
    """The one user turn that answers ``assistant``'s calls, with stand-ins for the unanswered.

    Gemini rejects a model turn whose ``functionCall`` parts outnumber the ``functionResponse``
    parts of the turn after it - *"Please ensure that the number of function response parts is
    equal to the number of function call parts of the function call turn"*, a 400 on every
    subsequent request, not only the one that produced the gap. The loop appends the assistant
    message when the model stops streaming and the results only once the whole batch has run, so
    a ``KeyboardInterrupt`` in between (a batch can be one long shell command) ends the process
    with the log in exactly that shape, and resuming it wedges the session behind a provider error
    that names none of this.

    Two things make this repair different from the OpenAI one in ``picoagent.core.provider``,
    and both come from Gemini having no tool-call ids. A ``functionResponse`` is matched by
    position and name, so the responses have to be *one turn* directly after the calls - a
    stand-in in a turn of its own would leave the count of the following turn wrong, and add a
    second user turn where the format allows one. And nothing can be matched afterwards, so the
    pairing is done here, once, by walking the calls in order: ``paired`` tells the ``role: tool``
    branch which results have already gone out so it cannot send them twice.

    Results are looked for in every later message rather than only the one that should follow,
    for the same reason core does it: a plugin that rewrites history can move a result, and
    answering a call that *is* answered further down would put the same tool's output in twice.

    The repair belongs here rather than in the session log, which goes on recording what actually
    happened - a call with no result. This is one dialect's rule about a request body.
    """
    if not assistant.tool_calls:
        return []
    results = {result.tool_call_id: result for message in later for result in message.tool_results}
    parts, missing = [], []
    for call in assistant.tool_calls:
        result = results.get(call.id)
        if result is None:
            missing.append(call)
            parts.append(_stand_in_part(call))
        else:
            paired.add(call.id)
            parts.append(_response_part(call.name, result))
    if missing:
        # Not a warning: this renders the same history on every turn for the rest of the session,
        # so a warning would repeat until the user stopped reading it. `-v` shows it once per turn
        # to whoever is asking why the model is talking about an unknown outcome.
        log.info("answering %d interrupted tool call(s) for this request: %s",
                 len(missing), ", ".join(call.name for call in missing))
    return parts


def _response_part(name: str, result: ToolResult) -> dict:
    """What a tool actually reported, as the part Gemini pairs with a ``functionCall``."""
    return {"functionResponse": {"name": name,
                                 "response": {"output": result.content, "error": result.is_error}}}


def _stand_in_part(call: ToolCall) -> dict:
    """The answer to a call the log never recorded a result for.

    No ``error`` key, unlike a real result. The field is a boolean and the outcome is not one:
    ``false`` invites the model to build on work that may not exist, ``true`` invites it to retry
    a command that may already have run. Gemini takes any JSON object as ``response``, so leaving
    the field out says exactly as much as is known, and the text says the rest.
    """
    return {"functionResponse": {"name": call.name, "response": {"output": INTERRUPTED_TOOL_RESULT}}}


class VertexProvider:
    name = "vertex"

    def __init__(self, project: str, location: str, base_url: str | None = None, token: str | None = None):
        self.project, self.location = project, location
        self._base = (base_url or f"https://{location}-aiplatform.googleapis.com").rstrip("/")
        self._token = token

    # ------------------------------------------------------------------ auth
    def access_token(self) -> str:
        """Explicit token > env var > gcloud CLI. Raises ``RuntimeError`` if none works."""
        if self._token:
            return self._token
        if os.environ.get("GOOGLE_OAUTH_ACCESS_TOKEN"):
            return os.environ["GOOGLE_OAUTH_ACCESS_TOKEN"]
        try:
            return subprocess.run(["gcloud", "auth", "print-access-token"], capture_output=True, text=True,
                                  timeout=20, check=True).stdout.strip()
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"no Vertex credentials: set GOOGLE_OAUTH_ACCESS_TOKEN or install gcloud ({exc})")

    # ------------------------------------------------------------------ request
    def _request_body(self, system: str, messages: list[Message], tools, max_tokens: int,
                      thinking: str, temperature: float | None = None) -> dict:
        body: dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": to_gemini_contents(messages, {}),
            "generationConfig": {"maxOutputTokens": max_tokens},
        }
        if tools:
            body["tools"] = [{"functionDeclarations": [
                {"name": t.name, "description": t.description, "parameters": clean_schema(t.parameters)}
                for t in tools]}]
        if temperature is not None:
            # Gemini puts sampling in generationConfig, alongside maxOutputTokens.
            # `is not None`: 0.0 is a setting, not an absence.
            body["generationConfig"]["temperature"] = temperature
        budgets = {"low": 1024, "medium": 8192, "high": 24576}
        if thinking in budgets:
            body["generationConfig"]["thinkingConfig"] = {"thinkingBudget": budgets[thinking], "includeThoughts": True}
        return body

    def _url(self, model: str) -> str:
        return (f"{self._base}/v1/projects/{self.project}/locations/{self.location}"
                f"/publishers/google/models/{model}:streamGenerateContent?alt=sse")

    async def stream(self, *, system, messages, tools, model, max_tokens, thinking="off",
                     temperature=None):
        """Stream ``StreamEvent``s. The blocking HTTP read runs in a thread and feeds a queue."""
        try:
            token = self.access_token()
        except RuntimeError as exc:
            yield StreamEvent("error", error=str(exc))
            return
        request = urllib.request.Request(
            self._url(model), method="POST",
            data=json.dumps(self._request_body(system, messages, tools, max_tokens, thinking,
                                               temperature)).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})

        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()
        threading.Thread(target=self._read_sse, args=(request, queue, loop), daemon=True).start()

        usage: dict[str, int] = {}
        while (item := await queue.get()) is not None:
            if isinstance(item, Exception):
                yield StreamEvent("error", error=str(item))
                return
            if item.get("usageMetadata"):
                meta = item["usageMetadata"]
                usage = {"input": meta.get("promptTokenCount", 0), "output": meta.get("candidatesTokenCount", 0)}
            for event in self._events_from_chunk(item):
                yield event
        yield StreamEvent("done", usage=usage)

    @staticmethod
    def _read_sse(request, queue: asyncio.Queue, loop: asyncio.AbstractEventLoop) -> None:
        """Thread body: push each ``data:`` JSON object, an Exception on failure, then ``None``."""
        def put(item) -> None:
            """Hand ``item`` to the consumer, or drop it once there is no consumer left.

            ``stream`` returns on the first error event - a refused redirect, an HTTP failure -
            and its loop is closed while this thread is still finishing. ``call_soon_threadsafe``
            raises on a closed loop, and this thread has nowhere to report that, so it would die
            printing a traceback about an outcome the session already handled.
            """
            try:
                loop.call_soon_threadsafe(queue.put_nowait, item)
            except RuntimeError:
                pass
        try:
            with SAME_ORIGIN_OPENER.open(request, timeout=600) as response:
                for raw in response:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if line.startswith("data:"):
                        try:
                            put(json.loads(line[5:]))
                        except json.JSONDecodeError:
                            continue
        except urllib.error.HTTPError as exc:
            put(RuntimeError(f"HTTP {exc.code}: {exc.read().decode(errors='replace')[:500]}"))
        except Exception as exc:  # noqa: BLE001 - surface anything as a provider error
            put(RuntimeError(f"{type(exc).__name__}: {exc}"))
        put(None)

    @staticmethod
    def _events_from_chunk(chunk: dict):
        """Translate one Gemini SSE chunk into zero or more StreamEvents."""
        for candidate in chunk.get("candidates", []):
            for part in (candidate.get("content") or {}).get("parts", []):
                if "functionCall" in part:
                    fc = part["functionCall"]
                    yield StreamEvent("tool_call", tool_call=ToolCall(new_id(), fc["name"], fc.get("args") or {}))
                elif part.get("thought"):
                    yield StreamEvent("thinking", text=part.get("text", ""))
                elif "text" in part:
                    yield StreamEvent("text", text=part["text"])


def register(api):
    cfg = api.plugin_config()
    api.warn_about_project_config()
    api.register_provider(VertexProvider(
        project=cfg.get("project") or os.environ.get("GOOGLE_CLOUD_PROJECT", "my-project"),
        location=cfg.get("location") or os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1"),
        base_url=cfg.get("base_url") or os.environ.get("VERTEX_BASE_URL"),
        token=cfg.get("token"),
    ))
