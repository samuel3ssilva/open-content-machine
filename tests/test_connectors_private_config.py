"""Tests for content_machine.connectors.private_config (Gate E0 §5, Fable
ruling F6): the fail-closed private endpoint configuration loader.

Every "realistic-looking" hostname/endpoint value in this file is
``example.com``/``example.org`` shaped -- invented, synthetic, and resolves
to nothing real. No test in this file makes a network call, reads
``data/private/``, or points ``Settings.private_source_config_path`` at a
real file.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from content_machine.config.settings import Settings
from content_machine.connectors import private_config as private_config_module
from content_machine.connectors.private_config import (
    ExpiredPrivateConfigError,
    InvalidPrivateConfigError,
    MissingPrivateConfigError,
    PrivateConfigError,
    PrivateSourceConfig,
    PrivateSourceEndpoint,
    UnreadablePrivateConfigError,
    load_private_source_config,
    source_allowed_hosts_from_config,
)

# Synthetic, example.com/example.org-shaped values only. Deliberately
# "realistic-looking" (a plausible vendor-feed path) so the leak tests below
# prove something real: a substring that WOULD matter if it were the actual
# private endpoint.
_SYNTHETIC_HOSTNAME = "partner-feed.acme-connectors.example.com"
_SYNTHETIC_ENDPOINT = "https://partner-feed.acme-connectors.example.com/v1/items.json"
_SYNTHETIC_HOSTNAME_2 = "changelog.vendor-example.example.org"
_SYNTHETIC_ENDPOINT_2 = "https://changelog.vendor-example.example.org/rss.xml"


def _write_config(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _valid_payload() -> dict:
    return {
        "endpoints": [
            {
                "source_id": "partner-feed",
                "hostname": _SYNTHETIC_HOSTNAME,
                "endpoint": _SYNTHETIC_ENDPOINT,
            },
            {
                "source_id": "vendor-changelog",
                "hostname": _SYNTHETIC_HOSTNAME_2,
                "endpoint": _SYNTHETIC_ENDPOINT_2,
            },
        ]
    }


# --- missing file --------------------------------------------------------


def test_no_path_configured_fails_closed_as_missing() -> None:
    settings = Settings(private_source_config_path=None)
    with pytest.raises(MissingPrivateConfigError) as excinfo:
        load_private_source_config(settings)
    assert str(excinfo.value) == "private source config: not configured or file not found"


def test_configured_path_that_does_not_exist_fails_closed_as_missing(tmp_path: Path) -> None:
    missing_path = tmp_path / "does-not-exist.json"
    settings = Settings(private_source_config_path=missing_path)
    with pytest.raises(MissingPrivateConfigError):
        load_private_source_config(settings)


def test_missing_and_no_path_are_the_same_reason_code(tmp_path: Path) -> None:
    """Both "nothing configured" and "configured but absent" must be the
    SAME exception type -- CI relies on this single, always-exercised path
    (module docstring)."""
    missing_path = tmp_path / "nope.json"
    with pytest.raises(MissingPrivateConfigError):
        load_private_source_config(Settings(private_source_config_path=None))
    with pytest.raises(MissingPrivateConfigError):
        load_private_source_config(path=missing_path)


def test_path_that_is_a_directory_fails_closed_as_missing(tmp_path: Path) -> None:
    directory = tmp_path / "a_directory.json"
    directory.mkdir()
    with pytest.raises(MissingPrivateConfigError):
        load_private_source_config(path=directory)


def test_neither_settings_nor_path_is_a_caller_contract_error() -> None:
    """Calling with nothing at all is a programmer error (ValueError), not
    a fail-closed data outcome -- this module never guesses a default or
    reads the environment itself."""
    with pytest.raises(ValueError, match="requires either"):
        load_private_source_config()


# --- unreadable file -------------------------------------------------------


def test_unreadable_file_fails_closed_distinctly(tmp_path: Path) -> None:
    config_path = tmp_path / "private_sources.json"
    _write_config(config_path, _valid_payload())
    config_path.chmod(0o000)
    try:
        with pytest.raises(UnreadablePrivateConfigError) as excinfo:
            load_private_source_config(path=config_path)
        assert str(config_path) not in str(excinfo.value)
        assert "OSError" in str(excinfo.value) or "PermissionError" in str(excinfo.value)
    finally:
        # Restore permissions so pytest's own tmp_path cleanup can delete it.
        config_path.chmod(0o644)


# --- invalid content ---------------------------------------------------


def test_malformed_json_fails_closed_as_invalid_content(tmp_path: Path) -> None:
    config_path = tmp_path / "private_sources.json"
    config_path.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(InvalidPrivateConfigError) as excinfo:
        load_private_source_config(path=config_path)
    assert str(excinfo.value) == "private source config: invalid content (JSONDecodeError)"


def test_schema_violation_fails_closed_as_invalid_content(tmp_path: Path) -> None:
    config_path = tmp_path / "private_sources.json"
    payload = {
        "endpoints": [
            {"source_id": "x", "hostname": "", "endpoint": _SYNTHETIC_ENDPOINT},
        ]
    }
    _write_config(config_path, payload)
    with pytest.raises(InvalidPrivateConfigError) as excinfo:
        load_private_source_config(path=config_path)
    assert str(excinfo.value) == "private source config: invalid content (ValidationError)"


def test_duplicate_source_id_fails_closed_as_invalid_content(tmp_path: Path) -> None:
    config_path = tmp_path / "private_sources.json"
    payload = {
        "endpoints": [
            {
                "source_id": "dup",
                "hostname": _SYNTHETIC_HOSTNAME,
                "endpoint": _SYNTHETIC_ENDPOINT,
            },
            {
                "source_id": "dup",
                "hostname": _SYNTHETIC_HOSTNAME_2,
                "endpoint": _SYNTHETIC_ENDPOINT_2,
            },
        ]
    }
    _write_config(config_path, payload)
    with pytest.raises(InvalidPrivateConfigError):
        load_private_source_config(path=config_path)


# --- F6: the core security requirement ----------------------------------


def test_pydantic_validation_error_does_leak_the_input_value_by_default() -> None:
    """Proves the threat this module exists to close: pydantic's OWN
    ValidationError, uncontrolled, embeds the offending input value in its
    str(). This is what load_private_source_config must never let escape.

    Uses ``_SYNTHETIC_HOSTNAME`` (under pydantic's ~48-char truncation
    threshold for embedded input values, confirmed directly against this
    repo's pinned pydantic version) so the FULL value is provably present,
    not merely a truncated fragment of it -- the strongest form of this
    demonstration.
    """
    with pytest.raises(ValidationError) as excinfo:
        PrivateSourceEndpoint.model_validate(
            {
                "source_id": "leaky",
                "hostname": _SYNTHETIC_HOSTNAME,
                "endpoint": _SYNTHETIC_ENDPOINT,
                "rate_limit_max_calls": _SYNTHETIC_HOSTNAME,  # wrong type
            }
        )
    # The raw pydantic exception DOES leak -- this is the problem, not the fix.
    assert _SYNTHETIC_HOSTNAME in str(excinfo.value)


def test_loader_never_leaks_the_endpoint_on_a_type_mismatch(tmp_path: Path) -> None:
    config_path = tmp_path / "private_sources.json"
    payload = {
        "endpoints": [
            {
                "source_id": "partner-feed",
                "hostname": _SYNTHETIC_HOSTNAME,
                "endpoint": _SYNTHETIC_ENDPOINT,
                "rate_limit_max_calls": _SYNTHETIC_ENDPOINT,  # wrong type: str, not int
            }
        ]
    }
    _write_config(config_path, payload)

    with pytest.raises(InvalidPrivateConfigError) as excinfo:
        load_private_source_config(path=config_path)

    exc = excinfo.value
    assert _SYNTHETIC_ENDPOINT not in str(exc)
    assert _SYNTHETIC_ENDPOINT not in repr(exc)
    assert _SYNTHETIC_HOSTNAME not in str(exc)
    assert _SYNTHETIC_HOSTNAME not in repr(exc)
    assert str(exc) == "private source config: invalid content (ValidationError)"


def test_loader_never_leaks_the_endpoint_on_an_extra_field_violation(tmp_path: Path) -> None:
    config_path = tmp_path / "private_sources.json"
    payload = _valid_payload()
    payload["unexpected_field"] = _SYNTHETIC_ENDPOINT_2
    _write_config(config_path, payload)

    with pytest.raises(InvalidPrivateConfigError) as excinfo:
        load_private_source_config(path=config_path)

    exc = excinfo.value
    assert _SYNTHETIC_ENDPOINT not in str(exc)
    assert _SYNTHETIC_ENDPOINT_2 not in str(exc)
    assert _SYNTHETIC_ENDPOINT not in repr(exc)
    assert _SYNTHETIC_ENDPOINT_2 not in repr(exc)


def test_secret_str_fields_never_leak_via_repr_str_or_dump() -> None:
    endpoint = PrivateSourceEndpoint(
        source_id="partner-feed",
        hostname=_SYNTHETIC_HOSTNAME,
        endpoint=_SYNTHETIC_ENDPOINT,
    )
    assert _SYNTHETIC_HOSTNAME not in repr(endpoint)
    assert _SYNTHETIC_HOSTNAME not in str(endpoint)
    assert _SYNTHETIC_ENDPOINT not in repr(endpoint)
    assert _SYNTHETIC_ENDPOINT not in str(endpoint)
    assert _SYNTHETIC_HOSTNAME not in endpoint.model_dump_json()
    assert _SYNTHETIC_ENDPOINT not in endpoint.model_dump_json()
    dumped = endpoint.model_dump()
    assert _SYNTHETIC_HOSTNAME not in repr(dumped)
    assert _SYNTHETIC_ENDPOINT not in repr(dumped)

    config = PrivateSourceConfig(endpoints=(endpoint,))
    assert _SYNTHETIC_HOSTNAME not in repr(config)
    assert _SYNTHETIC_ENDPOINT not in repr(config)
    assert _SYNTHETIC_HOSTNAME not in config.model_dump_json()
    assert _SYNTHETIC_ENDPOINT not in config.model_dump_json()


def test_expired_config_error_never_leaks_hostname_or_endpoint() -> None:
    exc = ExpiredPrivateConfigError(("partner-feed",))
    assert _SYNTHETIC_HOSTNAME not in str(exc)
    assert _SYNTHETIC_ENDPOINT not in str(exc)
    assert "partner-feed" in str(exc)


# --- valid synthetic config ----------------------------------------------


def test_valid_synthetic_config_loads(tmp_path: Path) -> None:
    config_path = tmp_path / "private_sources.json"
    _write_config(config_path, _valid_payload())

    config = load_private_source_config(path=config_path)

    assert isinstance(config, PrivateSourceConfig)
    assert len(config.endpoints) == 2
    assert {e.source_id for e in config.endpoints} == {"partner-feed", "vendor-changelog"}


def test_valid_config_via_settings(tmp_path: Path) -> None:
    config_path = tmp_path / "private_sources.json"
    _write_config(config_path, _valid_payload())
    settings = Settings(private_source_config_path=config_path)

    config = load_private_source_config(settings)

    assert len(config.endpoints) == 2


def test_source_allowed_hosts_from_config_produces_correct_mapping(tmp_path: Path) -> None:
    config_path = tmp_path / "private_sources.json"
    _write_config(config_path, _valid_payload())
    config = load_private_source_config(path=config_path)

    mapping = source_allowed_hosts_from_config(config)

    assert mapping == {
        "partner-feed": frozenset({_SYNTHETIC_HOSTNAME}),
        "vendor-changelog": frozenset({_SYNTHETIC_HOSTNAME_2}),
    }


def test_empty_endpoints_list_loads_to_empty_config(tmp_path: Path) -> None:
    config_path = tmp_path / "private_sources.json"
    _write_config(config_path, {"endpoints": []})

    config = load_private_source_config(path=config_path)

    assert config.endpoints == ()
    assert source_allowed_hosts_from_config(config) == {}


# --- expired / unauthorized permission -----------------------------------


def test_expired_authorized_until_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(private_config_module, "_today_utc", lambda: date(2026, 7, 27))
    config_path = tmp_path / "private_sources.json"
    payload = {
        "endpoints": [
            {
                "source_id": "partner-feed",
                "hostname": _SYNTHETIC_HOSTNAME,
                "endpoint": _SYNTHETIC_ENDPOINT,
                "authorized_until": "2026-07-26",
            }
        ]
    }
    _write_config(config_path, payload)

    with pytest.raises(ExpiredPrivateConfigError) as excinfo:
        load_private_source_config(path=config_path)
    assert excinfo.value.expired_source_ids == ("partner-feed",)


def test_authorized_until_today_is_still_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boundary: valid THROUGH the expiry date itself, mirroring
    permissions.PermissionRegistry.authorize_retrieval's own reading."""
    monkeypatch.setattr(private_config_module, "_today_utc", lambda: date(2026, 7, 27))
    config_path = tmp_path / "private_sources.json"
    payload = {
        "endpoints": [
            {
                "source_id": "partner-feed",
                "hostname": _SYNTHETIC_HOSTNAME,
                "endpoint": _SYNTHETIC_ENDPOINT,
                "authorized_until": "2026-07-27",
            }
        ]
    }
    _write_config(config_path, payload)

    config = load_private_source_config(path=config_path)
    assert len(config.endpoints) == 1


def test_unauthorized_flag_fails_closed_even_without_expiry_date(tmp_path: Path) -> None:
    config_path = tmp_path / "private_sources.json"
    payload = {
        "endpoints": [
            {
                "source_id": "partner-feed",
                "hostname": _SYNTHETIC_HOSTNAME,
                "endpoint": _SYNTHETIC_ENDPOINT,
                "authorized": False,
            }
        ]
    }
    _write_config(config_path, payload)

    with pytest.raises(ExpiredPrivateConfigError) as excinfo:
        load_private_source_config(path=config_path)
    assert excinfo.value.expired_source_ids == ("partner-feed",)


def test_one_expired_source_fails_the_whole_load_never_silently_drops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One expired source must fail the WHOLE load closed -- never silently
    continue with the remaining, still-valid sources (module docstring's
    "reject, never silently strip" rule)."""
    monkeypatch.setattr(private_config_module, "_today_utc", lambda: date(2026, 7, 27))
    config_path = tmp_path / "private_sources.json"
    payload = {
        "endpoints": [
            {
                "source_id": "partner-feed",
                "hostname": _SYNTHETIC_HOSTNAME,
                "endpoint": _SYNTHETIC_ENDPOINT,
                "authorized_until": "2026-07-26",
            },
            {
                "source_id": "vendor-changelog",
                "hostname": _SYNTHETIC_HOSTNAME_2,
                "endpoint": _SYNTHETIC_ENDPOINT_2,
            },
        ]
    }
    _write_config(config_path, payload)

    with pytest.raises(ExpiredPrivateConfigError) as excinfo:
        load_private_source_config(path=config_path)
    assert excinfo.value.expired_source_ids == ("partner-feed",)


# --- error taxonomy --------------------------------------------------------


def test_all_fail_closed_exceptions_share_a_common_base() -> None:
    assert issubclass(MissingPrivateConfigError, PrivateConfigError)
    assert issubclass(UnreadablePrivateConfigError, PrivateConfigError)
    assert issubclass(InvalidPrivateConfigError, PrivateConfigError)
    assert issubclass(ExpiredPrivateConfigError, PrivateConfigError)


def test_missing_unreadable_invalid_expired_are_distinct_types(tmp_path: Path) -> None:
    """The four fail-closed outcomes must be distinguishable by a caller
    (e.g. a CLI deciding what to tell an operator) -- never collapsed into
    one generic error."""
    reason_types = {
        MissingPrivateConfigError,
        UnreadablePrivateConfigError,
        InvalidPrivateConfigError,
        ExpiredPrivateConfigError,
    }
    assert len(reason_types) == 4


# --- public surface ---------------------------------------------------------


def test_secret_str_type_is_used_for_hostname_and_endpoint() -> None:
    endpoint = PrivateSourceEndpoint(
        source_id="partner-feed", hostname=_SYNTHETIC_HOSTNAME, endpoint=_SYNTHETIC_ENDPOINT
    )
    assert isinstance(endpoint.hostname, SecretStr)
    assert isinstance(endpoint.endpoint, SecretStr)
    assert endpoint.hostname.get_secret_value() == _SYNTHETIC_HOSTNAME
    assert endpoint.endpoint.get_secret_value() == _SYNTHETIC_ENDPOINT
