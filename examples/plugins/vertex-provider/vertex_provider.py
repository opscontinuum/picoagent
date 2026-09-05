"""Google Vertex AI (Gemini) provider plugin - standard library only.

Gemini does **not** speak the OpenAI wire format, so this plugin maps picoagent's
neutral messages to Gemini's ``contents`` / ``parts`` / ``functionCall`` /
``functionResponse`` structures and streams from::

    POST https://{location}-aiplatform.googleapis.com/v1/projects/{project}/locations/{location}
         /publishers/google/models/{model}:streamGenerateContent?alt=sse

Authentication is an OAuth bearer token: ``GOOGLE_OAUTH_ACCESS_TOKEN`` if set,
otherwise ``gcloud auth print-access-token``.

Configuration (``[plugins.vertex-provider]`` in config.toml, or env vars)::

    project  = "my-gcp-project"        # GOOGLE_CLOUD_PROJECT
    location = "us-central1"           # GOOGLE_CLOUD_LOCATION
    base_url = "http://127.0.0.1:8766" # VERTEX_BASE_URL - override for fakes / proxies

Run with:  picoagent --provider vertex -m gemini-2.5-pro
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
import urllib.error
import urllib.request
from typing import Any

from picoagent.core.types import Message, StreamEvent, ToolCall, new_id

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
    """
    contents: list[dict] = []
    for message in messages:
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
        elif message.role == "tool":
            contents.append({"role": "user", "parts": [
                {"functionResponse": {"name": call_names.get(result.tool_call_id, "tool"),
                                      "response": {"output": result.content, "error": result.is_error}}}
                for result in message.tool_results]})
    return contents


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
        put = lambda item: loop.call_soon_threadsafe(queue.put_nowait, item)  # noqa: E731
        try:
            with urllib.request.urlopen(request, timeout=600) as response:
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
    api.register_provider(VertexProvider(
        project=cfg.get("project") or os.environ.get("GOOGLE_CLOUD_PROJECT", "my-project"),
        location=cfg.get("location") or os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1"),
        base_url=cfg.get("base_url") or os.environ.get("VERTEX_BASE_URL"),
        token=cfg.get("token"),
    ))
