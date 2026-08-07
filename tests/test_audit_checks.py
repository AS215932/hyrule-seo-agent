"""Tests for app.audit.checks over PageSnapshot/CrawlResult fixtures."""

from __future__ import annotations

from urllib.parse import urlsplit

from app.audit.checks import audit_crawl
from app.audit.html_meta import parse_page
from app.models import BrokenLink, CrawlResult, Finding, PageSnapshot, severity_rank

BASE = "https://hyrule.host"

# Mirrors what hyrule.host actually serves on "/".
CLEAN_HTML = """
<title>Hyrule Cloud — infrastructure for autonomous agents</title>
<meta name="description" content="Hyrule Cloud gives autonomous agents a machine-readable path to compute, domains, network intelligence, and egress on AS215932.">
<link rel="canonical" href="https://hyrule.host/">
<meta name="robots" content="index, follow, max-image-preview:large">
<meta property="og:title" content="t"><meta property="og:description" content="d">
<meta property="og:image" content="https://hyrule.host/static/og-image.png">
<meta name="twitter:title" content="t"><meta name="twitter:description" content="d">
<script type="application/ld+json">{}</script>
<a href="/services">s</a>
"""


def snap(path: str = "/", **overrides: object) -> PageSnapshot:
    """A snapshot that passes every per-page check unless overridden."""
    url = BASE + path
    fields: dict[str, object] = {
        "url": url,
        "status_code": 200,
        "title": "Hyrule Cloud — infrastructure for autonomous agents",
        "meta_description": (
            "Hyrule Cloud gives autonomous agents a machine-readable path to compute, "
            "domains, network intelligence, and egress on AS215932."
        ),
        "canonical": url,
        "robots_meta": "index, follow",
        "og": {"og:title": "t", "og:description": "d", "og:image": "i"},
        "twitter": {"twitter:title": "t", "twitter:description": "d"},
        "has_json_ld": True,
    }
    fields.update(overrides)
    return PageSnapshot(**fields)  # type: ignore[arg-type]


def crawl(pages: list[PageSnapshot], **overrides: object) -> CrawlResult:
    """A healthy crawl whose sitemap covers exactly the given pages."""
    fields: dict[str, object] = {
        "base_url": BASE,
        "pages": pages,
        "robots_txt_ok": True,
        "sitemap_ok": True,
        "sitemap_paths": [urlsplit(page.url).path or "/" for page in pages],
    }
    fields.update(overrides)
    return CrawlResult(**fields)  # type: ignore[arg-type]


def checks_of(findings: list[Finding]) -> set[tuple[str, str]]:
    return {(finding.check, finding.severity) for finding in findings}


def test_clean_page_produces_zero_findings() -> None:
    page = parse_page(f"{BASE}/", CLEAN_HTML)
    result = audit_crawl(crawl([page], sitemap_paths=["/"]), site_base_url=BASE)
    assert result == []


def test_bare_html_page_fires_all_meta_checks() -> None:
    page = parse_page(f"{BASE}/bare", "<p>no head at all</p>")
    findings = audit_crawl(crawl([page]), site_base_url=BASE)
    assert checks_of(findings) == {
        ("title", "error"),
        ("meta_description", "error"),
        ("social_meta", "warning"),
        ("canonical", "warning"),
        ("json_ld", "warning"),
    }
    assert all(finding.source == "audit" and finding.url == f"{BASE}/bare" for finding in findings)
    title = next(finding for finding in findings if finding.check == "title")
    assert title.message == "missing <title>"


def test_length_bands() -> None:
    low = snap("/low", title="Too short", meta_description="short desc")
    high = snap("/high", title="x" * 71, meta_description="d" * 181)
    edge = snap("/edge", title="t" * 70, meta_description="d" * 180)
    findings = audit_crawl(crawl([low, high, edge]), site_base_url=BASE)
    assert checks_of(findings) == {("title", "warning"), ("meta_description", "warning")}
    assert {finding.message for finding in findings} == {
        "title length is 9 chars",
        "title length is 71 chars",
        "meta description length is 10 chars",
        "meta description length is 181 chars",
    }
    assert not [finding for finding in findings if finding.url == f"{BASE}/edge"]


