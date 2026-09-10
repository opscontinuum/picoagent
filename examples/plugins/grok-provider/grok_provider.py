"""grok-provider - xAI Grok for picoagent.

**This plugin is no longer needed.** Grok speaks the OpenAI chat-completions wire format, so a
name and a URL is the whole of it, and core now registers a provider for every
``[providers.<name>]`` table in your config::

    [providers.grok]
    base_url = "https://api.x.ai/v1"
    api_key  = "xai-..."

That is the same provider this file registers, with nothing to install, approve or upgrade. The
plugin still loads and still works, so an install that has it enabled keeps running; it is kept
here as the smallest possible worked example of ``register(api)``.

Grok speaks the OpenAI chat-completions wire format, so this plugin is just the
built-in client pointed at ``https://api.x.ai/v1`` under the name ``grok``.

Configuration (``[providers.grok]`` **in your own config**, or env vars)::

    [providers.grok]
    base_url = "https://api.x.ai/v1"   # XAI_BASE_URL - override for fakes / proxies
    api_key  = "xai-..."               # XAI_API_KEY

``[providers.<name>]`` is where every provider's endpoint lives, this one and core's ``openai``
alike: the dialect is the code in this file, and which host it speaks to is a value. What this
plugin contributes is the dialect's *identity*, not its address, so pointing it at a proxy, a
mirror or a fake in a test is a line in your config file rather than a fork of the plugin.

Both keys are read from your ``~/.picoagent/config.toml`` only. ``providers`` is in
``config.USER_ONLY``, so a cloned repository's ``.picoagent/config.toml`` cannot set either -
and it is ``base_url`` that matters most here, not ``api_key``. A repository that sets only the
URL, leaving your key where it is, sends *your* key to *its* host on the first turn.

``[plugins.grok-provider]`` is where these two used to live. It still works, so an existing
config keeps running, and ``api.provider_config`` names on stderr which key it read from there
and where to move it. Nothing here is worth a repository's opinion, so nothing is accepted from
one; :meth:`warn_about_project_config` says so at session start rather than in silence.

Run with:  picoagent --provider grok -m grok-4
"""
import os

from picoagent.core.provider import OpenAICompatProvider


def register(api):
    cfg = api.provider_config("grok")
    api.warn_about_project_config()
    api.register_provider(OpenAICompatProvider(
        name="grok",
        base_url=cfg.get("base_url") or os.environ.get("XAI_BASE_URL", "https://api.x.ai/v1"),
        api_key=cfg.get("api_key") or os.environ.get("XAI_API_KEY", ""),
    ))
