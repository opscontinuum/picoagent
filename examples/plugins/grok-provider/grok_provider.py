"""grok-provider - xAI Grok for picoagent.

Grok speaks the OpenAI chat-completions wire format, so this plugin is just the
built-in client pointed at ``https://api.x.ai/v1`` under the name ``grok``.

Configuration (``[plugins.grok-provider]`` or env vars)::

    base_url = "https://api.x.ai/v1"   # XAI_BASE_URL - override for fakes / proxies
    api_key  = "xai-..."               # XAI_API_KEY

Run with:  picoagent --provider grok -m grok-4
"""
import os

from picoagent.core.provider import OpenAICompatProvider


def register(api):
    cfg = api.plugin_config()
    api.register_provider(OpenAICompatProvider(
        name="grok",
        base_url=cfg.get("base_url") or os.environ.get("XAI_BASE_URL", "https://api.x.ai/v1"),
        api_key=cfg.get("api_key") or os.environ.get("XAI_API_KEY", ""),
    ))
