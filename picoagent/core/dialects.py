"""One provider per ``[providers.<name>]`` table, built from the dialect that table names.

A **dialect** is code: a wire format, a mapping from picoagent's neutral messages to somebody's
request body, and the streaming rules that come back. An **endpoint** is a URL. picoagent ships
exactly two dialects - :class:`~picoagent.core.provider.OpenAICompatProvider` and
:class:`~picoagent.core.vertex.VertexProvider` - and everything else that used to look like a
"provider" turns out to be a name plus a URL, which is configuration::

    [providers.grok]                        # no dialect key -> OpenAI-compatible
    base_url = "https://api.x.ai/v1"
    api_key  = "xai-..."

    [providers.local]                       # the same dialect, a local server
    base_url = "http://localhost:11434/v1"

    [providers.milgemini]                   # the other dialect, a different host
    dialect  = "vertex"
    base_url = "https://genai.example"
    project  = "..."
    location = "..."

That is three usable providers and no plugin installed. The plugin that used to be needed for
the first of them was thirty-four lines of which six did work: the built-in client renamed and
pointed at another URL. Registering a name against a URL is not a thing anybody should have to
write, review, trust and install a module for.

What a plugin is still for is a third *dialect* - Anthropic's messages API, Bedrock - which is
real code with real mapping decisions in it. That seam is unchanged: a plugin registering a
provider under a name replaces whatever a config table put there, because the registry documents
that later registration wins and plugins load after core.

Two rules this module holds to:

* **An unknown dialect is refused, never guessed.** Falling back to the OpenAI client for
  ``dialect = "gemini"`` would speak the wrong wire format to an endpoint the user deliberately
  configured, which fails as a 400 naming a request field rather than as anything about the typo
  - and against a *proxy* that accepts both, it would silently work with the wrong mapping. So a
  table naming a dialect that does not exist registers nothing, and says so, naming the ones that
  do.
* **Nothing here reads a repository's config.** ``("providers",)`` is in
  :data:`~picoagent.core.config.USER_ONLY`, so the tables walked below have already had a
  repository's opinions stripped out of them before the merge that produced ``cfg``.
"""
from __future__ import annotations

import os
from typing import Any, Callable

from .provider import OpenAICompatProvider, Provider
from .text import safe_for_display
from .vertex import VertexProvider

#: The dialect a ``[providers.<name>]`` table gets when it names none. It is the format most
#: endpoints speak, and the one a first run against OpenAI, Ollama or a gateway needs, so the
#: zero-config path stays zero-config: ``DEFAULTS`` carries ``providers = {"openai": {}}`` and
#: that empty table is a complete description of the built-in client.
DEFAULT_DIALECT = "openai"

#: The key in a provider's table that chooses its wire format.
DIALECT_KEY = "dialect"


def _openai(name: str, table: dict) -> Provider:
    """The OpenAI-compatible client for one table.

    ``None`` rather than ``""`` for a missing value, deliberately: the client resolves its own
    environment fallbacks (``PICOAGENT_BASE_URL``, ``OPENAI_API_KEY``) when it is handed nothing,
    and an empty string would be an answer that suppresses them.
    """
    return OpenAICompatProvider(name=name, base_url=table.get("base_url"),
                                api_key=table.get("api_key"), extra_headers=table.get("headers"))


def _vertex(name: str, table: dict) -> Provider:
    """The Gemini/Vertex client for one table.

    The environment fallbacks are Google's own names, so an install already exporting
    ``GOOGLE_CLOUD_PROJECT`` for ``gcloud`` needs nothing in config.toml but the dialect. ``token``
    is accepted and not asked for: see :mod:`picoagent.core.vertex` on why an hour-lived OAuth
    token is not a thing to write into a config file.
    """
    return VertexProvider(
        name=name,
        project=table.get("project") or os.environ.get("GOOGLE_CLOUD_PROJECT", "my-project"),
        location=table.get("location") or os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1"),
        base_url=table.get("base_url") or os.environ.get("VERTEX_BASE_URL"),
        token=table.get("token"))


#: Every dialect core knows how to build, by the value that names it in a config table. A plugin
#: does not add to this: it registers a finished provider object, which is the wider seam and the
#: one that does not require core to know how the plugin's client is constructed.
DIALECTS: dict[str, Callable[[str, dict], Provider]] = {"openai": _openai, "vertex": _vertex}


class UnknownDialect(Exception):
    """A ``[providers.<name>]`` table naming a wire format picoagent does not have."""


def refusal_for(name: str, dialect: Any) -> str:
    """Why a table cannot be built, worded for whoever is looking at their config file.

    It names the provider, because a config with four tables has to say which one; the value as
    written, because the usual cause is a spelling (``gemini`` for ``vertex``); and every dialect
    that does exist, because the next thing the reader needs is what to put there instead. It
    does not suggest a plugin, since a plugin does not add a dialect *value* - it registers a
    provider object, and the name it registers under is the table's name with no ``dialect`` key
    involved at all.
    """
    known = ", ".join(sorted(DIALECTS))
    return (f"refusing to build the provider '{name}': its config table sets dialect = "
            f"{safe_for_display(str(dialect))!r}, which is not a wire format picoagent speaks. "
            f"The dialects are: {known}. Leave dialect out for an OpenAI-compatible endpoint. "
            f"Nothing is registered under '{name}' this session, because guessing a wire format "
            f"for an endpoint you configured on purpose is worse than not having it.")


def build_provider(name: str, table: dict) -> Provider:
    """The provider one ``[providers.<name>]`` table describes.

    Raises :class:`UnknownDialect` for a dialect that does not exist. A raise rather than a
    returned ``None``, because there is exactly one honest thing to do with the result and a
    caller that forgot to check would otherwise register nothing and say nothing.
    """
    dialect = table.get(DIALECT_KEY, DEFAULT_DIALECT)
    build = DIALECTS.get(dialect) if isinstance(dialect, str) else None
    if build is None:
        raise UnknownDialect(refusal_for(name, dialect))
    return build(name, table)


def providers_from_config(cfg: dict) -> tuple[list[Provider], list[str]]:
    """Every provider the config describes, and the refusals for the tables that describe none.

    Both halves are returned rather than the failures being logged from here, because the caller
    is the one that knows where a startup notice goes - stderr before a frontend exists, a
    ``notice`` event once one does - and a library embedder may want neither.

    A ``providers`` value that is not a table of tables is skipped rather than refused. It is not
    a wire-format mistake, so it has none of the danger that makes an unknown dialect worth
    stopping for, and the config layer already reports a malformed setting in its own words.
    """
    tables = cfg.get("providers")
    if not isinstance(tables, dict):
        return [], []
    providers: list[Provider] = []
    refusals: list[str] = []
    for name, table in tables.items():
        if not isinstance(table, dict):
            continue
        try:
            providers.append(build_provider(str(name), table))
        except UnknownDialect as exc:
            refusals.append(str(exc))
    return providers, refusals
