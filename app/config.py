"""Settings for the SEO agent (env prefix ``SEO_AGENT_``).

Every external integration is opt-in: an empty credential/URL disables that
collector or action without failing the process, so the agent degrades to
whatever surface is configured (a bare crawl+audit works with zero secrets).
"""

from __future__ import annotations

from pydantic import field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # FastAPI bind (loop VM convention: overlay-bound, one port per service).
    host: str = "::"
    port: int = 8790
    environment: str = "development"

    # Site under management. The crawler is hard-limited to this origin.
    site_base_url: str = "https://hyrule.host"

    # Writable state root: SQLite db + the hyrule-web workspace checkout.
    data_dir: str = "/var/lib/seo-agent"

    # Fail-closed: no PR is pushed/opened unless ops flips this explicitly.
    dry_run: bool = True

    # Scheduler (disabled for one-shot seoctl runs and in tests).
    scheduler_enabled: bool = True
    audit_interval_s: int = 86400
    metrics_interval_s: int = 86400
    indexnow_interval_s: int = 21600
    draft_interval_s: int = 604800
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

    # LLM drafting via OpenRouter (model ids in config/seo-agent.toml).
    openrouter_api_key: str = ""
    model_policy_path: str = "config/seo-agent.toml"

    # GitHub App used to push branches + open draft PRs. All required to
    # enable non-dry-run PR opening.
    github_app_id: str = ""
    github_installation_id: str = ""
    github_private_key_path: str = ""
    github_repo: str = "AS215932/hyrule-web"
    git_remote_url: str = "https://github.com/AS215932/hyrule-web.git"
    git_default_branch: str = "main"

    # Guardrails (enforced in code, not prompts).
    max_open_prs: int = 2
    min_pr_interval_hours: int = 24
    max_diff_lines: int = 300
    # Comma-separated path prefixes a draft may touch inside hyrule-web.
    allowed_edit_paths: str = "hyrule_web/templates/,hyrule_web/seo.py"

    # Command run inside the workspace before any push; a non-zero exit
    # aborts the PR. Mirrors hyrule-web's own CI gate.
    workspace_validate_cmd: str = "uv sync -q && uv run ruff check hyrule_web && uv run python -m pytest -q"

    # Weekly summary. Empty → off.
    discord_webhook_url: str = ""

    model_config = {"env_prefix": "SEO_AGENT_"}

    @field_validator("site_base_url", "umami_base_url")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @property
    def psi_path_list(self) -> list[str]:
        return [p.strip() for p in self.psi_paths.split(",") if p.strip()]

    @property
    def allowed_edit_path_list(self) -> list[str]:
        return [p.strip() for p in self.allowed_edit_paths.split(",") if p.strip()]


settings = Settings()
