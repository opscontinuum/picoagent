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
import json
import logging
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from picoagent.core.tools import truncate
from picoagent.core.types import ToolResult

log = logging.getLogger("es_doctor")

DEFAULT_LOGS_INDEX = "logs-*,filebeat-*"
DEFAULT_METRICS_INDEX = "metrics-*,metricbeat-*"
DEFAULT_TRACES_INDEX = "traces-apm*,apm-*"


class ESError(Exception):
    """Anything the cluster refused or the network swallowed. Tools turn it into a result."""


class ESClient:
    """Minimal REST client. Raises ``ESError`` with the server's message on non-2xx."""

    def __init__(self, url: str, api_key: str = "", username: str = "", password: str = "",
                 verify_tls: bool = True, ca_cert: str = ""):
        """``ca_cert`` is the secure answer to a self-signed cluster: trust that CA rather than
        nobody. ``verify_tls=False`` remains as a last resort, but it disables certificate *and*
        hostname checking, which makes the connection interceptable by anything on the path -
        so it is the wrong tool for the common case it tends to get used for.
        """
        self.url = url.rstrip("/")
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

    def request(self, method: str, path: str, body: dict | None = None, raw: bool = False) -> Any:
        """``raw=True`` returns the decoded body unparsed - ``_nodes/hot_threads`` answers plain
        text, not JSON, and ``json.loads`` on it would raise where the caller wants the text."""
        req = urllib.request.Request(self.url + (path if path.startswith("/") else "/" + path), method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json", **({"Authorization": self._auth} if self._auth else {})})
        try:
            with urllib.request.urlopen(req, timeout=60, context=self._ctx) as resp:
                payload = resp.read()
                return payload.decode(errors="replace") if raw else json.loads(payload or b"null")
        except urllib.error.HTTPError as exc:
            raise ESError(f"HTTP {exc.code} {method} {path}: {exc.read().decode(errors='replace')[:800]}") from exc
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
    """Base: holds the client and turns ESError into an error ToolResult."""

    def __init__(self, es: ESClient, settings: Settings):
        self.es, self.settings = es, settings

    async def execute(self, args: dict, ctx) -> ToolResult:
        try:
            return self.run(args, ctx)
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
