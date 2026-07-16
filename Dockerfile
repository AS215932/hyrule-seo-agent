# seo-agent service image.
# Built on the loop VM by ansible/roles/seo_agent (docker build at a pinned
# SHA), mirroring the agent-core-collector image pattern. git is required at
# runtime for the hyrule-web workspace the drafter edits and validates in;
# uv is kept in the image because that validation runs hyrule-web's own
# uv-based gate inside the container.
FROM python:3.14-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    UV_NO_CACHE=1 \
    PATH="/app/.venv/bin:${PATH}" \
    SEO_AGENT_HOST=:: \
    SEO_AGENT_PORT=8790 \
    SEO_AGENT_DATA_DIR=/var/lib/seo-agent

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.9.17 /uv /uvx /usr/local/bin/

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY config ./config
COPY app ./app
RUN uv sync --frozen --no-dev

EXPOSE 8790
# Runs as a named non-root user created by the Ansible role via --user; the
# state dir is a bind mount owned by that user.
ENTRYPOINT ["uvicorn", "app.main:app", "--host", "::", "--port", "8790"]
