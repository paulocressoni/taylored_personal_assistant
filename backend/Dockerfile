# ---------- STAGE 1: builder ----------
# Heavy work (installing deps) happens here so it can be discarded later.
FROM python:3.12-slim AS builder

# The official way to get uv into the image (no separate install step).
COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /bin/uv

WORKDIR /app

# --- Layer-cache strategy starts HERE ---
# Only the dependency manifests are copied. NOT the source code.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
# --- any edit to pyproject.toml/uv.lock invalidates this layer only ---

# ---------- STAGE 2: runtime ----------
# Slim: only the venv + source, no build tooling.
FROM python:3.12-slim AS runtime

WORKDIR /app

# Copy the finished virtualenv from the builder (no reinstall).
COPY --from=builder /app/.venv /app/.venv

# Copy ONLY the application source (this is the layer that changes often).
COPY app/ ./app/

ENV PATH="/app/.venv/bin:$PATH"

# Install curl for healthcheck and clean up apt cache to reduce image size
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

# Healthcheck to ensure the application is running
# TODO: One improvement: add --start-period=15s so a slow cold start isn't flagged as a crash.
HEALTHCHECK --interval=30s CMD curl -f http://localhost:8000/health || exit 1

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
