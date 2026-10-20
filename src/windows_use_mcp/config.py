"""Resolution of the LLM settings used to drive the Windows-Use agent.

Settings are resolved from two places, in priority order:

1. Environment variables:
   ``WINDOWS_USE_PROVIDER``, ``WINDOWS_USE_MODEL``, ``WINDOWS_USE_API_KEY``,
   ``WINDOWS_USE_BASE_URL``.
2. The Windows-Use CLI configuration at ``~/.windows-use/config.json`` (the
   active provider entry; its API key is decrypted by the upstream package).

Individual calls may override ``provider`` and ``model``. API keys are never
accepted as a tool argument - they only ever come from the environment or the
on-disk CLI config.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

ENV_PROVIDER = "WINDOWS_USE_PROVIDER"
ENV_MODEL = "WINDOWS_USE_MODEL"
ENV_API_KEY = "WINDOWS_USE_API_KEY"
ENV_BASE_URL = "WINDOWS_USE_BASE_URL"
ENV_FALLBACKS = "WINDOWS_USE_FALLBACKS"

#: Providers that talk to a hosted API and therefore need a key.
REMOTE_PROVIDERS = {
    "groq",
    "openai",
    "atlas_cloud",
    "anthropic",
    "google",
    "mistral",
    "cerebras",
    "open_router",
    "azure_openai",
    "litellm",
    "deepseek",
    "nvidia",
    "perplexity",
}


class ConfigurationError(RuntimeError):
    """Raised when no usable LLM configuration could be resolved."""


@dataclass(frozen=True)
class LLMSettings:
    """A fully resolved LLM configuration."""

    provider: str
    model: str
    api_key: str | None = None
    base_url: str | None = None
    source: str = "unknown"
    name: str | None = None

    def describe(self) -> str:
        """Human readable summary. Never includes the API key itself."""
        parts = [
            f"provider={self.provider}",
            f"model={self.model}",
            f"base_url={self.base_url or '(default)'}",
            f"api_key={'set' if self.api_key else 'missing'}",
            f"source={self.source}",
        ]
        return " | ".join(parts)

    def public_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "api_key_configured": bool(self.api_key),
            "source": self.source,
        }


def _cli_config() -> LLMSettings | None:
    """Read the active provider entry from the Windows-Use CLI config."""
    try:
        from windows_use.cli.config import get_active_config
    except Exception:
        return None

    try:
        cfg = get_active_config()
    except Exception:
        return None

    if not cfg:
        return None

    provider = cfg.get("provider")
    model = cfg.get("llm")
    if not provider or not model:
        return None

    return LLMSettings(
        provider=provider,
        model=model,
        api_key=cfg.get("api_key") or None,
        base_url=cfg.get("base_url") or None,
        source="windows-use-cli-config",
    )


def _env_config() -> LLMSettings | None:
    provider = os.environ.get(ENV_PROVIDER)
    model = os.environ.get(ENV_MODEL)
    if not provider or not model:
        return None
    return LLMSettings(
        provider=provider,
        model=model,
        api_key=os.environ.get(ENV_API_KEY) or None,
        base_url=os.environ.get(ENV_BASE_URL) or None,
        source="env",
    )


def resolve_settings(provider: str | None = None, model: str | None = None) -> LLMSettings:
    """Resolve the LLM settings, applying optional per-call overrides.

    Raises:
        ConfigurationError: when no provider/model pair could be determined.
    """
    env = _env_config()

    # The CLI config lives inside the upstream package, so reading it pulls in
    # the whole (slow) windows_use import. Only do that when it is needed.
    cli: LLMSettings | None = None
    base = env
    if base is None:
        base = cli = _cli_config()
    elif provider and provider != base.provider:
        cli = _cli_config()
        if cli is not None and cli.provider == provider:
            base = cli

    if base is None:
        raise ConfigurationError(
            "No LLM configured for the Windows-Use agent. Set the environment "
            f"variables {ENV_PROVIDER} and {ENV_MODEL} (plus {ENV_API_KEY} for hosted "
            "providers), or run the `windows-use` CLI once to create "
            "~/.windows-use/config.json."
        )

    resolved_provider = provider or base.provider
    resolved_model = model or base.model

    # A base_url is only meaningful for the provider it was configured with.
    if resolved_provider == base.provider:
        resolved_key = base.api_key
        resolved_base_url = base.base_url
    else:
        resolved_key = os.environ.get(ENV_API_KEY) or None
        resolved_base_url = os.environ.get(ENV_BASE_URL) or None

    if resolved_provider in REMOTE_PROVIDERS and not resolved_key:
        raise ConfigurationError(
            f"Provider '{resolved_provider}' needs an API key. Set {ENV_API_KEY} or run the "
            "`windows-use` CLI to store one in ~/.windows-use/config.json."
        )

    source = base.source
    if provider or model:
        source = f"{source}+override"

    return LLMSettings(
        provider=resolved_provider,
        model=resolved_model,
        api_key=resolved_key,
        base_url=resolved_base_url,
        source=source,
    )


def build_llm(settings: LLMSettings):
    """Instantiate the Windows-Use chat model described by ``settings``."""
    from windows_use.cli.setup import create_llm

    return create_llm(
        provider=settings.provider,
        model=settings.model,
        api_key=settings.api_key,
        base_url=settings.base_url,
    )


def fallback_settings() -> list[LLMSettings]:
    """Read the optional fallback chain from ``WINDOWS_USE_FALLBACKS``.

    The variable holds a JSON array of provider configurations::

        [
          {"name": "agnes",  "provider": "openai", "model": "agnes-3.0-flash",
           "api_key": "...", "base_url": "https://apihub.agnes-ai.com/v1"},
          {"name": "atria",  "provider": "openai", "model": "Atria-Dawn",
           "api_key": "...", "base_url": "https://api.atria-asi.ai/v1/"}
        ]

    They are tried in order when the primary provider cannot serve a request
    (quota exhausted, rate limited, network error).
    """
    raw = os.environ.get(ENV_FALLBACKS)
    if not raw:
        return []

    try:
        items = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"{ENV_FALLBACKS} is not valid JSON: {exc}") from exc

    if not isinstance(items, list):
        raise ConfigurationError(f"{ENV_FALLBACKS} must be a JSON array.")

    out: list[LLMSettings] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        provider = item.get("provider")
        model = item.get("model")
        if not provider or not model:
            continue
        out.append(
            LLMSettings(
                provider=provider,
                model=model,
                api_key=item.get("api_key") or None,
                base_url=item.get("base_url") or None,
                source="fallback",
                name=item.get("name") or None,
            )
        )
    return out


def resolve_candidates(provider: str | None = None, model: str | None = None) -> list[LLMSettings]:
    """Return the primary configuration followed by the fallback chain."""
    primary = resolve_settings(provider, model)

    seen = {(primary.provider, primary.model, primary.base_url)}
    candidates = [primary]

    for extra in fallback_settings():
        key = (extra.provider, extra.model, extra.base_url)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(extra)

    return candidates
