"""Runs the full agent loop (prompt -> tool call -> tool exec -> second turn) against a fake server
for each provider dialect. Standard library only:  python -m unittest discover -s tests -v"""
from __future__ import annotations
import asyncio, json, os, sys, tempfile, threading, unittest, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from picoagent.core.config import load_config                       # noqa: E402
from picoagent.core.loop import AgentLoop, Runtime                  # noqa: E402
from picoagent.core.session import Session                          # noqa: E402
from picoagent.core.tools import BUILTIN_TOOLS                      # noqa: E402
from picoagent.core.provider import (OpenAICompatProvider, RedirectRefused,  # noqa: E402
                                     _SameOriginRedirects, to_openai_messages)
from picoagent.core.types import Message, ToolCall, ToolResult      # noqa: E402
from picoagent.plugins import loader                                # noqa: E402
from picoagent import cli                                           # noqa: E402
from picoagent.testing.fakes import FakeServer  # noqa: E402
from helpers import ROOT, temp_dir  # noqa: E402,F811


class ErrorScrubbingTests(unittest.TestCase):
    """A provider that echoes the Authorization header back in an error body must not leak it:
    error text reaches the terminal and the --json stream."""

    def test_the_key_is_redacted_from_error_text(self):
        provider = OpenAICompatProvider(base_url="http://x/v1", api_key="sk-supersecret-1234")
        scrubbed = provider._scrub("HTTP 401: bad key sk-supersecret-1234 rejected")
        self.assertNotIn("sk-supersecret-1234", scrubbed)
        self.assertIn("[redacted]", scrubbed)

    def test_an_empty_key_does_not_redact_everything(self):
        provider = OpenAICompatProvider(base_url="http://x/v1", api_key="")
        self.assertEqual(provider._scrub("HTTP 500: boom"), "HTTP 500: boom")


class Capture:
    """Minimal frontend that records events."""
    def __init__(self): self.events: list[tuple[str, dict]] = []; self.text = ""
    async def emit(self, e, p):
        self.events.append((e, p))
        if e == "assistant_delta": self.text += p["text"]
    async def ask(self, *a, **k): return True
    async def read_input(self): return None
    async def run(self, agent): pass


def make_runtime(tmp: Path) -> Runtime:
    os.environ["PICOAGENT_HOME"] = str(tmp / "home")
    cfg = load_config(tmp, {"model": "test"})
    rt = Runtime(cfg, tmp, Session(tmp / "s.jsonl", tmp))
    for t in BUILTIN_TOOLS:
        rt.tools.register(t())
    rt.frontend = Capture()
    return rt


class ListModelsTests(unittest.TestCase):
    """`GET /models` on the provider, and the `/model` command built on it."""

    def test_list_models_returns_sorted_ids_from_the_server(self):
        with FakeServer("openai") as srv:
            provider = OpenAICompatProvider(base_url=srv.url + "/v1", api_key="k")
            names = asyncio.run(provider.list_models())
        self.assertEqual(names, ["fake-large", "fake-small"])

    def test_list_models_sends_the_key(self):
        with FakeServer("openai") as srv:
            provider = OpenAICompatProvider(base_url=srv.url + "/v1", api_key="sekrit")
            asyncio.run(provider.list_models())
        self.assertEqual(srv.requests[0]["headers"]["Authorization"], "Bearer sekrit")

    def test_list_models_on_an_unreachable_server_raises_a_readable_error(self):
        provider = OpenAICompatProvider(base_url="http://127.0.0.1:9/v1", api_key="k")
        with self.assertRaises(RuntimeError) as caught:
            asyncio.run(provider.list_models())
        self.assertNotIn("Traceback", str(caught.exception))

    def test_model_list_command_marks_the_current_model(self):
        with tempfile.TemporaryDirectory() as d, FakeServer("openai") as srv:
            rt = make_runtime(Path(d))
            rt.providers.register(OpenAICompatProvider(base_url=srv.url + "/v1", api_key="k"))
            rt.provider_name, rt.model = "openai", "fake-small"
            out = asyncio.run(cli.list_models(rt))
        self.assertIn("* fake-small", out)
        self.assertIn("  fake-large", out)
        self.assertIn("2 models", out)

    def test_model_list_on_a_provider_that_cannot_enumerate_says_so(self):
        with tempfile.TemporaryDirectory() as d:
            rt = make_runtime(Path(d))
            rt.providers.register(_NoListing())
            rt.provider_name = "nolisting"
            out = asyncio.run(cli.list_models(rt))
        self.assertIn("cannot list models", out)

    def test_model_list_reports_an_unreachable_server_instead_of_raising(self):
        with tempfile.TemporaryDirectory() as d:
            rt = make_runtime(Path(d))
            rt.providers.register(OpenAICompatProvider(base_url="http://127.0.0.1:9/v1", api_key="k"))
            rt.provider_name = "openai"
            out = asyncio.run(cli.list_models(rt))
        self.assertIn("could not list models", out)


