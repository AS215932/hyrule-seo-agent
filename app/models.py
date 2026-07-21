"""Typed payloads shared across collectors, audit, and actions."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, Field

Severity = Literal["info", "warning", "error"]
Source = Literal["crawl", "audit", "gsc", "psi", "umami", "agent"]

_SEVERITY_ORDER: dict[str, int] = {"error": 0, "warning": 1, "info": 2}


def severity_rank(severity: str) -> int:
    """Sort key: errors first."""
    return _SEVERITY_ORDER.get(severity, 3)


class Finding(BaseModel):
    """One audit/metric finding. ``fingerprint`` is stable across runs so the
    store can dedupe and track first/last seen."""

    fingerprint: str = ""
    check: str
    severity: Severity
    message: str
    url: str | None = None
    source: Source = "audit"
    evidence: dict[str, Any] = Field(default_factory=dict)

    def model_post_init(self, __context: Any) -> None:
        if not self.fingerprint:
            raw = json.dumps([self.check, self.url or "", self.message], sort_keys=True)
            self.fingerprint = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class PageSnapshot(BaseModel):
    """Parsed head/meta state of one crawled page."""

    url: str
    status_code: int = 0
    # Response content-type; HTML meta checks skip non-HTML resources
    # (favicon/PNG/llms.txt are legitimately title-less). Empty means unknown
    # (fixture-built or fetch-failed snapshots) and is treated as HTML.
    content_type: str = ""
    title: str = ""
    meta_description: str = ""
    canonical: str = ""
    robots_meta: str = ""
    og: dict[str, str] = Field(default_factory=dict)
    twitter: dict[str, str] = Field(default_factory=dict)
    has_json_ld: bool = False
    internal_links: list[str] = Field(default_factory=list)
    html_bytes: int = 0
    fetched_ms: float = 0.0


class BrokenLink(BaseModel):
    page_url: str
    href: str
    status: int | None = None


class CrawlResult(BaseModel):
    """Everything one polite same-origin crawl learned."""

    base_url: str
    pages: list[PageSnapshot] = Field(default_factory=list)
    broken_links: list[BrokenLink] = Field(default_factory=list)
    robots_txt_ok: bool = False
    sitemap_ok: bool = False
    sitemap_paths: list[str] = Field(default_factory=list)
    sitemap_hash: str = ""


class MetricSample(BaseModel):
    """One numeric observation from a collector."""

    source: str
    metric: str
    key: str = ""
    value: float