def test_social_meta_lists_missing_keys() -> None:
    page = snap("/", og={"og:title": "t"}, twitter={})
    findings = audit_crawl(crawl([page]), site_base_url=BASE)
    assert len(findings) == 1
    finding = findings[0]
    assert (finding.check, finding.severity) == ("social_meta", "warning")
    for key in ("og:description", "og:image", "twitter:title", "twitter:description"):
        assert key in finding.message
    assert "og:title" not in finding.message


def test_canonical_trailing_slash_insensitive() -> None:
    no_slash = snap("/", canonical=BASE)
    extra_slash = snap("/services", canonical=f"{BASE}/services/")
    findings = audit_crawl(crawl([no_slash, extra_slash]), site_base_url=BASE)
    assert findings == []


def test_canonical_mismatch_and_missing() -> None:
    mismatch = snap("/services", canonical=f"{BASE}/domains")
    missing = snap("/agents", canonical="")
    findings = audit_crawl(crawl([mismatch, missing]), site_base_url=BASE)
    assert checks_of(findings) == {("canonical", "warning")}
    by_url = {finding.url: finding for finding in findings}
    assert by_url[f"{BASE}/services"].message == "canonical mismatch"
    assert by_url[f"{BASE}/services"].evidence == {
        "canonical": f"{BASE}/domains",
        "expected": f"{BASE}/services",
    }
    assert by_url[f"{BASE}/agents"].message == "missing canonical link"


def test_robots_meta_noindex_is_error() -> None:
    page = snap("/", robots_meta="noindex, nofollow")
    findings = audit_crawl(crawl([page]), site_base_url=BASE)
    assert checks_of(findings) == {("robots_meta", "error")}


def test_http_status_and_no_meta_checks_on_non_200() -> None:
    gone = PageSnapshot(url=f"{BASE}/gone", status_code=404)
    failed = PageSnapshot(url=f"{BASE}/flaky", status_code=0)
    moved = PageSnapshot(url=f"{BASE}/moved", status_code=301)
    findings = audit_crawl(crawl([gone, failed, moved], sitemap_paths=[]), site_base_url=BASE)
    assert checks_of(findings) == {("http_status", "error"), ("http_status", "warning")}
    by_url = {finding.url: finding for finding in findings}
    assert by_url[f"{BASE}/gone"].severity == "error"
    assert by_url[f"{BASE}/gone"].evidence == {"status": 404}
    assert by_url[f"{BASE}/flaky"].severity == "warning"
    assert f"{BASE}/moved" not in by_url


def test_robots_txt_and_sitemap_errors() -> None:
    page = snap("/")
    findings = audit_crawl(
        crawl([page], robots_txt_ok=False, sitemap_ok=False, sitemap_paths=[]),
        site_base_url=BASE,
    )
    assert checks_of(findings) == {("robots_txt", "error"), ("sitemap", "error")}


def test_broken_link_error() -> None:
    page = snap("/")
    link = BrokenLink(page_url=f"{BASE}/", href="/nope", status=404)
    findings = audit_crawl(crawl([page], broken_links=[link]), site_base_url=BASE)
    assert checks_of(findings) == {("broken_link", "error")}
    finding = findings[0]
    assert finding.url == f"{BASE}/"
    assert "/nope" in finding.message
    assert f"{BASE}/" in finding.message
    assert finding.evidence == {"href": "/nope", "status": 404}


def test_sitemap_drift_flags_broken_sitemap_urls() -> None:
    ok = snap("/")
    gone = PageSnapshot(url=f"{BASE}/gone", status_code=404)
    findings = audit_crawl(crawl([ok, gone], sitemap_paths=["/", "/gone"]), site_base_url=BASE)
    drift = [finding for finding in findings if finding.check == "sitemap_drift"]
    assert len(drift) == 1
    assert drift[0].severity == "error"
    assert "sitemap lists a broken URL" in drift[0].message
    assert "/gone" in drift[0].message
    assert drift[0].url == f"{BASE}/gone"
    assert drift[0].evidence == {"path": "/gone", "status": 404}
    assert ("http_status", "error") in checks_of(findings)


