"""Storefront contract. The CEO selects a platform; we plug in the matching adapter.

Agents only ever talk to this protocol (through tools / the store MCP), so switching from the mock
store to Shopify, Lemon Squeezy, Paddle, Gumroad, ... does not change any prompt or graph.
"""

from __future__ import annotations

from typing import Any, Protocol

from aecom.money import vat_split

# EU B2C digital goods are taxed at the *customer's* VAT rate (OSS scheme). Prices are VAT-inclusive.
# Standard rates of all 27 EU member states as of 2026.
VAT_RATES = {
    "AT": 0.20, "BE": 0.21, "BG": 0.20, "HR": 0.25, "CY": 0.19, "CZ": 0.21, "DK": 0.25, "EE": 0.24, "FI": 0.255,
    "FR": 0.20, "DE": 0.19, "GR": 0.24, "HU": 0.27, "IE": 0.23, "IT": 0.22, "LV": 0.21, "LT": 0.21, "LU": 0.17,
    "MT": 0.18, "NL": 0.21, "PL": 0.23, "PT": 0.23, "RO": 0.21, "SK": 0.23, "SI": 0.22, "ES": 0.21, "SE": 0.25,
    # non-EU markets the simulator sells to
    "US": 0.0,   # no VAT; US sales tax is out of scope for the PoC
    "GB": 0.20,  # UK VAT is outside OSS and needs a separate HMRC registration
}
VAT_RATES["EL"] = VAT_RATES["GR"]  # Greece's code in EU VAT systems


def vat_rate(country: str) -> float:
    """Standard VAT rate for a customer country; unknown countries raise instead of defaulting to 0%."""
    try:
        return VAT_RATES[country.upper()]
    except KeyError:
        raise ValueError(f"no VAT rate for country {country}") from None


def vat_included(gross: float, country: str) -> float:
    """VAT contained in a VAT-inclusive price, rounded to cents."""
    return float(vat_split(gross, vat_rate(country))[1])


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
