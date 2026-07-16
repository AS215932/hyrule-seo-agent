"""Stdlib HTML head/meta parsing → PageSnapshot.

Ported from hyrule-business ``hyrule_brand_loop.seo_audit._HTMLMetaParser`` and
adapted from file paths to live crawled pages: the crawler hands us
``(url, html)`` and gets back the typed snapshot the audit checks operate on.
Crawled HTML is untrusted input — this parser only ever extracts attribute/text
strings; nothing here is executed or re-fed to a model raw.
"""

from __future__ import annotations

from html.parser import HTMLParser


class _HTMLMetaParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_title = False
        self.title_parts: list[str] = []
        self.meta: dict[str, str] = {}
        self.links: list[dict[str, str]] = []
        self.script_types: list[str] = []
        self.anchors: list[str] = []

    @property
    def title(self) -> str:
        return "".join(self.title_parts).strip()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {key.lower(): value or "" for key, value in attrs}
        tag = tag.lower()
        if tag == "title":
            self.in_title = True
        if tag == "meta":
            key = attr.get("name") or attr.get("property")
            content = attr.get("content")
            if key and content:
                self.meta[key.lower()] = content
        if tag == "link":
            self.links.append(attr)
        if tag == "script" and attr.get("type"):
            self.script_types.append(attr["type"].lower())
        if tag == "a" and attr.get("href"):
            self.anchors.append(attr["href"])

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self.in_title = False

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_parts.append(data)


def parse_page(url: str, html: str, *, status_code: int = 200, fetched_ms: float = 0.0):
    """Parse one page's HTML into a PageSnapshot."""
    from app.models import PageSnapshot

    parser = _HTMLMetaParser()
    parser.feed(html)
    canonical = next(
        (link.get("href", "") for link in parser.links if link.get("rel", "").lower() == "canonical"),
        "",
    )
    og = {k: v for k, v in parser.meta.items() if k.startswith("og:")}
    twitter = {k: v for k, v in parser.meta.items() if k.startswith("twitter:")}
    return PageSnapshot(
        url=url,
        status_code=status_code,
        title=parser.title,
        meta_description=parser.meta.get("description", ""),
        canonical=canonical,
        robots_meta=parser.meta.get("robots", ""),
        og=og,
        twitter=twitter,
        has_json_ld="application/ld+json" in parser.script_types,
        internal_links=list(parser.anchors),
        html_bytes=len(html.encode("utf-8", errors="ignore")),
        fetched_ms=fetched_ms,
    )