class ModelCommandTests(unittest.TestCase):
    def _command(self, rt):
        return rt.commands.get("model").handler

    def test_setting_a_model_emits_model_select(self):
        with tempfile.TemporaryDirectory() as d:
            rt = make_runtime(Path(d))
            cli.register_core_commands(rt)
            seen = []
            rt.events.on("model_select", lambda p, c: seen.append(p) or None, owner="test")
            asyncio.run(self._command(rt)("gpt-4.1", rt))
        self.assertEqual(rt.model, "gpt-4.1")
        self.assertEqual(seen[0]["previous"], "test")
        self.assertEqual(seen[0]["model"], "gpt-4.1")

    def test_bare_model_shows_current_without_changing_it(self):
        with tempfile.TemporaryDirectory() as d:
            rt = make_runtime(Path(d))
            cli.register_core_commands(rt)
            out = asyncio.run(self._command(rt)("", rt))
        self.assertEqual(rt.model, "test")
        self.assertIn("model: openai/test", out)
        self.assertIn("/model list", out)


class _NoListing:
    """A provider without the optional list_models - the protocol doesn't require it."""
    name = "nolisting"

    async def stream(self, **kw):
        return
        yield  # pragma: no cover - keeps this an async generator


class ProviderRoundTrip(unittest.TestCase):
    def _run(self, rt: Runtime, provider: str) -> Capture:
        rt.provider_name = provider
        asyncio.run(AgentLoop(rt).run("hello"))
        return rt.frontend

    def _assert_round_trip(self, fe: Capture, srv: FakeServer):
        results = [p["result"] for e, p in fe.events if e == "tool_result"]
        self.assertEqual(len(results), 1, "expected exactly one tool execution")
        self.assertIn("from-server", results[0].content)
        self.assertFalse(results[0].is_error)
        self.assertIn("tool said: from-server", fe.text)
        self.assertEqual(len(srv.requests), 2, "expected two model calls")

    def test_openai(self):
        with tempfile.TemporaryDirectory() as d, FakeServer("openai") as srv:
            rt = make_runtime(Path(d))
            rt.providers.register(OpenAICompatProvider(base_url=srv.url + "/v1", api_key="k"))
            self._assert_round_trip(self._run(rt, "openai"), srv)
            self.assertTrue(srv.requests[0]["body"]["tools"])

    def test_grok(self):
        with tempfile.TemporaryDirectory() as d, FakeServer("grok") as srv:
            rt = make_runtime(Path(d))
            os.environ["XAI_BASE_URL"], os.environ["XAI_API_KEY"] = srv.url + "/v1", "xai-test"
            loader.load_plugin(ROOT / "examples/plugins/grok-provider", rt,
                               loader.TrustStore(Path(d) / "home"), allow_untrusted=True)
            fe = self._run(rt, "grok")
            self._assert_round_trip(fe, srv)
            self.assertTrue(any(e == "thinking_delta" for e, _ in fe.events), "reasoning_content surfaced")

    def test_vertex(self):
        with tempfile.TemporaryDirectory() as d, FakeServer("vertex") as srv:
            rt = make_runtime(Path(d))
            os.environ["VERTEX_BASE_URL"], os.environ["GOOGLE_OAUTH_ACCESS_TOKEN"] = srv.url, "ya29.test"
            loader.load_plugin(ROOT / "examples/plugins/vertex-provider", rt,
                               loader.TrustStore(Path(d) / "home"), allow_untrusted=True)
            fe = self._run(rt, "vertex")
            self._assert_round_trip(fe, srv)
            body2 = srv.requests[1]["body"]
            self.assertIn("functionResponse", json.dumps(body2["contents"]), "tool result mapped to Gemini part")
            self.assertNotIn("additionalProperties", json.dumps(body2.get("tools")))


