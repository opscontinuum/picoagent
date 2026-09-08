"""The HTTP client and the handful of pieces both es-doctor tool modules share.

Why this is its own module, and not the bottom of ``es_doctor.py``: the plugin loader
imports the *entry* module under a mangled name (``picoagent_plugin_es-doctor``) and builds
a fresh module object on every load. A sibling that did ``from es_doctor import ESError``
would therefore get a second, unrelated ``ESError`` class - and ``except ESError`` would
quietly stop catching the errors the client actually raises, turning every expected failure
into a crash. A module both sides import by its plain name is imported once and keeps one
identity, which is what an exception class needs.

So: ``ESClient``/``ESError``/``Settings``/``_ESTool``/``result``/``text_table`` live here,
``es_doctor.py`` keeps the data tools and ``register()``, ``es_admin.py`` the cluster ones.
"""
from __future__ import annotations

import base64
import inspect
import json
import logging
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from picoagent.core.provider import RedirectRefused, _SameOriginRedirects
from picoagent.core.tools import truncate
from picoagent.core.types import ToolResult

log = logging.getLogger("es_doctor")

DEFAULT_LOGS_INDEX = "logs-*,filebeat-*"
DEFAULT_METRICS_INDEX = "metrics-*,metricbeat-*"
DEFAULT_TRACES_INDEX = "traces-apm*,apm-*"

#: The methods that cannot change anything, whatever endpoint they are aimed at.
#:
#: This gate used to be a denylist: ``DELETE`` plus seven path substrings. Elasticsearch has
#: hundreds of mutating endpoints, so everything nobody had thought to enumerate went through -
#: ``PUT /_cluster/settings`` to stop allocation cluster-wide, ``POST /<index>/_bulk`` carrying
#: delete actions, ``PUT /_scripts/<id>`` to store a script, ``POST /_aliases`` to change what
#: every read resolves to, ``_restore`` over live indices, and ``POST /<index>/_doc`` to write
#: forged evidence into the logs an assessor is reading. A list of what is *permitted* cannot
#: fail that way: an endpoint nobody has thought about is refused rather than allowed.
READ_ONLY_METHODS = frozenset({"GET", "HEAD"})

#: The POST endpoints that only read. Elasticsearch takes a query in a request body, and a body
#: on a GET does not survive every proxy and client in front of a cluster, so these are POSTs
#: with no way around it - refusing every POST would refuse searching, which is the plugin's job.
#:
#: Each entry matches a whole path, not a substring, and the list is deliberately short: it is
#: what this plugin's own tools call plus ``_count``, the one obvious sibling of ``_search``.
#: Anything else a person genuinely wants is what ``allow_destructive`` is for.
READ_ONLY_POST_PATHS = (
    re.compile(r"(?:[^/]+/)?_search"),                    # es_search, es_logs, es_metrics, es_correlate
    re.compile(r"(?:[^/]+/)?_count"),                     # the same query shape, counting instead
    re.compile(r"_cluster/allocation/explain"),           # es_shards explain=true; the body names the shard
    re.compile(r"_index_template/_simulate_index/[^/]+"), # es_templates: which template an index would win
)

#: Path spellings that are refused before the allowlist is consulted: a percent-encoded slash and
#: a parent reference are both ways to write one endpoint and have the server read another, and
#: an allowlist that matches on the spelling is only as good as the two agreeing on it.
_PATH_TRICKS = re.compile(r"%2f|\.\.", re.I)


def is_destructive(method: str, path: str) -> bool:
    """True unless ``method`` cannot change anything, or ``path`` is a read that needs a body."""
    if method.upper() in READ_ONLY_METHODS:
        return False
    if method.upper() != "POST":
        return True
    endpoint = path.split("?")[0].split("#")[0].strip("/")
    if _PATH_TRICKS.search(endpoint):
        return True
    return not any(pattern.fullmatch(endpoint) for pattern in READ_ONLY_POST_PATHS)


def destructive_refusal(method: str, path: str) -> str:
    """Why the call was refused, and the one setting that permits it. Read by the model and the user."""
    return (f"refusing {method.upper()} {path}: es-doctor makes read-only calls only - GET, HEAD, "
            "and the few POST endpoints that read (_search, _count, allocation explain, index "
            "template simulation). This is a destructive Elasticsearch call; set "
            "allow_destructive = true in [plugins.es-doctor] to permit it.")


