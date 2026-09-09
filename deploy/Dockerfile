# Clean-room image: pinned Python + Playwright Chromium, non-root user.
# Chromium OS libraries are installed with playwright's distro-correct tooling,
# browsers go into the non-root user's cache.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PIP_NO_CACHE_DIR=1

# OS libraries for Chromium (apt deps resolved for this distro by playwright).
RUN pip install --no-cache-dir "playwright==1.62.0" \
    && playwright install-deps chromium

# Non-root runtime user (Chromium refuses to run as root without --no-sandbox).
RUN useradd --create-home --uid 1000 botuser

WORKDIR /app
COPY pyproject.toml README.md config.yaml ./
COPY src ./src
RUN chown -R botuser:botuser /app

USER botuser
# Pin the same playwright version used to validate the code; browsers go to
# /home/botuser/.cache/ms-playwright.
RUN pip install --no-cache-dir -e . \
    && playwright install chromium

# Runtime state (.env, data/, config.yaml) is mounted from the host.
CMD ["python", "-u", "-m", "linkedin_bot.cli", "run"]
