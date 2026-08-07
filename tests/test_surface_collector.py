"""fetch_surface against mocked origins: per-doc degradation, disabled
origins, redirect following, and JSON parsing."""

from __future__ import annotations

import httpx
import respx

from app.collectors.surface import fetch_surface

WEB = "https://site.test"
API = "https://api.site.test"
BAZAAR = "https://bazaar.test/discovery/resources"
UA = "hyrule-seo-agent/test"


async def _sweep(**kwargs: str) -> tuple[respx.MockRouter, object]:
    args = {"site_base_url": WEB, "api_base_url": API, "user_agent": UA, "bazaar_url": ""}
    args.update(kwargs)
    async with httpx.AsyncClient() as client:
        snapshot = await fetch_surface(client, **args)  # type: ignore[arg-type]
    return snapshot


async def test_full_sweep_populates_every_doc() -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{WEB}/llms.txt").respond(200, text="# hi")
        router.get(f"{WEB}/robots.txt").respond(200, text="User-agent: *\nAllow: /")
        # The brand origin serves its manifest as a redirect to the API origin;
        # the sweep must follow it and see the JSON.
        router.get(f"{WEB}/.well-known/x402.json").respond(
            302, headers={"location": f"{API}/.well-known/x402.json"}
        )
        router.get(f"{WEB}/.well-known/agent-card.json").respond(404)
        router.get(f"{WEB}/indexnow.txt").respond(404)
        router.get(f"{API}/.well-known/x402.json").respond(200, json={"x402Version": 2, "resources": []})
        router.get(f"{API}/.well-known/agent-card.json").respond(200, json={"name": "n"})
        router.get(f"{API}/openapi.json").respond(200, json={"paths": {}})
        router.get(f"{API}/robots.txt").respond(200, text="User-agent: *\nAllow: /")
        router.get(f"{API}/llms.txt").respond(200, text="# api")
        router.get(BAZAAR).respond(200, json={"items": []})
        snapshot = await _sweep(bazaar_url=BAZAAR)

    assert len(snapshot.docs()) == 11
    assert snapshot.web_x402 is not None and snapshot.web_x402.status_code == 200
    assert snapshot.web_x402.json_body == {"x402Version": 2, "resources": []}
    assert snapshot.web_agent_card is not None and snapshot.web_agent_card.status_code == 404
    assert snapshot.api_openapi is not None and snapshot.api_openapi.json_body == {"paths": {}}
    assert snapshot.bazaar is not None and snapshot.bazaar.json_body == {"items": []}


async def test_transport_failure_degrades_to_status_zero() -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{WEB}/llms.txt").mock(side_effect=httpx.ConnectError("down"))
        router.route().respond(200, text="ok")
        snapshot = await _sweep()

    assert snapshot.web_llms_txt is not None
    assert snapshot.web_llms_txt.status_code == 0
    # One doc failing must not stop the rest of the sweep.
    assert snapshot.web_robots_txt is not None and snapshot.web_robots_txt.status_code == 200


async def test_empty_api_base_url_skips_api_origin_entirely() -> None:
    with respx.mock(assert_all_called=False) as router:
        router.route(host="site.test").respond(200, text="ok")
        api_route = router.route(host="api.site.test").respond(200, text="never")
        snapshot = await _sweep(api_base_url="")

    assert api_route.called is False
    assert snapshot.api_x402 is None and snapshot.api_openapi is None
    assert snapshot.bazaar is None
    assert len(snapshot.docs()) == 5


async def test_non_json_body_leaves_json_body_none() -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{API}/openapi.json").respond(200, text="<html>oops</html>")
        router.route().respond(200, text="ok")
        snapshot = await _sweep()

    assert snapshot.api_openapi is not None
    assert snapshot.api_openapi.status_code == 200
    assert snapshot.api_openapi.json_body is None
    assert "<html>" in snapshot.api_openapi.text
