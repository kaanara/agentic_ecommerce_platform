"""Storefront contract. The CEO selects a platform; we plug in the matching adapter.

Agents only ever talk to this protocol (through tools / the store MCP), so switching from the mock
store to Shopify, Lemon Squeezy, Paddle, Gumroad, ... does not change any prompt or graph.
"""

from __future__ import annotations

from typing import Any, Protocol

# EU B2C digital goods are taxed at the *customer's* VAT rate (OSS scheme). Prices are VAT-inclusive.
VAT_RATES = {"DE": 0.19, "FR": 0.20, "NL": 0.21, "IT": 0.22, "ES": 0.21, "AT": 0.20, "PL": 0.23, "SE": 0.25, "US": 0.0, "GB": 0.20}


def vat_included(gross: float, country: str) -> float:
    rate = VAT_RATES.get(country.upper(), 0.0)
    return round(gross * rate / (1 + rate), 2)


class StoreAdapter(Protocol):
    name: str

    def list_products(self, status: str | None = None) -> list[dict[str, Any]]: ...
    def get_product(self, sku: str) -> dict[str, Any] | None: ...
    def create_product(self, sku: str, title: str, price_eur: float, kind: str, description: str = "",
                       unit_cost_eur: float = 0.0, vendor: str | None = None, asset_url: str | None = None) -> dict[str, Any]: ...
    def update_price(self, sku: str, price_eur: float) -> dict[str, Any]: ...
    def set_status(self, sku: str, status: str) -> dict[str, Any]: ...
    def add_license_keys(self, sku: str, keys: list[str]) -> int: ...
    def inventory(self) -> list[dict[str, Any]]: ...
    def create_order(self, customer_email: str, sku: str, country: str = "DE", campaign_id: str | None = None) -> dict[str, Any]: ...
    def deliver_order(self, order_id: str) -> dict[str, Any]: ...
    def refund_order(self, order_id: str, amount_eur: float, reason: str) -> dict[str, Any]: ...
    def get_order(self, order_id: str) -> dict[str, Any] | None: ...
    def list_orders(self, limit: int = 50) -> list[dict[str, Any]]: ...


def get_store() -> StoreAdapter:
    from aecom.config import get_settings

    name = get_settings().store_adapter
    if name == "mock":
        from aecom.store.mock import MockStore

        return MockStore()
    raise NotImplementedError(
        f"Store adapter '{name}' is not implemented yet. The CEO's platform decision record lists the "
        "provisioning steps; add aecom/store/<platform>.py implementing StoreAdapter."
    )
