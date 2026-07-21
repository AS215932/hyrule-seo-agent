"""Settings for the Hyrule Beacon worker (legacy env prefix ``SEO_AGENT_``).

Every external integration is opt-in: an empty credential/URL disables that
collector or action without failing the process, so the agent degrades to
whatever surface is configured (a bare crawl+audit works with zero secrets).
"""

from __future__ import annotations

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # FastAPI bind (loop VM convention: overlay-bound, one port per service).
    host: str = "::"
    port: int = 8790
    environment: str = "development"

    # Hyrule Beacon managed-worker connection. Direct aliases intentionally
    # omit SEO_AGENT_ so the credential contract is product-owned and can be
    # shared by local and deployed workers without legacy naming.
    beacon_control_plane_url: str = Field(
        default="",
        validation_alias=AliasChoices(
            "BEACON_CONTROL_PLANE_URL", "SEO_AGENT_BEACON_CONTROL_PLANE_URL"
        ),
    )
    beacon_worker_token: str = Field(
        default="",
        validation_alias=AliasChoices("BEACON_WORKER_TOKEN", "SEO_AGENT_BEACON_WORKER_TOKEN"),
    )
    beacon_managed_mode: bool = Field(
        default=False,
        validation_alias=AliasChoices("BEACON_MANAGED_MODE", "SEO_AGENT_BEACON_MANAGED_MODE"),
    )
    beacon_poll_interval_s: float = Field(
        default=10.0,
        validation_alias=AliasChoices(
            "BEACON_POLL_INTERVAL_S", "SEO_AGENT_BEACON_POLL_INTERVAL_S"
        ),
    )
    beacon_execute_automatic_actions: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "BEACON_EXECUTE_AUTOMATIC_ACTIONS",
            "SEO_AGENT_BEACON_EXECUTE_AUTOMATIC_ACTIONS",
        ),
    )

    # Site under management. The crawler is hard-limited to this origin.
    site_base_url: str = "https://hyrule.host"
    x402_base_url: str = "https://cloud.hyrule.host"
    skills_repository_url: str = "https://github.com/AS215932/hyrule-cloud"
    mcp_server_url: str = "https://github.com/AS215932/hyrule-cloud"

    # Writable state root: run data and LangGraph/SQLite checkpoints only.
    data_dir: str = "/var/lib/seo-agent"

    # Scheduler (disabled for one-shot seoctl runs and in tests).
    scheduler_enabled: bool = True
    audit_interval_s: int = 86400
    metrics_interval_s: int = 86400
    indexnow_interval_s: int = 21600
    report_interval_s: int = 604800

    # Crawler caps — this is our own site; stay polite anyway.
    crawl_max_pages: int = 60
    crawl_max_depth: int = 3
    crawl_timeout_s: float = 10.0
    user_agent: str = "hyrule-seo-agent/0.1 (+https://hyrule.host)"

    # Google Search Console (Domain property). Empty credentials path → off.
    # The WIF/service-account JSON is provisioned by ops at /etc/seo-agent/.
    gsc_site_url: str = "sc-domain:hyrule.host"
    google_credentials_path: str = ""

    # PageSpeed Insights v5. Empty key → off.
    psi_api_key: str = ""
    psi_paths: str = "/,/services,/domains,/agents"  # comma-separated

    # Self-hosted Umami. All three required to enable the collector.
    umami_base_url: str = ""
    umami_api_token: str = ""
    umami_website_id: str = ""

    # IndexNow: must equal hyrule-web's HYRULE_WEB_INDEXNOW_KEY so the
    # published key file at /indexnow.txt validates our pings. Empty → off.
    indexnow_key: str = ""

    # Optional advisory analysis via OpenRouter. Deterministic code still owns
    # proposals, risk classification, approvals, and execution capability.
    openrouter_api_key: str = ""
    model_policy_path: str = "config/seo-agent.toml"


    # Weekly summary. Empty → off.
    discord_webhook_url: str = ""

    model_config = {"env_prefix": "SEO_AGENT_", "populate_by_name": True}

    @field_validator(
        "site_base_url",
        "x402_base_url",
        "umami_base_url",
        "beacon_control_plane_url",
    )
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @property
    def psi_path_list(self) -> list[str]:
        return [p.strip() for p in self.psi_paths.split(",") if p.strip()]

    @property
    def beacon_configured(self) -> bool:
        return bool(self.beacon_control_plane_url and self.beacon_worker_token)


settings = Settings()
