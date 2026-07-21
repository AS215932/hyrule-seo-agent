# Hyrule Beacon worker image.
# Built on the loop VM by ansible/roles/seo_agent at an immutable source SHA,
# mirroring the agent-core-collector image pattern. The worker has no Git
# credentials or repository mutation path: publishing and source changes stay
# approval-gated Beacon handoffs.
FROM python:3.14-slim AS builder

ENV UV_NO_CACHE=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.9.17 /uv /uvx /usr/local/bin/

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY config ./config
COPY app ./app
RUN uv sync --frozen --no-dev

FROM python:3.14-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:${PATH}" \
    SEO_AGENT_HOST=:: \
    SEO_AGENT_PORT=8790 \
    SEO_AGENT_DATA_DIR=/var/lib/seo-agent

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=builder /app /app

EXPOSE 8790
# The deployment role supplies a non-root UID and a writable state mount.
ENTRYPOINT ["seo-agent"]
