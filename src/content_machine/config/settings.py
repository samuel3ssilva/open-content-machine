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

    # Gate E0 §5 (private endpoint configuration loader, Fable ruling F6).
    # Points at a file OUTSIDE this repository -- typically in the
    # Founder's private workspace -- that carries a real source_id,
    # hostname, endpoint, limits, and authorization state for one or more
    # connector sources. Unset by default and in CI: CI must never require
    # this file to exist (see
    # content_machine.connectors.private_config.load_private_source_config,
    # which fails closed on a missing path exactly the same way it fails
    # closed on a configured-but-absent file). No default or example value
    # here is ever a real endpoint.
    private_source_config_path: Path | None = None

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
