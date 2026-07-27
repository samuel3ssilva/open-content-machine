"""Synthetic, network-free adapters and fixtures for exercising the Gate D
connector runtime (spec §9). Everything in this subpackage is deterministic,
offline demonstration/test scaffolding -- no real source, no network I/O, no
vendor SDK import anywhere.
"""

from __future__ import annotations

from content_machine.connectors.synthetic.adapters import (
    MaliciousContentSyntheticAdapter,
    OversizedContentSyntheticAdapter,
    PartialBatchSyntheticAdapter,
    RateLimitedSyntheticAdapter,
    RevokedPermissionSyntheticAdapter,
    SuccessfulSyntheticAdapter,
    SyntheticItemSpec,
    TimeoutSyntheticAdapter,
    redirect_chain_flags,
)

__all__ = [
    "MaliciousContentSyntheticAdapter",
    "OversizedContentSyntheticAdapter",
    "PartialBatchSyntheticAdapter",
    "RateLimitedSyntheticAdapter",
    "RevokedPermissionSyntheticAdapter",
    "SuccessfulSyntheticAdapter",
    "SyntheticItemSpec",
    "TimeoutSyntheticAdapter",
    "redirect_chain_flags",
]
