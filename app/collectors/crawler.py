"""Polite same-origin BFS crawl of the managed site.

Hard constraints: only URLs on ``base_url``'s exact scheme+host are ever
requested; robots.txt disallows for our user agent are honored (page fetches
and link checks alike); total requests are capped at ``max_pages`` page
fetches plus a fixed link-check budget; timeouts belong to the caller's
``client``. ``crawl_site`` never raises — a failing page degrades to a
``status_code=0`` snapshot, a failing crawl to an empty ``CrawlResult``.
"""

from __future__ import annotations

import hashlib
import time
import urllib.parse
import urllib.robotparser
import xml.etree.ElementTree as ET
from collections import deque

import httpx
import structlog

from app.audit.html_meta import parse_page
from app.models import BrokenLink, CrawlResult, PageSnapshot

log = structlog.get_logger()

# HEAD link checks may add at most this many requests beyond ``max_pages``.
_LINK_CHECK_BUDGET = 20


def _sitemap_paths(raw: bytes) -> list[str]:
    """Path component of every ``<loc>``, tolerant of any (or no) namespace."""
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []
    paths: list[str] = []
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] == "loc" and element.text:
            paths.append(urllib.parse.urlsplit(element.text.strip()).path or "/")
    return paths


def _normalize(referrer: str, href: str, origin: tuple[str, str]) -> str | None:
    """Resolve ``href`` against ``referrer``, stripping fragment/query for
    dedup. Returns None for anything off our exact scheme+host — such URLs
    must never be fetched."""
    try:
        parts = urllib.parse.urlsplit(urllib.parse.urljoin(referrer, href))
    except ValueError:
        return None
    if (parts.scheme, parts.netloc) != origin:
        return None
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path or "/", "", ""))


async def crawl_site(
    client: httpx.AsyncClient,
    base_url: str,
    *,
    max_pages: int = 60,
    max_depth: int = 3,
    user_agent: str = "hyrule-seo-agent/0.1",
) -> CrawlResult:
    """BFS-crawl ``base_url`` from "/" plus its sitemap paths. Never raises."""
    try:
        return await _crawl(client, base_url, max_pages=max_pages, max_depth=max_depth, user_agent=user_agent)
    except Exception:
        log.warning("crawl_failed", base_url=base_url, exc_info=True)
        return CrawlResult(base_url=base_url)


async def _crawl(
    client: httpx.AsyncClient,
    base_url: str,
    *,
    max_pages: int,
    max_depth: int,
    user_agent: str,
) -> CrawlResult:
    base = base_url.rstrip("/")
    origin_parts = urllib.parse.urlsplit(base)
    origin = (origin_parts.scheme, origin_parts.netloc)
    headers = {"User-Agent": user_agent}
    result = CrawlResult(base_url=base_url)
    budget = max_pages + _LINK_CHECK_BUDGET
    requests_made = 0

    robots = urllib.robotparser.RobotFileParser()
    robots_lines: list[str] = []
    try:
        requests_made += 1
        response = await client.get(f"{base}/robots.txt", headers=headers)
        if response.status_code == 200 and response.text.strip():
            result.robots_txt_ok = True
            robots_lines = response.text.splitlines()
    except Exception:
        log.warning("robots_fetch_failed", base_url=base_url, exc_info=True)
    robots.parse(robots_lines)  # empty lines → allow-all

    try:
        requests_made += 1
        response = await client.get(f"{base}/sitemap.xml", headers=headers)
        if response.status_code == 200:
            result.sitemap_ok = True
            result.sitemap_hash = hashlib.sha256(response.content).hexdigest()
            result.sitemap_paths = _sitemap_paths(response.content)
    except Exception:
        log.warning("sitemap_fetch_failed", base_url=base_url, exc_info=True)

    queue: deque[tuple[str, int]] = deque()
    seen: set[str] = set()
    for path in ["/", *result.sitemap_paths]:
        url = _normalize(f"{base}/", path, origin)
        if url is not None and url not in seen:
            seen.add(url)
            queue.append((url, 0))

    fetched: dict[str, int] = {}  # normalized url → observed status (0 = transport failure)
    first_ref: dict[str, str] = {}  # normalized internal href → first page referencing it

    while queue and len(result.pages) < max_pages:
        url, depth = queue.popleft()
        if not robots.can_fetch(user_agent, url):
            continue
        requests_made += 1
        started = time.monotonic()
        try:
            response = await client.get(url, headers=headers)
        except Exception:
            log.warning("page_fetch_failed", url=url, exc_info=True)
            result.pages.append(PageSnapshot(url=url, status_code=0))
            fetched[url] = 0
            continue
        elapsed_ms = (time.monotonic() - started) * 1000.0
        fetched[url] = response.status_code
        content_type = response.headers.get("content-type", "")
        if content_type and "html" not in content_type.lower():
            result.pages.append(
                PageSnapshot(
                    url=url,
                    status_code=response.status_code,
                    content_type=content_type,
                    fetched_ms=elapsed_ms,
                )
            )
            continue
        snapshot = parse_page(url, response.text, status_code=response.status_code, fetched_ms=elapsed_ms)
        snapshot.content_type = content_type or "text/html"
        result.pages.append(snapshot)
        for href in snapshot.internal_links:
            link = _normalize(url, href, origin)
            if link is None:  # off-origin: never queued, never checked
                continue
            first_ref.setdefault(link, url)
            if link not in seen and depth < max_depth:
                seen.add(link)
                queue.append((link, depth + 1))

    for href, referrer in first_ref.items():
        if href in fetched:
            if fetched[href] >= 400:
                result.broken_links.append(BrokenLink(page_url=referrer, href=href, status=fetched[href]))
            continue
        if requests_made >= budget or not robots.can_fetch(user_agent, href):
            continue
        requests_made += 1
        try:
            response = await client.head(href, headers=headers)
        except Exception:
            log.warning("link_check_failed", href=href, exc_info=True)
            continue
        if response.status_code >= 400:
            result.broken_links.append(BrokenLink(page_url=referrer, href=href, status=response.status_code))

    return result