class ESError(Exception):
    """Anything the cluster refused or the network swallowed. Tools turn it into a result."""


class ESClient:
    """Minimal REST client. Raises ``ESError`` with the server's message on non-2xx."""

    def __init__(self, url: str, api_key: str = "", username: str = "", password: str = "",
                 verify_tls: bool = True, ca_cert: str = "", allow_destructive: bool = False):
        """``ca_cert`` is the secure answer to a self-signed cluster: trust that CA rather than
        nobody. ``verify_tls=False`` remains as a last resort, but it disables certificate *and*
        hostname checking, which makes the connection interceptable by anything on the path -
        so it is the wrong tool for the common case it tends to get used for.

        ``allow_destructive`` lives on the client, not only on the tool that takes a method and a
        path from the model, because every module in this plugin reaches the cluster through
        :meth:`request`. A gate on one tool's arguments protects that tool; a gate here protects
        the next tool somebody writes, including one that builds a path out of a log document.
        """
        self.url = url.rstrip("/")
        self.allow_destructive = allow_destructive
        self._auth = (f"ApiKey {api_key}" if api_key
                      else "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode() if username else "")
        # One context, built secure, then weakened only on the explicit opt-out. Written this
        # way so the protocol floor is set on every path - including the insecure one, where
        # giving up certificate checking is no reason to also accept TLS 1.0.
        self._ctx = ssl.create_default_context(cafile=ca_cert) if ca_cert else ssl.create_default_context()
        self._ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        if not ca_cert and not verify_tls:
            log.warning("es-doctor: TLS verification is OFF for %s. Anything on the network path "
                        "can read and alter this traffic, including credentials. Prefer ca_cert.",
                        self.url)
            # Public API rather than ssl._create_unverified_context(): same result, and this
            # spells out exactly which two checks are being given up.
            self._ctx.check_hostname = False
            self._ctx.verify_mode = ssl.CERT_NONE
        # Every request goes through this opener, so the redirect rule cannot be lost by forgetting
        # it at one call site. urllib re-sends the ``Authorization`` header to whatever a
        # ``Location`` names, so a 302 - from a proxy in front of the cluster, or from anything that
        # can answer for it - would deliver the API key or the Basic password, and the query in the
        # body, to a host the user never configured. Core's ``_SameOriginRedirects`` refuses that
        # redirect rather than following it without the header; a stripped-header request still
        # arrives, and what the assessor is searching for is itself worth reading.
        #
        # Built here rather than reusing core's ``_OPENER`` because ``opener.open()`` takes no
        # ``context=``: the TLS context above is this client's own, so the opener has to carry it.
        # Importing a name core spells with a leading underscore is deliberate - a copied security
        # control drifts silently, while a rename in core fails this plugin's import loudly, which
        # is the failure a human notices. See the same note in the vertex-provider plugin.
        self._opener = urllib.request.build_opener(_SameOriginRedirects,
                                                   urllib.request.HTTPSHandler(context=self._ctx))

    def request(self, method: str, path: str, body: dict | None = None, raw: bool = False) -> Any:
        """``raw=True`` returns the decoded body unparsed - ``_nodes/hot_threads`` answers plain
        text, not JSON, and ``json.loads`` on it would raise where the caller wants the text.

        Anything that is not a read is refused here unless the user set ``allow_destructive``. The
        refusal is an ``ESError`` like any other expected failure, so it reaches the model as a
        tool result naming the setting rather than unwinding out of a tool.
        """
        if is_destructive(method, path) and not self.allow_destructive:
            raise ESError(destructive_refusal(method, path))
        return self._send(method, path, body, raw)

    def request_after_confirmation(self, method: str, path: str, body: dict | None = None) -> Any:
        """The gate's one bypass: a write the user was shown in full and agreed to.

        Two tools ask before they write - ``es_slowlog`` for three named threshold keys, and
        ``es_snapshots`` for the test blob repository verification puts on every node. A person
        who has just read the exact change and said yes is a stronger authority than a config
        key, so refusing them because ``allow_destructive`` is unset would refuse the write they
        just approved.

        A separate method rather than a flag on :meth:`request`: this is the whole bypass, and
        grepping for its name lists every call that is allowed to take it. Which is only true
        while every caller meets the contract, so a write nobody was shown does not come here
        even when something else authorises it - ``es_slowlog`` running headless under
        ``allow_destructive`` calls :meth:`request`, because that is what happened.
        """
        return self._send(method, path, body)

    def _send(self, method: str, path: str, body: dict | None = None, raw: bool = False) -> Any:
        """The HTTP itself. Everything above decides whether the call may be made at all."""
        req = urllib.request.Request(self.url + (path if path.startswith("/") else "/" + path), method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json", **({"Authorization": self._auth} if self._auth else {})})
        try:
            with self._opener.open(req, timeout=60) as resp:
                payload = resp.read()
                if raw:
                    return payload.decode(errors="replace")
                try:
                    return json.loads(payload or b"null")
                except json.JSONDecodeError as exc:
                    # ``es_request`` takes its path from the model, and several endpoints answer
                    # plain text: every /_cat/* without format=json, and _nodes/hot_threads. That
                    # is an expected failure, so it becomes an ESError like any other and reaches
                    # the model as a result naming the two ways out. Left unhandled it was a
                    # JSONDecodeError raised out of the tool, which is the one thing the "expected
                    # failures are values" rule exists to prevent.
                    raise ESError(f"{method} {path} did not answer JSON ({exc}). Elasticsearch "
                                  "answers plain text for /_cat/* without format=json and for "
                                  "_nodes/hot_threads: add format=json to a cat call, or use "
                                  "es_hot_threads, which reads the text form.") from exc
        except urllib.error.HTTPError as exc:
            raise ESError(f"HTTP {exc.code} {method} {path}: {exc.read().decode(errors='replace')[:800]}") from exc
        except RedirectRefused as exc:
            # A refused redirect is an expected failure like any other: the tools turn ESError into
            # a result, and anything else would raise out of a tool's ``run`` instead of reporting.
            raise ESError(str(exc)) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise ESError(f"cannot reach {self.url}: {exc}") from exc

    def search(self, index: str, body: dict) -> dict:
        return self.request("POST", f"/{urllib.parse.quote(index, safe='*,-.')}/_search", body)


@dataclass
class Settings:
    """The plugin's configuration, as the tools need it."""
    logs_index: str = DEFAULT_LOGS_INDEX
    metrics_index: str = DEFAULT_METRICS_INDEX
    traces_index: str = DEFAULT_TRACES_INDEX
    allow_destructive: bool = False


class _ESTool:
    """Base for every tool in this plugin: holds the client, turns ESError into an error result.

    ``run`` may be written ``def`` or ``async def``. Most of these tools are one HTTP call and
    a render, which is synchronous; two of the administration tools stop to ask the user
    through ``ctx.ui.ask`` and so must be awaited. One base that awaits an awaitable covers
    both, and keeps the ``ESError`` conversion in a single place - a second base for the async
    half would have to repeat the ``except`` clause that is the whole reason this class exists.
    """

    def __init__(self, es: ESClient, settings: Settings):
        self.es, self.settings = es, settings

    async def execute(self, args: dict, ctx) -> ToolResult:
        try:
            outcome = self.run(args, ctx)
            return await outcome if inspect.isawaitable(outcome) else outcome
        except ESError as exc:
            return ToolResult(ctx.tool_call_id, str(exc), is_error=True)

    def run(self, args: dict, ctx) -> ToolResult:  # pragma: no cover - overridden
        raise NotImplementedError


def result(ctx, text: str, is_error: bool = False, **details) -> ToolResult:
    """Every tool's last line: truncate to the session's limits, say so when it cut."""
    body, cut = truncate(text, ctx.config["tool_output_max_bytes"], ctx.config["tool_output_max_lines"])
    return ToolResult(ctx.tool_call_id, body + ("\n[truncated]" if cut else ""), is_error=is_error, details=details)


def text_table(header: list[str], rows: list[list]) -> str:
    """Fixed-width table; columns as wide as their widest cell, header included."""
    widths = [max(len(str(x)) for x in col) for col in zip(header, *rows)]
    line = lambda cells: "  ".join(str(c).ljust(w) for c, w in zip(cells, widths))  # noqa: E731
    return "\n".join([line(header), line(["-" * w for w in widths])] + [line(r) for r in rows])
