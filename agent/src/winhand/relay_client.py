"""Outbound connection to the winhand relay (implemented in the relay phase)."""

from __future__ import annotations


async def run_forever(url: str, token: str) -> None:  # pragma: no cover - replaced by the relay phase
    raise SystemExit("relay support is not implemented yet")