def test_sitemap_drift_flags_pages_missing_from_sitemap() -> None:
    pages = [snap("/"), snap("/services"), snap("/domains")]
    findings = audit_crawl(crawl(pages, sitemap_paths=["/"]), site_base_url=BASE)
    drift = [finding for finding in findings if finding.check == "sitemap_drift"]
    assert len(drift) == 1
    assert drift[0].severity == "warning"
    assert drift[0].evidence == {"paths": ["/domains", "/services"]}
    assert "/domains" in drift[0].message
    assert "/services" in drift[0].message


def test_sitemap_drift_lists_at_most_ten_paths() -> None:
    pages = [snap("/"), *(snap(f"/p{i:02d}") for i in range(12))]
    findings = audit_crawl(crawl(pages, sitemap_paths=["/"]), site_base_url=BASE)
    drift = [finding for finding in findings if finding.check == "sitemap_drift"]
    assert len(drift) == 1
    assert drift[0].evidence == {"paths": [f"/p{i:02d}" for i in range(10)]}
    assert "2 more" in drift[0].message


def test_sitemap_drift_skipped_when_sitemap_not_ok() -> None:
    pages = [snap("/"), snap("/services")]
    findings = audit_crawl(crawl(pages, sitemap_ok=False, sitemap_paths=[]), site_base_url=BASE)
    assert checks_of(findings) == {("sitemap", "error")}


def test_ordering_is_deterministic_and_severity_sorted() -> None:
    pages = [
        snap("/", robots_meta="noindex"),
        snap("/services", canonical="", has_json_ld=False),
        PageSnapshot(url=f"{BASE}/gone", status_code=404),
        parse_page(f"{BASE}/bare", "<p></p>"),
    ]

    def build() -> CrawlResult:
        return crawl(
            pages,
            robots_txt_ok=False,
            sitemap_paths=["/", "/services", "/gone"],
            broken_links=[BrokenLink(page_url=f"{BASE}/", href="/dead", status=410)],
        )

    first = audit_crawl(build(), site_base_url=BASE)
    second = audit_crawl(build(), site_base_url=BASE)
    assert first == second
    assert first
    keys = [(severity_rank(finding.severity), finding.check, finding.url or "") for finding in first]
    assert keys == sorted(keys)
    assert all(finding.severity != "info" for finding in first)
    assert all(finding.source == "audit" for finding in first)


def test_non_html_resources_skip_meta_checks() -> None:
    """favicon/PNG/llms.txt are legitimately title-less (live-site regression)."""
    from app.models import CrawlResult, PageSnapshot

    crawl = CrawlResult(
        base_url="https://hyrule.host",
        robots_txt_ok=True,
        sitemap_ok=True,
        sitemap_paths=["/llms.txt"],
        pages=[
            PageSnapshot(
                url="https://hyrule.host/favicon.ico",
                status_code=200,
                content_type="image/x-icon",
            ),
            PageSnapshot(
                url="https://hyrule.host/llms.txt",
                status_code=200,
                content_type="text/plain; charset=utf-8",
            ),
        ],
    )
    from app.audit.checks import audit_crawl

    assert audit_crawl(crawl, site_base_url="https://hyrule.host") == []


def test_twitter_card_requires_image() -> None:
    # summary_large_image with no image at all → twitter:image joins the
    # missing-social list; og:image alone satisfies the card's fallback.
    no_image = snap(
        "/",
        og={"og:title": "t", "og:description": "d"},
        twitter={"twitter:title": "t", "twitter:description": "d", "twitter:card": "summary_large_image"},
    )
    findings = audit_crawl(crawl([no_image]), site_base_url=BASE)
    assert checks_of(findings) == {("social_meta", "warning")}
    assert "twitter:image" in findings[0].message and "og:image" in findings[0].message

    fallback = snap(
        "/",
        twitter={"twitter:title": "t", "twitter:description": "d", "twitter:card": "summary_large_image"},
    )
    assert audit_crawl(crawl([fallback]), site_base_url=BASE) == []
