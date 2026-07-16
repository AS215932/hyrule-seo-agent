# seo-agent

AS215932 autonomous SEO agent for [hyrule.host](https://hyrule.host): collects
Search Console, PageSpeed Insights, self-crawl, and Umami signals; runs a
deterministic audit (ported from the hyrule-business brand loop); and turns
ranked findings into **human-reviewed draft PRs** against
[hyrule-web](https://github.com/AS215932/hyrule-web). It never merges.

## Shape

- FastAPI on `:8790` (`/health`, `/metrics`) with an in-process asyncio
  scheduler: crawl+audit daily, GSC/PSI/Umami daily, IndexNow check 6-hourly,
  draft weekly, Discord report weekly.
- State in SQLite under `SEO_AGENT_DATA_DIR` (findings, metric samples, run
  log, PR ledger, sitemap hash).
- agent-core `TraceEvent`s (graph_id `seo-agent`) to the loop collector —
  set `HYRULE_SEO_AGENT_CORE_TRACE=1` and `..._COLLECTOR_URL`.
- Deployed as a Docker container on the `loop` VM by
  `network-operations/ansible/roles/seo_agent` at a SHA pin.

## Guardrails (code, not prompts)

- `SEO_AGENT_DRY_RUN=true` by default — nothing is pushed or opened.
- PR-only: no merge capability exists; PRs are always drafts labelled
  `seo-agent`/`ai-generated`/`draft`.
- Budget: refuses when ≥2 seo-agent PRs are open or one was opened <24h ago.
- Drafts may only touch `hyrule_web/templates/` and `hyrule_web/seo.py`,
  ≤300 changed lines, each find-string unique in its file.
- hyrule-web's own ruff+pytest gate runs in the workspace before any push.
- Crawled pages / search queries are treated as untrusted data end-to-end;
  the drafting model has no tools and its output is validated mechanically.

## Local dry run

```sh
uv sync --group dev
SEO_AGENT_DATA_DIR=./data uv run seoctl run-audit   # crawl + audit hyrule.host
SEO_AGENT_DATA_DIR=./data uv run seoctl draft       # dry-run: prints the would-be PR
SEO_AGENT_DATA_DIR=./data uv run seoctl status
uv run ruff check . && uv run mypy app && uv run python -m pytest -q --cov
```

Related: `network-operations` (deploy + Vault runbook), `agentic-observatory`
(the `seo-agent` loop card), `hyrule-business` (origin of the audit lane).
