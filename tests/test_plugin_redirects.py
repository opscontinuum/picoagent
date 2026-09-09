"""The vertex provider sends a credential to a URL a user configured. A redirect must not move it.

``urllib`` re-sends every header that is not about the body to whatever ``Location`` names, so a
server answering with a 302 to another host is handed the ``Authorization`` header in full -
demonstrated here against a second local server that records what it received. ``requests`` and
``curl`` strip the header on a cross-origin redirect; ``urllib`` does not, and it will follow a
redirect to ``ftp:`` as well.

The vertex provider carries an OAuth bearer token minted from the user's ``gcloud`` login, so it
is exactly the case core's ``_SameOriginRedirects`` exists for: the redirect is refused rather
than followed with the header dropped, because the request body - a conversation - is worth
stealing on its own. ``test_providers.RedirectTests`` holds the same property for core's own
provider; this file holds it for the shipped provider plugin that opens its own connections.
es-doctor holds it for its Elasticsearch client in its own repository
(``opscontinuum/es-doctor``, ``tests/test_redirects.py``).

The redirect is done by a real ``http.server`` on loopback rather than a stubbed opener: what is
under test is what ``urllib`` does with a ``Location`` header, so a fake that answers instead of
redirecting would prove nothing about it.
"""
import asyncio
import importlib.util
import json
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from helpers import ROOT

_spec = importlib.util.spec_from_file_location(
    "vertex_provider", ROOT / "examples/plugins/vertex-provider/vertex_provider.py")
vertex = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vertex)

#: Prefix the second request carries, so a server told to redirect to itself redirects once.
MOVED = "/moved"


class _RedirectHandler(BaseHTTPRequestHandler):
    """Records every request, then redirects it once or answers it."""

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self._handle()

    def _handle(self):
        self.server.received.append((self.path, dict(self.headers)))
        if self.server.redirect_to and not self.path.startswith(MOVED):
            self.send_response(302)
            self.send_header("Location", self.server.redirect_to + MOVED + self.path)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = json.dumps({"status": "green"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        """Silence: the assertions are the test's output, not the access log."""


class _Server:
    """A real HTTP server on a loopback port, so ``urllib`` itself follows the redirect."""

    def __init__(self, redirect_to: str | None = None):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _RedirectHandler)
        self.httpd.redirect_to = redirect_to
        self.httpd.received = []
        # Poll far more often than the 0.5s default: ``shutdown`` blocks until the loop notices,
        # and every test here builds two servers, so the default would cost the suite seconds.
        threading.Thread(target=self.httpd.serve_forever, args=(0.01,), daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    @property
    def received(self) -> list:
        return self.httpd.received

    def redirect_to_self(self) -> None:
        self.httpd.redirect_to = self.url

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def authorization_seen(self) -> list[str]:
        return [headers.get("Authorization", "") for _, headers in self.received]


class _RedirectCase(unittest.TestCase):
    """A gateway that redirects everything to ``elsewhere``, and the two servers to assert on."""

    def setUp(self):
        self.elsewhere = _Server()
        self.gateway = _Server(redirect_to=self.elsewhere.url)
        self.addCleanup(self.gateway.close)
        self.addCleanup(self.elsewhere.close)


class VertexRedirectTests(_RedirectCase):
    TOKEN = "ya29.SECRET-ACCESS-TOKEN"

    def _stream(self, base_url: str) -> list:
        provider = vertex.VertexProvider(project="p", location="us-central1", base_url=base_url,
                                         token=self.TOKEN)

        async def collect():
            return [event async for event in provider.stream(
                system="s", messages=[], tools=[], model="gemini-2.5-pro", max_tokens=16,
                thinking="off")]
        return asyncio.run(collect())

    def test_a_cross_origin_redirect_never_delivers_the_access_token(self):
        self._stream(self.gateway.url)
        self.assertNotIn(f"Bearer {self.TOKEN}", self.elsewhere.authorization_seen())

    def test_a_cross_origin_redirect_is_refused_rather_than_followed(self):
        """The body is the conversation, so the other host gets no request at all, not a stripped one."""
        self._stream(self.gateway.url)
        self.assertEqual(self.elsewhere.received, [])

    def test_the_refusal_reaches_the_session_as_an_error_event(self):
        events = self._stream(self.gateway.url)
        self.assertEqual([event.type for event in events], ["error"])
        self.assertIn("redirect", events[0].error)
        self.assertNotIn(self.TOKEN, events[0].error)

    def test_the_reader_thread_survives_a_session_that_has_already_gone(self):
        """The refusal makes ``stream`` return on its first event, which closes the loop under the
        thread still holding the connection. Handing an item to a closed loop raises, and a daemon
        thread that raises prints a traceback for an outcome the session already reported and
        handled - so the thread has to notice the consumer left rather than die at it.
        """
        loop = asyncio.new_event_loop()
        loop.close()
        request = urllib.request.Request(self.gateway.url + "/v1/models", method="POST", data=b"{}")
        vertex.VertexProvider._read_sse(request, asyncio.Queue(), loop)

    def test_a_same_origin_redirect_is_still_followed_with_the_token(self):
        """A gateway moving a path inside its own origin is ordinary; refusing it would cost setups
        that work today, so the rule has to stay on the case where the host changes."""
        self.gateway.redirect_to_self()
        events = self._stream(self.gateway.url)
        self.assertEqual([event.type for event in events], ["done"])
        followed = [headers.get("Authorization") for path, headers in self.gateway.received
                    if path.startswith(MOVED)]
        self.assertEqual(followed, [f"Bearer {self.TOKEN}"])


if __name__ == "__main__":
    unittest.main()
