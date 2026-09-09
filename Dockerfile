# syntax=docker/dockerfile:1
# linkin-bot: capped, human-paced LinkedIn connection bot — a long-lived
# service (FastAPI dashboard on :8080 + built-in daily schedule).
# Build: docker compose build      Run: docker compose up -d
#
# Rebuilds are fast: pip's downloaded wheels live in a BuildKit cache mount
# (~/.cache/pip) shared across builds, so `docker compose build` only
# re-fetches a dependency when pyproject.toml actually changes.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Chromium OS libraries, resolved for this distro by playwright's tooling.
# Pinned to the playwright version the code was validated against.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install "playwright==1.62.0" \
    && playwright install-deps chromium

# Install the package first (layers well): metadata + src only, no data/.
COPY pyproject.toml README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -e ".[server]"

# Runtime config; keep it a thin layer so overriding it is a cheap bind mount.
COPY config.yaml ./

# Non-root runtime user. uid 10001 matches the other bots; make ./data
# writable by this uid on the host once: sudo chown -R 10001:10001 data
# HOME=/app so the Chromium download at build time lands under /app/.cache.
RUN useradd --system --uid 10001 --home-dir /app linkinbot \
    && chown -R linkinbot:linkinbot /app

ENV HOME=/app

USER linkinbot
RUN playwright install chromium

# Runtime state (data/, config.yaml) is mounted from the host.
# `serve` = dashboard + scheduled runs; override with one-shot CLI commands:
#   docker compose run --rm linkin-bot run --limit 3
ENTRYPOINT ["linkedin-bot"]
CMD ["serve"]
