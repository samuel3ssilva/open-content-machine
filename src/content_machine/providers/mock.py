"""Deterministic, offline provider used by the demo and all tests (ADR 0002).

MockProvider never performs network I/O and is always available. It returns a
canned, deterministic response so pipelines and tests are reproducible with no
credentials.
"""

from __future__ import annotations

from content_machine.providers.base import ModelRequest, ModelResponse


class MockProvider:
    """A provider that echoes a deterministic, canned response."""

    @property
    def name(self) -> str:
        return "mock"

    def is_available(self) -> bool:
        return True

    def complete(self, request: ModelRequest) -> ModelResponse:
        """Return a deterministic response derived from the request.

        The output depends only on the request fields, so identical requests
        always produce identical responses.
        """
        schema = request.schema_name or "none"
        text = f"[mock:{schema}] {request.prompt.strip()}"
        structured: dict[str, object] | None = None
        if request.schema_name is not None:
            structured = {
                "schema_name": request.schema_name,
                "note": "Deterministic mock structured output; no model was called.",
            }
        return ModelResponse(text=text, structured=structured, provider=self.name)
