"""crawl_site against a tiny mocked site: same-origin hard limit, robots.txt
disallow, sitemap ingestion, broken-link detection, and the page cap."""

from __future__ import annotations

import hashlib

import httpx
import respx

from app.collectors.crawler import crawl_site

BASE = "https://site.test"

ROBOTS = "User-agent: *\nDisallow: /private\n"
SITEMAP = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
    "<url><loc>https://site.test/</loc></url>"
    "<url><loc>https://site.test/about</loc></url>"
    "</urlset>"
)
HOME = (
    "<html><head><title>Home</title></head><body>"
    '<a href="/about">About</a>'
    '<a href="/private">Private</a>'
    '<a href="/missing">Missing</a>'
    '<a href="https://evil.example/">Evil</a>'
    "</body></html>"
)
ABOUT = '<html><head><title>About</title></head><body><a href="/">Home</a></body></html>'


def _mock_site(router: respx.MockRouter) -> dict[str, respx.Route]:
    return {
        "robots": router.get(f"{BASE}/robots.txt").respond(200, text=ROBOTS),
        "sitemap": router.get(f"{BASE}/sitemap.xml").respond(
            200, content=SITEMAP.encode(), headers={"content-type": "application/xml"}
        ),
        "home": router.route(url=f"{BASE}/").respond(200, html=HOME),
        "about": router.route(url=f"{BASE}/about").respond(200, html=ABOUT),
        "missing": router.route(url=f"{BASE}/missing").respond(404, html="<html><title>404</title></html>"),
        "private": router.route(url=f"{BASE}/private").respond(200, html="<html></html>"),
        "evil": router.route(host="evil.example").respond(200, html="<html></html>"),
    }


async def test_crawl_collects_pages_and_broken_links() -> None:
    with respx.mock(assert_all_called=False) as router:
        routes = _mock_site(router)
        async with httpx.AsyncClient() as client:
            result = await crawl_site(client, BASE)

    assert result.robots_txt_ok is True
    assert result.sitemap_ok is True
    assert result.sitemap_paths == ["/", "/about"]

    by_url = {page.url: page for page in result.pages}
    assert by_url[f"{BASE}/"].title == "Home"
    assert by_url[f"{BASE}/about"].status_code == 200
    assert by_url[f"{BASE}/missing"].status_code == 404
    assert f"{BASE}/private" not in by_url

    assert [(link.page_url, link.href, link.status) for link in result.broken_links] == [
        (f"{BASE}/", f"{BASE}/missing", 404)
    ]
    assert routes["private"].called is False
    assert routes["evil"].called is False


async def test_sitemap_hash_stable_across_crawls() -> None:
    expected = hashlib.sha256(SITEMAP.encode()).hexdigest()
    hashes: list[str] = []
    for _ in range(2):
        with respx.mock(assert_all_called=False) as router:
            _mock_site(router)
            async with httpx.AsyncClient() as client:
                result = await crawl_site(client, BASE)
        hashes.append(result.sitemap_hash)
    assert hashes == [expected, expected]


async def test_external_origin_never_requested() -> None:
    with respx.mock(assert_all_called=False) as router:
        routes = _mock_site(router)
        async with httpx.AsyncClient() as client:
            result = await crawl_site(client, BASE)

    assert routes["evil"].called is False
    assert all(page.url.startswith(f"{BASE}/") for page in result.pages)


async def test_robots_disallow_honored() -> None:
    with respx.mock(assert_all_called=False) as router:
        routes = _mock_site(router)
        async with httpx.AsyncClient() as client:
            result = await crawl_site(client, BASE)

    assert routes["private"].called is False
    assert f"{BASE}/private" not in {page.url for page in result.pages}


async def test_max_pages_cap_respected() -> None:
    with respx.mock(assert_all_called=False) as router:
        routes = _mock_site(router)
        async with httpx.AsyncClient() as client:
            result = await crawl_site(client, BASE, max_pages=1)

    assert len(result.pages) == 1
    assert result.pages[0].url == f"{BASE}/"
    # unfetched internal hrefs get a HEAD check within budget; robots still hold
    assert [(link.href, link.status) for link in result.broken_links] == [(f"{BASE}/missing", 404)]
    assert routes["private"].called is False
    assert routes["evil"].called is False


async def test_never_raises_on_total_failure() -> None:
    with respx.mock(assert_all_called=False) as router:
        router.route(host="site.test").mock(side_effect=httpx.ConnectError)
        async with httpx.AsyncClient() as client:
            result = await crawl_site(client, BASE)

    assert result.robots_txt_ok is False
    assert result.sitemap_ok is False
    assert [page.status_code for page in result.pages] == [0]  # "/" degraded, crawl survived
    assert result.broken_links == []
