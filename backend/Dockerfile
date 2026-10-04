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

# Pull Debian's security updates at build time. The python:slim tag can lag
# behind the security archive -- it still ships libpcre2-8-0 10.46-1~deb13u2
# after 10.46-1~deb13u3 was published -- and CI fails the build on any
# fixable HIGH/CRITICAL finding. Never ship a runtime image holding a known
# CVE when the fix is one apt-get away.
# Cached like any other layer: if Trivy reports a stale OS package again,
# touch this line or build with `--no-cache-filter runtime`.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get upgrade --yes \
    && rm -rf /var/lib/apt/lists/*

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
