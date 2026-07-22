"""OpenAI provider stub (ADR 0002 §6).

No real API call exists in this sprint. The vendor SDK is imported lazily inside
methods so the base install works without it. ``is_available`` returns True only
when a key is configured AND the SDK is importable; even then ``complete`` does
not call out — it raises to make the "future sprint after security review"
boundary explicit.
"""

from __future__ import annotations

from content_machine.config.settings import Settings
from content_machine.providers.base import (
    ModelRequest,
    ModelResponse,
    ProviderNotConfiguredError,
)


class OpenAIProvider:
    """Stub provider for OpenAI models. Performs no network I/O this sprint."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def name(self) -> str:
        return "openai"

    def is_available(self) -> bool:
        """True only if an API key is set and the ``openai`` SDK is installed."""
        if not self._settings.openai_api_key:
            return False
        try:
            import openai  # noqa: F401  (lazy availability probe only)
        except ImportError:
            return False
        return True

    def complete(self, request: ModelRequest) -> ModelResponse:
        """Never calls the network in this sprint.

        Raises :class:`ProviderNotConfiguredError` when unusable, or
        ``NotImplementedError`` when configured, deferring real calls to a
        future ticket with security review.
        """
        if not self.is_available():
            raise ProviderNotConfiguredError(
                "OpenAI provider is not configured. Set OPENAI_API_KEY in .env "
                "and install the 'openai' extra. Never paste keys into the CLI "
                "or chat."
            )
        raise NotImplementedError(
            "Real provider calls land in a future sprint after security review."
        )
