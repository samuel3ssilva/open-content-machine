"""Typed application settings.

This is the ONLY module in the package that reads environment variables
(see docs/architecture.md §2 dependency rules). Everything else receives a
``Settings`` instance or explicit arguments.

Environment variables use the ``CONTENT_MACHINE_`` prefix, e.g.
``CONTENT_MACHINE_PROVIDER``, ``CONTENT_MACHINE_SALT``,
``CONTENT_MACHINE_DATA_DIR``. Vendor API keys keep their conventional,
unprefixed names (``ANTHROPIC_API_KEY``, ``OPENAI_API_KEY``) so the same
``.env`` works with the vendors' own tooling; they are still read only here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, loaded from environment and an optional ``.env``."""

    model_config = SettingsConfigDict(
        env_prefix="CONTENT_MACHINE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    provider: Literal["mock", "anthropic", "openai"] = "mock"
    salt: str | None = None
    data_dir: Path = Path("data/private")

    # Vendor keys are optional and read by their conventional names. They exist
    # here so provider modules can check availability without reading os.environ
    # themselves (keeping config the single env-reading choke point).
    anthropic_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("ANTHROPIC_API_KEY", "CONTENT_MACHINE_ANTHROPIC_API_KEY"),
    )
    openai_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("OPENAI_API_KEY", "CONTENT_MACHINE_OPENAI_API_KEY"),
    )


def get_settings() -> Settings:
    """Return a freshly loaded :class:`Settings` instance."""
    return Settings()
