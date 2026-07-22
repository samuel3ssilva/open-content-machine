"""Provider abstraction (ADR 0002).

The product core depends only on the :class:`ModelProvider` protocol here, never
on a vendor SDK. Requests and responses are Pydantic models. No implementation
in this sprint performs network I/O.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel


class ProviderNotConfiguredError(RuntimeError):
    """Raised when a real provider is selected but not usable.

    The message points the user to ``.env`` and never asks for keys to be pasted
    into the chat / CLI.
    """


class ModelRequest(BaseModel):
    """A single completion request. ``schema_name`` names the JSON Schema the
    response should be validated against (structured outputs; ADR 0002 §4)."""

    prompt: str
    schema_name: str | None = None
    max_tokens: int = 1024


class ModelResponse(BaseModel):
    """A single completion response. ``structured`` holds validated structured
    output when a schema was requested; ``text`` is the raw text."""

    text: str
    structured: dict[str, object] | None = None
    provider: str


@runtime_checkable
class ModelProvider(Protocol):
    """Interface every provider implements."""

    @property
    def name(self) -> str:
        """Stable provider identifier (e.g. ``"mock"``)."""
        ...

    def is_available(self) -> bool:
        """Whether this provider can currently serve requests."""
        ...

    def complete(self, request: ModelRequest) -> ModelResponse:
        """Serve a completion request."""
        ...
