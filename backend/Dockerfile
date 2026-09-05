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

# Create a dedicated non-root user (least privilege): the process must not
# run as root so a compromise inside the container isn't root in it.
RUN useradd --create-home --shell /usr/sbin/nologin appuser

# Copy the finished virtualenv from the builder (no reinstall).
COPY --from=builder /app/.venv /app/.venv

# Copy ONLY the application source, owned by the app user. The checkpointer
# writes checkpoints.db into the CWD (/app), so /app must be writable too.
COPY --chown=appuser:appuser app/ ./app/
RUN chown appuser:appuser /app

ENV PATH="/app/.venv/bin:$PATH"

USER appuser

# Healthcheck to ensure the application is running.
# python:slim ships NO curl, so use the stdlib one-liner instead of apt-get
# installing curl (keeps the image smaller). This is the same pattern
# documented in docs/docker-cheatsheet.md.
HEALTHCHECK --interval=30s CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