class TemperatureTests(unittest.TestCase):
    """`temperature` reaches each dialect's wire format, and is absent unless it is set.

    Absence matters as much as presence: the default is ``None`` so an existing install keeps
    whatever sampling its server already defaults to. Sending a value picked here would quietly
    change behaviour for everyone who never asked for it.

    0.0 is the value under test throughout because it is the one a naive ``if temperature:``
    drops, and it is also the one anybody pinning sampling actually wants.
    """

    def _run(self, rt: Runtime, provider: str) -> None:
        rt.provider_name = provider
        asyncio.run(AgentLoop(rt).run("hello"))

    def _vertex_runtime(self, directory: str, srv: FakeServer) -> Runtime:
        rt = make_runtime(Path(directory))
        os.environ["VERTEX_BASE_URL"], os.environ["GOOGLE_OAUTH_ACCESS_TOKEN"] = srv.url, "ya29.test"
        loader.load_plugin(ROOT / "examples/plugins/vertex-provider", rt,
                           loader.TrustStore(Path(directory) / "home"), allow_untrusted=True)
        return rt

    def test_the_runtime_takes_temperature_from_config(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ["PICOAGENT_HOME"] = str(Path(d) / "home")
            cfg = load_config(Path(d), {"temperature": 0.2})
            rt = Runtime(cfg, Path(d), Session(Path(d) / "s.jsonl", Path(d)))
            self.assertEqual(rt.temperature, 0.2)

    def test_the_default_leaves_temperature_unset(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ["PICOAGENT_HOME"] = str(Path(d) / "home")
            self.assertIsNone(load_config(Path(d), {})["temperature"])

    def test_openai_omits_temperature_when_it_is_unset(self):
        with tempfile.TemporaryDirectory() as d, FakeServer("openai") as srv:
            rt = make_runtime(Path(d))
            rt.providers.register(OpenAICompatProvider(base_url=srv.url + "/v1", api_key="k"))
            self._run(rt, "openai")
            self.assertNotIn("temperature", srv.requests[0]["body"])

    def test_openai_sends_a_zero_temperature(self):
        with tempfile.TemporaryDirectory() as d, FakeServer("openai") as srv:
            rt = make_runtime(Path(d))
            rt.temperature = 0.0
            rt.providers.register(OpenAICompatProvider(base_url=srv.url + "/v1", api_key="k"))
            self._run(rt, "openai")
            self.assertEqual(srv.requests[0]["body"]["temperature"], 0.0)

    def test_grok_sends_a_zero_temperature(self):
        """Grok is the built-in client under another name, so the knob must ride along."""
        with tempfile.TemporaryDirectory() as d, FakeServer("grok") as srv:
            rt = make_runtime(Path(d))
            rt.temperature = 0.0
            os.environ["XAI_BASE_URL"], os.environ["XAI_API_KEY"] = srv.url + "/v1", "xai-test"
            loader.load_plugin(ROOT / "examples/plugins/grok-provider", rt,
                               loader.TrustStore(Path(d) / "home"), allow_untrusted=True)
            self._run(rt, "grok")
            self.assertEqual(srv.requests[0]["body"]["temperature"], 0.0)

    def test_vertex_maps_temperature_into_generation_config(self):
        with tempfile.TemporaryDirectory() as d, FakeServer("vertex") as srv:
            rt = self._vertex_runtime(d, srv)
            rt.temperature = 0.0
            self._run(rt, "vertex")
            self.assertEqual(srv.requests[0]["body"]["generationConfig"]["temperature"], 0.0)

    def test_vertex_omits_temperature_when_it_is_unset(self):
        with tempfile.TemporaryDirectory() as d, FakeServer("vertex") as srv:
            rt = self._vertex_runtime(d, srv)
            self._run(rt, "vertex")
            self.assertNotIn("temperature", srv.requests[0]["body"]["generationConfig"])




class BaseUrlSchemeTests(unittest.TestCase):
    """`urlopen` speaks more than HTTP, so an unchecked base_url turns the client into a reader.

    `file:///etc/passwd` is the concrete case: `urllib.request.urlopen` resolves it against the
    local filesystem, so a base_url that reaches the request builder unchecked makes the model
    client open files instead of talking to a server. A repository cannot set `providers.base_url`
    (it is in `USER_ONLY`), but a plugin handed one from `[plugins.<name>]`, an environment
    variable and a typo all reach the same place.
    """

    def setUp(self):
        self.tmp = temp_dir()
        (self.tmp / "models").write_text('{"data": [{"id": "leaked-from-disk"}]}')
        (self.tmp / "chat").mkdir()
        (self.tmp / "chat" / "completions").write_text('data: {"choices":[{"delta":{"content":"hi"}}]}\n')
        self.file_base = "file://" + self.tmp.as_posix()

    def test_list_models_refuses_a_file_url_instead_of_reading_the_disk(self):
        """Matched on the refusal's own wording, not on the word ``file``.

        Every way this can go wrong says "file" somewhere: the refusal names the URL, and so does
        the ``No such file or directory`` a transport error would carry if the check were gone and
        the read merely missed. Only ``base_url must be http or https`` says the check ran.
        """
        provider = OpenAICompatProvider(base_url=self.file_base, api_key="k")
        with self.assertRaises(RuntimeError) as caught:
            asyncio.run(provider.list_models())
        self.assertIn("base_url must be http or https", str(caught.exception))
        self.assertNotIn("leaked-from-disk", str(caught.exception))

    def test_stream_refuses_a_file_url_as_an_error_event_not_an_exception(self):
        """Providers report expected failures as `StreamEvent("error")`; only bugs raise."""
        provider = OpenAICompatProvider(base_url=self.file_base, api_key="k")

        async def collect():
            return [event async for event in provider.stream(
                system="s", messages=[], tools=[], model="m", max_tokens=16, thinking="off")]

        events = asyncio.run(collect())
        self.assertEqual([event.type for event in events], ["error"])
        self.assertIn("base_url must be http or https", events[0].error)
        self.assertNotIn("hi", events[0].error)

    def test_the_refusal_names_the_url_so_the_user_can_find_the_setting(self):
        provider = OpenAICompatProvider(base_url=self.file_base, api_key="k")
        with self.assertRaises(RuntimeError) as caught:
            asyncio.run(provider.list_models())
        self.assertIn(self.file_base, str(caught.exception))

    def test_an_http_base_url_still_reaches_the_server(self):
        with FakeServer("openai") as srv:
            provider = OpenAICompatProvider(base_url=srv.url + "/v1", api_key="k")
            self.assertEqual(asyncio.run(provider.list_models()), ["fake-large", "fake-small"])

    def test_an_https_base_url_is_not_refused_for_its_scheme(self):
        """Nothing is listening, so this must fail as a transport error and not as a scheme one."""
        provider = OpenAICompatProvider(base_url="https://127.0.0.1:9/v1", api_key="k")
        with self.assertRaises(RuntimeError) as caught:
            asyncio.run(provider.list_models())
        self.assertNotIn("http or https", str(caught.exception))


class _RedirectHandler(BaseHTTPRequestHandler):
    """Records what it was sent, then either redirects or answers as a model server would."""

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self._handle()

    def _handle(self):
        self.server.received.append((self.path, dict(self.headers)))
        target = self.server.routes.get(self.path)
        if target:
            self.send_response(302)
            self.send_header("Location", target)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = b'{"data": [{"id": "beyond-the-redirect"}]}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        """Silence: the test's own output is the assertion, not the access log."""


class _RedirectServer:
    """A real HTTP server on a loopback port, so the redirect is followed by urllib itself."""

    def __init__(self, routes: dict[str, str] | None = None):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _RedirectHandler)
        self.httpd.routes = routes or {}
        self.httpd.received = []
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    @property
    def received(self) -> list:
        return self.httpd.received

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class RedirectTests(unittest.TestCase):
    """A credential must not leave the origin the user configured, and neither must the request.

    urllib's ``HTTPRedirectHandler`` re-sends every header that is not about the body to whatever
    ``Location`` names, so a gateway answering ``/models`` with a 302 to another host delivered
    ``Authorization: Bearer <key>`` there. ``requests`` and ``curl`` both strip the header on a
    cross-origin redirect; urllib does not. Dropping the header is not enough here either: the
    body of a ``/chat/completions`` request is the user's conversation, so the redirect is refused
    rather than followed without the key.
    """

    def setUp(self):
        self.elsewhere = _RedirectServer()
        self.gateway = _RedirectServer(routes={
            "/v1/models": self.elsewhere.url + "/v1/models",
            "/v1/chat/completions": self.elsewhere.url + "/v1/chat/completions"})
        self.addCleanup(self.gateway.close)
        self.addCleanup(self.elsewhere.close)
        self.key = "sk-SECRET-KEY-12345"

    def _provider(self, base: str) -> OpenAICompatProvider:
        return OpenAICompatProvider(base_url=base + "/v1", api_key=self.key)

    def _stream(self, provider):
        async def collect():
            return [event async for event in provider.stream(
                system="s", messages=[], tools=[], model="m", max_tokens=16, thinking="off")]
        return asyncio.run(collect())

    def _headers_seen_elsewhere(self) -> list[str]:
        return [headers.get("Authorization", "") for _, headers in self.elsewhere.received]

    def test_a_cross_origin_redirect_never_delivers_the_key(self):
        with self.assertRaises(RuntimeError):
            asyncio.run(self._provider(self.gateway.url).list_models())
        self.assertNotIn(f"Bearer {self.key}", self._headers_seen_elsewhere())

    def test_a_cross_origin_redirect_is_refused_rather_than_followed(self):
        """The request body is the conversation, so the other host gets no request at all."""
        with self.assertRaises(RuntimeError):
            asyncio.run(self._provider(self.gateway.url).list_models())
        self.assertEqual(self.elsewhere.received, [])

    def test_the_refusal_names_where_it_would_have_gone(self):
        """The other half of the pair above: this one pins the *cross-origin* wording.

        The target here is ``http:``, so the scheme branch cannot fire and only one refusal is
        reachable - but the branch is named in the assertion anyway, so that the two tests fail
        for different reasons rather than both resting on a URL that every refusal echoes.
        """
        with self.assertRaises(RuntimeError) as caught:
            asyncio.run(self._provider(self.gateway.url).list_models())
        message = str(caught.exception)
        self.assertIn(self.elsewhere.url.split("//")[1], message)
        self.assertIn("a host you did not configure", message)
        self.assertNotIn("must be http or https", message)
        self.assertNotIn(self.key, message)

    def test_stream_reports_it_as_an_error_event_not_an_exception(self):
        events = self._stream(self._provider(self.gateway.url))
        self.assertEqual([event.type for event in events], ["error"])
        self.assertEqual(self.elsewhere.received, [])

    def test_a_redirect_to_another_scheme_is_refused(self):
        """``urllib`` follows a redirect to ``ftp:`` happily; the scheme check has to cover the
        URL actually fetched, not only the one the user configured.

        Asserted on the wording only the *scheme* branch produces, because an ``ftp:`` target is
        refused twice over: it fails the scheme check, and it would fail the cross-origin check
        below it too, since an origin carries its scheme and the configured one is always http or
        https. Both refusals interpolate the URL, so matching on ``"ftp"`` - which is what this
        test used to do - passes whichever branch fired, and stays green with the scheme check
        deleted. The test names a specific control, so it has to fail when that control goes.
        """
        gateway = _RedirectServer(routes={"/v1/models": "ftp://127.0.0.1:9/models"})
        self.addCleanup(gateway.close)
        with self.assertRaises(RuntimeError) as caught:
            asyncio.run(self._provider(gateway.url).list_models())
        message = str(caught.exception)
        self.assertIn("a model endpoint must be http or https", message)
        self.assertIn("its scheme is 'ftp'", message)
        self.assertNotIn("a host you did not configure", message)

    def test_a_same_origin_redirect_is_still_followed_with_the_key(self):
        """The cost of the rule has to stay on the case that matters: a gateway moving a path
        within its own origin is ordinary, and refusing it would break working setups."""
        server = _RedirectServer(routes={"/v1/models": "/v2/models"})
        self.addCleanup(server.close)
        self.assertEqual(asyncio.run(self._provider(server.url).list_models()), ["beyond-the-redirect"])
        followed = [headers.get("Authorization") for path, headers in server.received
                    if path == "/v2/models"]
        self.assertEqual(followed, [f"Bearer {self.key}"])


class _OpenResponse:
    """Stands in for the live 302 ``urllib`` hands to ``redirect_request``."""

    def __init__(self, closing_raises: Exception | None = None):
        self.closed, self._closing_raises = False, closing_raises

    def close(self) -> None:
        self.closed = True
        if self._closing_raises is not None:
            raise self._closing_raises


class RefusedRedirectClosesTheResponse(unittest.TestCase):
    """A refusal returns nothing to ``urllib``, and ``urllib`` reads and closes the 302 only on
    the path that returns. So the response the refusal arrived on is the refuser's to close:
    left to the collector it surfaces as ``ResourceWarning: unclosed <socket.socket ...>`` in
    whatever unrelated test is running when the collection falls due.
    """

    def _refuse(self, response: _OpenResponse, newurl: str = "https://elsewhere.example/v1/models"):
        request = urllib.request.Request("https://gateway.example/v1/models")
        return _SameOriginRedirects().redirect_request(
            request, response, 302, "Found", {}, newurl)

    def test_the_response_a_refusal_arrived_on_is_closed(self):
        response = _OpenResponse()
        with self.assertRaises(RedirectRefused):
            self._refuse(response)
        self.assertTrue(response.closed)

    def test_a_close_that_fails_does_not_speak_in_place_of_the_refusal(self):
        """The refusal is the security answer; an I/O error from the socket on the way down
        would replace it with something the caller reports as a transport failure instead."""
        response = _OpenResponse(closing_raises=OSError("connection already reset"))
        with self.assertRaises(RedirectRefused):
            self._refuse(response)

    def test_a_followable_redirect_leaves_the_response_open(self):
        """``urllib`` reads and closes it itself once this returns, and hands it to an
        ``HTTPError`` on the method check inside the base class - closing early breaks both."""
        response = _OpenResponse()
        self.assertIsNotNone(self._refuse(response, "https://gateway.example/v2/models"))
        self.assertFalse(response.closed)


class InterruptedToolBatchTests(unittest.TestCase):
    """An assistant message whose tool calls were never answered must not reach the wire that way.

    A Ctrl-C between the assistant message and its results (the tool batch can run for minutes)
    leaves the log ending on ``tool_calls`` with no ``role: tool`` after it. OpenAI and the strict
    compatible servers reject that shape with a 400 on *every* later turn, so a resumed session is
    wedged for good and the user only sees an opaque provider error.
    """

    def _mapped(self, messages):
        return to_openai_messages("sys", messages)

    def test_an_unanswered_call_gets_an_answer_before_the_request_is_sent(self):
        messages = [Message(role="user", text="run it"),
                    Message(role="assistant", tool_calls=[ToolCall("c1", "shell", {"cmd": "sleep 600"})])]
        answered = [entry for entry in self._mapped(messages) if entry["role"] == "tool"]
        self.assertEqual([entry["tool_call_id"] for entry in answered], ["c1"])

    def test_the_answer_claims_neither_success_nor_failure(self):
        """The one thing known about an interrupted call is that its outcome is not known."""
        messages = [Message(role="assistant", tool_calls=[ToolCall("c1", "shell", {})])]
        content = [e for e in self._mapped(messages) if e["role"] == "tool"][0]["content"]
        self.assertIn("unknown", content.lower())
        self.assertNotIn("failed", content.lower())
        self.assertNotIn("succeeded", content.lower())

    def test_a_batch_that_finished_is_left_exactly_as_it_was(self):
        messages = [Message(role="assistant", tool_calls=[ToolCall("c1", "read", {})]),
                    Message(role="tool", tool_results=[ToolResult("c1", "file contents")])]
        answered = [e for e in self._mapped(messages) if e["role"] == "tool"]
        self.assertEqual([(e["tool_call_id"], e["content"]) for e in answered], [("c1", "file contents")])

    def test_only_the_calls_nobody_answered_are_answered_here(self):
        """A half-answered batch is a plugin's history rewrite, not something the loop writes.
        Every id has to be answered exactly once; which of the two entries comes first is the
        server's business, since it matches them by ``tool_call_id``."""
        calls = [ToolCall("c1", "read", {}), ToolCall("c2", "read", {})]
        messages = [Message(role="assistant", tool_calls=calls),
                    Message(role="tool", tool_results=[ToolResult("c1", "first")])]
        answered = [e for e in self._mapped(messages) if e["role"] == "tool"]
        self.assertEqual(sorted(e["tool_call_id"] for e in answered), ["c1", "c2"])
        self.assertEqual([e["content"] for e in answered if e["tool_call_id"] == "c1"], ["first"])

    def test_one_id_is_answered_once_however_often_the_model_repeated_it(self):
        """Two calls sharing an id is already a malformed assistant message, from a model or from
        a plugin that rewrote the batch. Answering each of them separately turns that into the
        duplicate-``tool_call_id`` 400 - the same class of refusal this repair exists to avoid."""
        calls = [ToolCall("dup", "read", {}), ToolCall("dup", "read", {})]
        answered = [e for e in self._mapped([Message(role="assistant", tool_calls=calls)])
                    if e["role"] == "tool"]
        self.assertEqual([e["tool_call_id"] for e in answered], ["dup"])

    def test_the_answer_sits_between_the_call_and_whatever_the_user_typed_next(self):
        """On ``-r`` the next entry is the new prompt, and a tool message after it is the same 400."""
        messages = [Message(role="assistant", tool_calls=[ToolCall("c1", "shell", {})]),
                    Message(role="user", text="what happened?")]
        roles = [entry["role"] for entry in self._mapped(messages)]
        self.assertEqual(roles, ["system", "assistant", "tool", "user"])

    def test_a_resumed_interrupted_session_answers_every_call_it_replays(self):
        """End to end: the log an interrupt leaves behind, read back the way ``-r`` reads it."""
        tmp = temp_dir()
        session = Session(tmp / "s.jsonl", tmp)
        session.append_message(Message(role="user", text="run it"))
        session.append_message(Message(role="assistant", tool_calls=[ToolCall("c1", "shell", {})]))
        before = (tmp / "s.jsonl").read_text()

        resumed = Session(tmp / "s.jsonl", tmp, resume=True)
        resumed.append_message(Message(role="user", text="are you there?"))
        mapped = to_openai_messages("sys", resumed.messages())

        called = [call["id"] for entry in mapped if entry["role"] == "assistant"
                  for call in entry.get("tool_calls", [])]
        self.assertEqual([entry["tool_call_id"] for entry in mapped if entry["role"] == "tool"], called)
        self.assertTrue((tmp / "s.jsonl").read_text().startswith(before),
                        "the repair is a rendering decision; the log keeps what actually happened")


if __name__ == "__main__":
    unittest.main()
