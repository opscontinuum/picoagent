"""Google Vertex AI (Gemini) as a plugin - what a provider plugin has to do, and no more.

**The dialect itself ships in core**, as :mod:`picoagent.core.vertex`, selected by
``dialect = "vertex"`` in a ``[providers.<name>]`` table. You do not need this plugin to reach
Vertex; four lines of TOML do it. What this file is for is the other half, the half core cannot
demonstrate because core does not need it: how a plugin *registers* a provider.

Read :mod:`picoagent.core.vertex` for the dialect - the ``contents`` / ``parts`` /
``functionCall`` mapping, the ``:streamGenerateContent?alt=sse`` stream, the schema cleaning and
the credential model. Read this file for ``api.provider_config``, ``warn_about_project_config``
and ``api.register_provider``. It used to carry its own copy of the dialect, which meant two
implementations of one wire format in one repository: they had already begun to drift - core
gained a ``base_url`` scheme check this copy never had - and that is the failure the split
prevents. A worked example is worth less than a correct one.

Registering under a name core also configures is deliberate and supported: later registration
wins, so enabling this plugin replaces core's ``vertex``. That is the seam a third dialect -
Anthropic, Bedrock - arrives through, and the reason it stays open.

Configuration (``[providers.vertex]`` **in your own config.toml**, or env vars)::

    [providers.vertex]
    project  = "my-gcp-project"        # GOOGLE_CLOUD_PROJECT
    location = "us-central1"           # GOOGLE_CLOUD_LOCATION
    base_url = "http://127.0.0.1:8766" # VERTEX_BASE_URL - override for fakes / proxies

``[providers.<name>]`` is where every provider's endpoint lives, this one and core's ``openai``
alike, and it carries whatever a dialect needs rather than a fixed pair: Gemini's URL is built
from ``project`` and ``location``, so those are endpoint settings in the same sense ``base_url``
is. The dialect is code; the host it speaks to is a value. That is why ``base_url`` sits beside
``location`` at all - the same dialect points at commercial Vertex AI for one user and at a
government deployment for another, and that is a line in a config file, not a second plugin.

Every one of these decides where an OAuth bearer token is sent, so all of them are read from the
user layer only. ``providers`` is in ``config.USER_ONLY``, so a repository's
``.picoagent/config.toml`` never reaches this table: a cloned repository setting ``base_url``
would otherwise receive a live Google access token, minted from your ``gcloud`` login, on the
first turn. See ``docs/security/trust-boundaries.md``. A redirect cannot move the token either -
the request goes through core's redirect-refusing opener, which this provider inherits rather
than reimplementing, and which a plugin writing its own HTTP would have to import deliberately.

``[plugins.vertex-provider]`` is where these settings used to live. It still works, so an
existing config keeps running, and ``api.provider_config`` names on stderr which key it read
from there and where to move it.

Run with:  picoagent --provider vertex -m gemini-2.0-flash
"""
import os

from picoagent.core.vertex import VertexProvider


def register(api):
    cfg = api.provider_config("vertex")
    api.warn_about_project_config()
    api.register_provider(VertexProvider(
        project=cfg.get("project") or os.environ.get("GOOGLE_CLOUD_PROJECT", "my-project"),
        location=cfg.get("location") or os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1"),
        base_url=cfg.get("base_url") or os.environ.get("VERTEX_BASE_URL"),
        token=cfg.get("token"),
    ))
