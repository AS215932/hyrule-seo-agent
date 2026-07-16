"""Deterministic SEO checks over one live crawl of the managed site.

Ported from hyrule-business ``hyrule_brand_loop.seo_audit``: the per-page
title/description length bands (10-70 / 50-180), social-meta key set, and
canonical/JSON-LD severity semantics are unchanged. Input moved from files on
disk to ``CrawlResult``/``PageSnapshot`` (parsed by ``html_meta``), which adds
live-only checks: HTTP status, robots/sitemap fetch health, crawler-verified
broken links, and sitemap drift. Unlike the brand loop, no info-level findings
are emitted: every finding is actionable.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from app.models import CrawlResult, Finding, PageSnapshot, Severity, severity_rank

# Length bands identical to hyrule_brand_loop.seo_audit.
_TITLE_BAND = (10, 70)
_DESCRIPTION_BAND = (50, 180)
# og:image is required on top of the brand loop's four text keys: every
# hyrule.host page ships a share card.
_SOCIAL_KEYS = ("og:title", "og:description", "og:image", "twitter:title", "twitter:description")
# The drift warning must stay one readable finding; the full path set is
# recoverable from the crawl itself.
_DRIFT_PATHS_SHOWN = 10


def _path(url: str) -> str:
    # urlsplit("https://x").path == "" — an empty path is the site root.
    return urlsplit(url).path or "/"


def _finding(
    check: str,
    severity: Severity,
    message: str,
    *,
    url: str | None = None,
    evidence: dict[str, Any] | None = None,
) -> Finding:
    return Finding(check=check, severity=severity, message=message, url=url, source="audit", evidence=evidence or {})


def _http_status_findings(page: PageSnapshot) -> list[Finding]:
    if page.status_code >= 400:
        return [
            _finding(
                "http_status",
                "error",
                f"page returned HTTP {page.status_code}",
                url=page.url,
                evidence={"status": page.status_code},
            )
        ]
    if page.status_code == 0:
        return [_finding("http_status", "warning", "page fetch failed", url=page.url, evidence={"status": 0})]
    return []


def _meta_findings(page: PageSnapshot, base: str) -> list[Finding]:
    """Head/meta checks; only meaningful for pages that served HTML with 200."""
    out: list[Finding] = []

    if not page.title:
        out.append(_finding("title", "error", "missing <title>", url=page.url))
    elif not _TITLE_BAND[0] <= len(page.title) <= _TITLE_BAND[1]:
        out.append(_finding("title", "warning", f"title length is {len(page.title)} chars", url=page.url))

    description = page.meta_description
    if not description:
        out.append(_finding("meta_description", "error", "missing meta description", url=page.url))
    elif not _DESCRIPTION_BAND[0] <= len(description) <= _DESCRIPTION_BAND[1]:
        out.append(
            _finding(
                "meta_description",
                "warning",
                f"meta description length is {len(description)} chars",
                url=page.url,
            )
        )

    social = {**page.og, **page.twitter}
    missing_social = [key for key in _SOCIAL_KEYS if key not in social]
    if missing_social:
        out.append(
            _finding("social_meta", "warning", "missing social metadata: " + ", ".join(missing_social), url=page.url)
        )

    # Canonical must point at this exact page on the managed origin; a bare
    # trailing-slash difference is not a real mismatch.
    expected = base + _path(page.url)
    if not page.canonical:
        out.append(_finding("canonical", "warning", "missing canonical link", url=page.url))
    elif page.canonical.rstrip("/") != expected.rstrip("/"):
        out.append(
            _finding(
                "canonical",
                "warning",
                "canonical mismatch",
                url=page.url,
                evidence={"canonical": page.canonical, "expected": expected},
            )
        )

    if not page.has_json_ld:
        out.append(_finding("json_ld", "warning", "missing JSON-LD structured data", url=page.url))

    # A noindex on the live site silently removes the page from search.
    if "noindex" in page.robots_meta.lower():
        out.append(_finding("robots_meta", "error", "robots meta contains noindex", url=page.url))

    return out


def _sitemap_drift_findings(crawl: CrawlResult, base: str) -> list[Finding]:
    """Sitemap vs crawl reality, both directions."""
    out: list[Finding] = []
    status_by_path: dict[str, int] = {}
    for page in crawl.pages:
        status_by_path.setdefault(_path(page.url), page.status_code)

    sitemap_paths = {path or "/" for path in crawl.sitemap_paths}

    for path in sorted(sitemap_paths):
        status = status_by_path.get(path)
        if status is not None and status >= 400:
            out.append(
                _finding(
                    "sitemap_drift",
                    "error",
                    f"sitemap lists a broken URL: {path}",
                    url=base + path,
                    evidence={"path": path, "status": status},
                )
            )

    # Reverse direction needs a trustworthy sitemap; a failed fetch would flag
    # every page. Non-empty title is the HTML proxy (assets have none).
    if crawl.sitemap_ok:
        missing = sorted(
            {
                _path(page.url)
                for page in crawl.pages
                if page.status_code == 200 and page.title and _path(page.url) not in sitemap_paths
            }
        )
        if missing:
            shown = missing[:_DRIFT_PATHS_SHOWN]
            message = "crawled pages missing from sitemap: " + ", ".join(shown)
            if len(missing) > _DRIFT_PATHS_SHOWN:
                message += f" and {len(missing) - _DRIFT_PATHS_SHOWN} more"
            out.append(_finding("sitemap_drift", "warning", message, evidence={"paths": shown}))

    return out


def audit_crawl(crawl: CrawlResult, *, site_base_url: str) -> list[Finding]:
    """Run every deterministic check over one crawl.

    Output order is stable — (severity, check, url) — so identical crawls
    produce byte-identical finding lists and stores can diff runs.
    """
    base = site_base_url.rstrip("/")
    findings: list[Finding] = []

    for page in crawl.pages:
        findings.extend(_http_status_findings(page))
        # Meta checks apply to HTML documents only — favicons, images, and
        # text artifacts like llms.txt are legitimately title-less. An empty
        # content_type (fixtures, failed fetches) is treated as HTML.
        is_html = not page.content_type or "html" in page.content_type.lower()
        if page.status_code == 200 and is_html:
            findings.extend(_meta_findings(page, base))

    if not crawl.robots_txt_ok:
        findings.append(_finding("robots_txt", "error", "robots.txt missing or invalid"))
    if not crawl.sitemap_ok:
        findings.append(_finding("sitemap", "error", "sitemap.xml missing or invalid"))

    for link in crawl.broken_links:
        findings.append(
            _finding(
                "broken_link",
                "error",
                f"broken link {link.href} on {link.page_url}",
                url=link.page_url,
                evidence={"href": link.href, "status": link.status},
            )
        )

    findings.extend(_sitemap_drift_findings(crawl, base))

    findings.sort(key=lambda f: (severity_rank(f.severity), f.check, f.url or ""))
    return findings
