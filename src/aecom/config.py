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
    # Anthropic server-side web search for agents whose TOOLS.md lists it under server_tools.
    # _20260209 (dynamic filtering) is the current version for the Claude 5.5 models; use web_search_20250305 on
    # older models or on Vertex AI.
    web_search_tool_type: str = "web_search_20260209"
    web_search_max_uses: int = 5        # per API call (the API's own max_uses)
    web_search_max_per_run: int = 15    # per agent run: once reached, web_search is unbound for the remaining steps
    # Billed per search on top of tokens: $10 per 1,000 searches, i.e. ~EUR 0.01 each
    web_search_price_eur: float = 0.01
    # The API takes allowed_domains or blocked_domains, never both: when both are set, allowed wins.
    web_search_allowed_domains: list[str] = []
    web_search_blocked_domains: list[str] = []
    # Anthropic server-side web fetch: pages are fetched on Anthropic's side, never from this host (no beta header)
    web_fetch_tool_type: str = "web_fetch_20260209"
    web_fetch_max_uses: int = 5
    web_fetch_max_content_tokens: int = 8000  # cap on the tokens one fetched page may add to the context
    web_fetch_price_eur: float = 0.0          # no per-use fee: fetched content is billed as ordinary input tokens
    web_fetch_allowed_domains: list[str] = []
    web_fetch_blocked_domains: list[str] = []

    # --- Data ----------------------------------------------------------------
    # SQLite by default so the PoC runs anywhere; set to postgresql+psycopg://... for docker-compose.
    database_url: str = f"sqlite:///{ROOT / 'data' / 'aecom.db'}"

    # Backups (aecom backup): directory, and an optional off-device copy (local path or user@host:/path via rsync)
    backup_dir: Path = ROOT / "data" / "backups"
    backup_target: str | None = None
    # Customer download route (GET /download/{order_id}?token=...): off until the API has a public URL
    download_route_enabled: bool = False
    # Etsy second sales channel (off by default; needs the Etsy connection under Operations > Connections)
    etsy_enabled: bool = False
    etsy_shop_id: str = ""
    # Optional read-only browser (Playwright MCP): per-run budgets enforced by aecom.browser_guard
    browser_max_calls: int = 20
    browser_max_navigations: int = 5
    browser_max_calls_per_domain: int = 10
    # Accountant export (DATEV): numbers your accountant gives you; empty is fine for a first draft
    datev_berater_nr: str = ""
    datev_mandant_nr: str = ""
    datev_wj_beginn: str = ""  # financial year start as YYYYMMDD; empty = 1 January of the exported year
    downloads_dir: Path = ROOT / "data" / "downloads"

    # --- Integrations --------------------------------------------------------
    agentmail_api_key: str | None = None
    agentmail_base_url: str = "https://api.agentmail.to/v0"
    agentmail_org_inbox: str = "enrgy@agentmail.to"
    # Real sends only when explicitly enabled — seed/demo customers are fake addresses.
    agentmail_live: bool = False
    store_adapter: str = "mock"
    # Lemon Squeezy (STORE_ADAPTER=lemonsqueezy). The API key and webhook secret are read via aecom.secrets.
    lemonsqueezy_base_url: str = "https://api.lemonsqueezy.com/v1"
    lemonsqueezy_store_id: str | None = None
    lemonsqueezy_currency: str = "EUR"  # the store currency; orders in another currency are refused, not converted
    lemonsqueezy_timeout_s: float = 20.0
    # Where automatic restocks get licence keys: "mock" mints fake keys (demo only), "none" = manual keys only
    license_key_source: str = "mock"
    # I13: download links of the built-in store, {download_base_url}/{order_id}/{token}, valid for N days
    download_base_url: str = "https://downloads.example"
    download_link_days: int = 7
    mcp_autoload: bool = True          # load registry MCP servers into agents' toolbelts
    mcp_load_timeout_s: float = 20.0

    # --- API -----------------------------------------------------------------
    api_base_url: str = "http://127.0.0.1:8000"

    # --- Chat and owner messaging (all editable in the dashboard: Settings) ----
    chat_agents: list[str] = ["ceo", "finance", "researcher", "sentinel", "foreman"]
    chat_history_turns: int = 12          # last N turns sent to the model; older ones are folded into a summary
    chat_daily_cap_share: float = 0.5     # chat may use at most this share of an agent's daily budget
    chat_retention_days: int = 90         # conversations idle longer than this are deleted (0 = keep forever)
    chat_max_attachment_mb: int = 5
    owner_message_daily_limit: int = 5    # message_owner calls per agent per day
    quiet_hours_start: str = ""           # "22:00"; empty = no quiet hours (P1 alerts ignore them)
    quiet_hours_end: str = ""
    dashboard_idle_hours: float = 12.0    # a dashboard session is signed out after this long idle
    telegram_enabled: bool = False        # bot token is the secret TELEGRAM_BOT_TOKEN
    whatsapp_enabled: bool = False        # WHATSAPP_* secrets; needs a public webhook (Cloudflare Tunnel)

    # --- Backups: started and scheduled from the dashboard (the scheduler runs them) ----
    backup_enabled: bool = True
    backup_schedule: str = "cron:15 3 * * *"
    backup_verify_after: bool = True
    backup_keep_daily: int = 7
    backup_keep_weeks: int = 4
    backup_keep_labelled: int = 5

    # --- Job schedules (see aecom.schedule_spec: "30s", "15m", "6h" or "cron:M H DOM MON DOW") ----
    schedule_drain: str = "30s"
    schedule_stock_check: str = "60m"
    schedule_reconcile: str = "15m"
    schedule_monitors: str = "5m"
    schedule_provisioning: str = "15m"
    schedule_expire_approvals: str = "30m"
    schedule_sync_store_catalog: str = "6h"
    schedule_poll_email: str = "60s"
    schedule_poll_telegram: str = "10s"
    schedule_finance_daily: str = "cron:0 7 * * *"
    schedule_weekly_review: str = "cron:0 8 * * 1"
    schedule_governance_report: str = "cron:30 7 1 * *"

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

    def web_cost_eur(self, web_searches: int, web_fetches: int) -> float:
        """Per-use fees of the Anthropic server tools, on top of the token cost."""
        return web_searches * self.web_search_price_eur + web_fetches * self.web_fetch_price_eur


@lru_cache
def get_settings() -> Settings:
    return Settings()
