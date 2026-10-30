"""F3 (Roadmap): sales channels. `StoreAdapter`/`get_store()` (`aecom.store.adapter`) assumes exactly one
storefront is "the business" - the one agents and the ledger talk to. This module adds the notion of a second,
independent **sales channel** (Etsy) that sells the *same* local catalog and draws from the *same* licence-key
pool as the primary channel, but needs its own listing sync and order mirroring because Etsy is not a
`StoreAdapter`: it never becomes the merchant of record for the whole business, it just sells alongside it.

`ChannelAdapter` is deliberately smaller than `StoreAdapter`:
- **Read-first sync** (`sync_listings`, `sync_orders`) is the only thing this module or the scheduler ever calls
  on its own. It is safe to run on a schedule or by hand.
- **Side-effecting writes** (`create_listing`, `update_listing_inventory`) exist on the protocol so the lead can
  register them as gated tools (T3: a human approves every call - creating or re-stocking a public listing is a
  storefront-facing, hard-to-undo action). Nothing in this package calls them automatically; they are reached only
  through whatever tool wraps them.

A channel is identified by name: `"primary"` is always `get_store()` (the configured `StoreAdapter`); `"etsy"` is
`aecom.store.etsy.EtsyChannel`, on only when `etsy_enabled` is set (default off - F3 ships disabled, like every
other channel/adapter in this codebase until a human provisions it; see `enabled_channels`).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol


class ChannelAdapter(Protocol):
    """What a sales channel beyond the primary store offers. `name` is the channel key used in `get_channel`."""

    name: str

    # ---- read-first sync (what sync_all_listings/sync_all_orders and the scheduler call)
    def sync_listings(self) -> dict[str, Any]: ...
    def sync_orders(self, since: datetime) -> dict[str, Any]: ...

    # ---- side-effecting (T3 once the lead registers a tool for these; nothing here calls them automatically)
    def create_listing(self, sku: str, title: str, description: str, price_eur: float, **kwargs: Any) -> dict[str, Any]: ...
    def update_listing_inventory(self, sku: str, quantity: int) -> dict[str, Any]: ...


def enabled_channels() -> list[str]:
    """Channel names currently turned on. ``"primary"`` (the configured `StoreAdapter`) is always on. ``"etsy"``
    is added only when ``etsy_enabled`` is true (``getattr`` with a ``False`` default, so this works even before
    the lead adds the field to `config.Settings` - F3 ships off by default, like every other integration here)."""
    from aecom.config import get_settings

    channels = ["primary"]
    if getattr(get_settings(), "etsy_enabled", False):
        channels.append("etsy")
    return channels


def get_channel(name: str) -> ChannelAdapter:
    """The adapter for one channel name. ``"primary"`` is always available (it is just `get_store()`); other
    channels raise `NotImplementedError` until both added here and turned on in `enabled_channels`."""
    if name == "primary":
        from aecom.store.adapter import get_store

        return get_store()
    if name == "etsy":
        from aecom.store.etsy import EtsyChannel

        return EtsyChannel()
    raise NotImplementedError(f"sales channel {name!r} is not registered; add it to aecom/store/channels.py")


def sync_all_listings() -> dict[str, dict[str, Any]]:
    """`sync_listings()` of every enabled channel except ``"primary"`` (the primary catalog is the source of
    truth; it is never "synced" from itself). Empty when no secondary channel is enabled."""
    return {name: get_channel(name).sync_listings() for name in enabled_channels() if name != "primary"}


def sync_all_orders(since: datetime) -> dict[str, dict[str, Any]]:
    """`sync_orders(since)` of every enabled channel except ``"primary"`` (primary-channel orders are created by
    its own checkout/webhook, not by a sync). Empty when no secondary channel is enabled."""
    return {name: get_channel(name).sync_orders(since) for name in enabled_channels() if name != "primary"}
