"""Where licence keys come from. Only keys a vendor actually issued may be sold.

`mock` mints random strings (demo only: they unlock nothing); `none` = no automatic source, so restocks
need explicit keys until a real vendor feed is connected (add a KeySource and register it below).
"""

from __future__ import annotations

import secrets
from typing import Protocol

NO_KEY_SOURCE = "no licence key source configured - add keys manually or connect the vendor feed"


class KeySource(Protocol):
    def fetch(self, sku: str, count: int) -> list[str]: ...


class MockKeySource:
    """Mints fake keys like a vendor feed would deliver them. Demo only - never with a real store."""

    def fetch(self, sku: str, count: int) -> list[str]:
        return [f"{sku}-{secrets.token_hex(4).upper()}-{secrets.token_hex(4).upper()}" for _ in range(count)]


def get_key_source() -> KeySource | None:
    """The configured automatic key source, or None when keys must be added manually."""
    from aecom.config import get_settings

    name = get_settings().license_key_source
    if name == "mock":
        return MockKeySource()
    if name == "none":
        return None
    raise NotImplementedError(f"Licence key source '{name}' is not implemented; add it to aecom/store/keysource.py.")
