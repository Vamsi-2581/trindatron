FROM python:3.12-slim

# Install uv from the official image (pinned via digest for reproducibility)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Runtime optimizations for Docker.
# PLAYWRIGHT_BROWSERS_PATH: install Chromium to a fixed, non-$HOME location
# so it stays readable after the USER switch below (default would be
# /root/.cache/ms-playwright, inaccessible to a non-root runtime user).
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# Build context is the repo root so that local dependencies are accessible:
#   packages/core/pyproject.toml references ../model-router, ../sandbox, ../agent
WORKDIR /app

# Copy local dependency packages first (better layer caching)
COPY packages/model-router packages/model-router
COPY packages/sandbox      packages/sandbox
COPY packages/agent        packages/agent

# Copy the core backend
COPY packages/core packages/core

# Install production dependencies from the committed lockfile
WORKDIR /app/packages/core
RUN uv sync --no-dev --frozen

# Chromium + the Linux libraries it needs (nss, atk, gbm, fontconfig, ...).
# --with-deps resolves these via apt-get for the detected distro instead of
# an enumerated, easily-stale package list. Must run as root (apt-get) —
# before the USER switch below.
RUN uv run playwright install --with-deps chromium

# Non-root runtime user. Chromium's own internal sandbox refuses to start
# when the browser process is already running as root (root defeats the
# point of its namespace sandbox) — the correct fix is not running as root,
# not --no-sandbox. Same uid/pattern as the sandbox execution images under
# packages/core/docker/.
RUN useradd -m -u 1000 trinity \
    && chown -R trinity:trinity /app /ms-playwright
USER trinity

# Add virtualenv binaries to PATH
ENV PATH="/app/packages/core/.venv/bin:$PATH"

# PORT is injected by Railway at runtime; fallback to 8000 for local testing
EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && uvicorn src.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
