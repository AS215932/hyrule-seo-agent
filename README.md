# Hyrule Beacon Worker

The repository and systemd service retain the operational names
`hyrule-seo-agent` and `seo-agent`; the product exposed to operators is Hyrule
Beacon.

The overlay-bound execution worker for [Hyrule Beacon](../hyrule-beacon). It
measures Hyrule's HTTP and x402 surfaces on the public indexes agents already
use, audits the evidence, optionally uses an LLM to prioritize findings, plans
policy-scoped improvements, and resumes approval-gated work from LangGraph
checkpoints.

There is no draft-PR loop and no private visibility index here. Git changes,
new registry submissions, publishing, wallet flows, paid forms, and community
messages are represented as exact Beacon actions; they do not happen merely
because the worker can describe them.

## Runtime shape

- FastAPI on `:8790` with `/health` and `/metrics`.
- One outbound managed loop leases runs from `/api/v1/beacon/worker/lease`.
- One LangGraph for both operator/API-triggered and proactively scheduled runs:
  `collect → audit → analyze → plan → approval → execute → report`.
- SQLite checkpointing under `SEO_AGENT_DATA_DIR` resumes the same `threadId`
  after a restart or operator decision.
- Existing GSC, PSI, Umami, crawl, IndexNow, reporting, and agent-core tracing
  remain available as evidence utilities.

The control plane owns schedules. That makes a scheduled run and a UI/API run
the same durable job instead of maintaining two automation implementations.

## Action boundary

- Read-only public measurement always runs.
- Only `indexnow.submit` has an installed automatic executor. It also requires
  `BEACON_EXECUTE_AUTOMATIC_ACTIONS=true` and a configured IndexNow key.
- Existing-listing synchronization is only eligible for automation after the
  control plane validates explicit ownership evidence.
- New registrations, publishing, and repository changes interrupt LangGraph
  and wait for approval of the exact action hash.
- Channels without an installed credentialed executor resume as explicit
  manual handoffs. Approval never adds a capability to the worker.
- Inaccessible channels are recorded as unknown, not absent and never as a
  fabricated rank.

## Local setup

```sh
uv sync --group dev
BEACON_CONTROL_PLANE_URL=http://localhost:5173 \
BEACON_WORKER_TOKEN='beacon_worker_…' \
SEO_AGENT_DATA_DIR=./data \
uv run seoctl beacon-once
```

For a continuously polling worker:

```sh
BEACON_MANAGED_MODE=true \
BEACON_CONTROL_PLANE_URL=http://localhost:5173 \
BEACON_WORKER_TOKEN='beacon_worker_…' \
SEO_AGENT_DATA_DIR=./data \
uv run hyrule-seo-agent
```

The full workflow can target production Beacon from a local worker by setting
`BEACON_CONTROL_PLANE_URL=https://beacon.hyrule.host` and using a dedicated,
revocable developer credential. Leave automatic actions disabled for routine
tests.

## Optional model analysis

Set `SEO_AGENT_OPENROUTER_API_KEY` to enable advisory prioritization. Model
output receives normalized findings, has no tools, cannot choose risk levels or
actions, and is ignored on failure. `config/seo-agent.toml` selects the model.

## Standalone evidence commands

```sh
SEO_AGENT_DATA_DIR=./data uv run seoctl run-audit
SEO_AGENT_DATA_DIR=./data uv run seoctl run-metrics
SEO_AGENT_DATA_DIR=./data uv run seoctl indexnow
SEO_AGENT_DATA_DIR=./data uv run seoctl status
```

## Verification

```sh
uv run ruff check .
uv run mypy app
uv run python -m pytest -q --cov
```

Production is deployed by `network-operations/ansible/roles/seo_agent` from an
immutable commit SHA. The service binds to Hyrule's overlay network and only
needs outbound HTTPS access to Beacon, owned surfaces, and measured public
channels.
