"""Runs the full agent loop (prompt -> tool call -> tool exec -> second turn) against a fake server
for each provider dialect. Standard library only:  python -m unittest discover -s tests -v"""
from __future__ import annotations
import asyncio, json, os, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from picoagent.core.config import load_config                       # noqa: E402
from picoagent.core.loop import AgentLoop, Runtime                  # noqa: E402
from picoagent.core.session import Session                          # noqa: E402
from picoagent.core.tools import BUILTIN_TOOLS                      # noqa: E402
from picoagent.core.provider import OpenAICompatProvider            # noqa: E402
from picoagent.plugins import loader                                # noqa: E402
from picoagent import cli                                           # noqa: E402
from picoagent.testing.fakes import FakeServer  # noqa: E402
from helpers import ROOT  # noqa: E402,F811


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


if __name__ == "__main__":
    unittest.main()
