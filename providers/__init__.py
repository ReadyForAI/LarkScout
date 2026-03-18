"""LLM provider factory.

Selects the active provider via LARKSCOUT_LLM_PROVIDER env var (default: gemini).
Supported values: "gemini", "openai" (any OpenAI-compatible REST API).

The resolved provider is cached as a module-level singleton so callers share one
instance per process.
"""

import os

from providers.base import LLMProvider

_provider: LLMProvider | None = None


def get_provider() -> LLMProvider:
    """Return the cached LLM provider, creating it on first call.

    The provider is selected by the LARKSCOUT_LLM_PROVIDER environment variable:
      - "gemini"  (default) → GeminiProvider
      - "openai"            → OpenAICompatProvider
    """
    global _provider
    if _provider is not None:
        return _provider

    name = os.environ.get("LARKSCOUT_LLM_PROVIDER", "gemini").lower().strip()

    if name == "gemini":
        from providers.gemini import GeminiProvider

        _provider = GeminiProvider()
    elif name == "openai":
        from providers.openai_compat import OpenAICompatProvider

        _provider = OpenAICompatProvider()
    else:
        raise ValueError(
            f"Unknown LARKSCOUT_LLM_PROVIDER={name!r}. "
            "Supported values: 'gemini', 'openai'."
        )

    return _provider


def reset_provider() -> None:
    """Clear the cached provider (used in tests)."""
    global _provider
    _provider = None
