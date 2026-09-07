"""grok-provider - xAI Grok for picoagent.

Grok speaks the OpenAI chat-completions wire format, so this plugin is just the
built-in client pointed at ``https://api.x.ai/v1`` under the name ``grok``.

Configuration (``[plugins.grok-provider]`` **in your own config**, or env vars)::

    base_url = "https://api.x.ai/v1"   # XAI_BASE_URL - override for fakes / proxies
    api_key  = "xai-..."               # XAI_API_KEY

Both are read from your ``~/.picoagent/config.toml`` only. ``api.plugin_config()`` answers
with the user layer, so a cloned repository's ``.picoagent/config.toml`` cannot set either -
and it is ``base_url`` that matters most here, not ``api_key``. A repository that sets only the
URL, leaving your key where it is, sends *your* key to *its* host on the first turn. That is
the same attack ``USER_ONLY`` closed for ``providers.openai.base_url``, arriving through a
plugin table instead. Nothing here is worth a repository's opinion, so nothing is accepted from
one; :meth:`warn_about_project_config` says so at session start rather than dropping it in
silence.

Run with:  picoagent --provider grok -m grok-4
"""
import os

from picoagent.core.provider import OpenAICompatProvider


def register(api):
    cfg = api.plugin_config()
    api.warn_about_project_config()
    api.register_provider(OpenAICompatProvider(
        name="grok",
        base_url=cfg.get("base_url") or os.environ.get("XAI_BASE_URL", "https://api.x.ai/v1"),
        api_key=cfg.get("api_key") or os.environ.get("XAI_API_KEY", ""),
    ))
