"""GitHub App auth + draft-PR opening against hyrule-web.

Ported from hyrule-business ``hyrule_brand_loop.github_pr`` with the gh-CLI
dependency replaced by direct REST calls using a GitHub App installation
token (the loop VM has no interactive gh login).

Fail-closed by design: there is deliberately NO merge capability in this
module, PRs are always opened as drafts, and callers must pass an explicit
``dry_run=False`` (wired to settings.dry_run) for anything to leave the box.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import structlog

from app.config import Settings

log = structlog.get_logger()

_API = "https://api.github.com"
PR_LABELS = ["seo-agent", "ai-generated", "draft"]
BRANCH_PREFIX = "seo-agent/"


def app_configured(settings: Settings) -> bool:
    return bool(
        settings.github_app_id
        and settings.github_installation_id
        and settings.github_private_key_path
        and Path(settings.github_private_key_path).exists()
    )


def _app_jwt(settings: Settings) -> str:
    import jwt  # pyjwt[crypto]

    key = Path(settings.github_private_key_path).read_text(encoding="utf-8")
    now = int(time.time())
    return jwt.encode(
        {"iat": now - 60, "exp": now + 540, "iss": settings.github_app_id},
        key,
        algorithm="RS256",
    )


async def installation_token(client: httpx.AsyncClient, settings: Settings) -> str:
    """Mint a short-lived installation token. Raises on failure — callers
    treat a token failure as 'PR opening unavailable this cycle'."""
    resp = await client.post(
        f"{_API}/app/installations/{settings.github_installation_id}/access_tokens",
        headers={
            "Authorization": f"Bearer {_app_jwt(settings)}",
            "Accept": "application/vnd.github+json",
        },
    )
    resp.raise_for_status()
    return str(resp.json()["token"])


async def open_agent_pr_state(
    client: httpx.AsyncClient, settings: Settings, token: str
) -> tuple[int, datetime | None]:
    """(count of open seo-agent PRs, most recent creation time) on the target repo."""
    resp = await client.get(
        f"{_API}/repos/{settings.github_repo}/pulls",
        params={"state": "open", "per_page": 100},
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
    )
    resp.raise_for_status()
    ours = [
        pr
        for pr in resp.json()
        if str(pr.get("head", {}).get("ref", "")).startswith(BRANCH_PREFIX)
    ]
    latest: datetime | None = None
    for pr in ours:
        created = pr.get("created_at")
        if created:
            stamp = datetime.fromisoformat(str(created).replace("Z", "+00:00")).astimezone(UTC)
            if latest is None or stamp > latest:
                latest = stamp
    return len(ours), latest


async def open_draft_pr(
    client: httpx.AsyncClient,
    settings: Settings,
    token: str,
    *,
    branch: str,
    title: str,
    body: str,
) -> str:
    """Open a draft PR for an already-pushed branch; return its html_url."""
    resp = await client.post(
        f"{_API}/repos/{settings.github_repo}/pulls",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        json={
            "title": title,
            "body": body,
            "head": branch,
            "base": settings.git_default_branch,
            "draft": True,
        },
    )
    resp.raise_for_status()
    pr: dict[str, Any] = resp.json()
    number = pr["number"]
    label_resp = await client.post(
        f"{_API}/repos/{settings.github_repo}/issues/{number}/labels",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        json={"labels": PR_LABELS},
    )
    if label_resp.status_code >= 400:  # labels are cosmetic; the PR already exists
        log.warning("pr_label_failed", status=label_resp.status_code, pr=number)
    return str(pr["html_url"])


def render_pr_body(
    *,
    intent: str,
    changed_files: list[str],
    evidence: list[str],
    model_transparency: str,
) -> str:
    """Required draft-PR body (ported verbatim in structure from the brand loop)."""
    changed = "\n".join(f"- `{path}`" for path in changed_files) or "- _No files listed yet_"
    evidence_lines = "\n".join(f"- {item}" for item in evidence) or "- _No citations listed yet_"
    return f"""## Intent

{intent}

## Changed files

{changed}

## Evidence / citations

{evidence_lines}

## Model transparency

{model_transparency}

## Human review checklist

- [ ] Factual claims are source-backed (Block G: never advertise a feature that isn't live).
- [ ] No sensitive infrastructure details are exposed.
- [ ] This remains a draft PR until human review is complete.

**Do not merge without human review.**
"""
