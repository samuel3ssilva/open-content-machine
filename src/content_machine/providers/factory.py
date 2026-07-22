"""Provider selection (ADR 0002 §3).

Maps the typed ``settings.provider`` choice onto a concrete implementation. The
default and only end-to-end-wired provider this sprint is ``mock``.
"""

from __future__ import annotations

from content_machine.config.settings import Settings
from content_machine.providers.anthropic_provider import AnthropicProvider
from content_machine.providers.base import ModelProvider
from content_machine.providers.mock import MockProvider
from content_machine.providers.openai_provider import OpenAIProvider


def get_provider(settings: Settings) -> ModelProvider:
    """Return the configured :class:`ModelProvider` implementation."""
    if settings.provider == "anthropic":
        return AnthropicProvider(settings)
    if settings.provider == "openai":
        return OpenAIProvider(settings)
    return MockProvider()
