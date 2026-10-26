"""Runtime configuration. Everything is overridable through environment variables / `.env`."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore")

    # --- LLM -----------------------------------------------------------------
    anthropic_api_key: str | None = None
    # "anthropic" = real models, "scripted" = deterministic offline model (tests / demos without a key)
    llm_mode: str = "anthropic"
    model_opus: str = "claude-opus-5-5"
    model_sonnet: str = "claude-sonnet-5-5"
    model_haiku: str = "claude-haiku-5-5"
    agent_max_steps: int = 12
    # USD per million tokens (input, output) — set to your contract prices; used for cost dashboards & budgets.
    price_opus: tuple[float, float] = (5.0, 25.0)
    price_sonnet: tuple[float, float] = (3.0, 15.0)
    price_haiku: tuple[float, float] = (1.0, 5.0)
    usd_to_eur: float = 0.92
    # Anthropic server-side web search for agents whose TOOLS.md lists it under server_tools
    web_search_tool_type: str = "web_search_20250305"
    # Anthropic server-side web fetch: pages are fetched on Anthropic's side, never from this host (no beta header)
    web_fetch_tool_type: str = "web_fetch_20260209"
    web_fetch_max_uses: int = 5

    # --- Data ----------------------------------------------------------------
    # SQLite by default so the PoC runs anywhere; set to postgresql+psycopg://... for docker-compose.
    database_url: str = f"sqlite:///{ROOT / 'data' / 'aecom.db'}"

    # --- Integrations --------------------------------------------------------
    agentmail_api_key: str | None = None
    agentmail_base_url: str = "https://api.agentmail.to/v0"
    agentmail_org_inbox: str = "enrgy@agentmail.to"
    # Real sends only when explicitly enabled — seed/demo customers are fake addresses.
    agentmail_live: bool = False
    store_adapter: str = "mock"
    mcp_autoload: bool = True          # load registry MCP servers into agents' toolbelts
    mcp_load_timeout_s: float = 20.0

    # --- API -----------------------------------------------------------------
    api_base_url: str = "http://127.0.0.1:8000"

    # Paths
    agents_dir: Path = ROOT / "agents"
    company_dir: Path = ROOT / "company"
    decisions_dir: Path = ROOT / "docs" / "decisions"
    policy_file: Path = Path(__file__).resolve().parent / "policy" / "policy.yaml"

    def model_for_tier(self, tier: str) -> str:
        return {"opus": self.model_opus, "sonnet": self.model_sonnet, "haiku": self.model_haiku}[tier]

    def cost_eur(self, tier: str, input_tokens: int, output_tokens: int) -> float:
        pin, pout = {"opus": self.price_opus, "sonnet": self.price_sonnet, "haiku": self.price_haiku}[tier]
        return (input_tokens * pin + output_tokens * pout) / 1_000_000 * self.usd_to_eur


@lru_cache
def get_settings() -> Settings:
    return Settings()
