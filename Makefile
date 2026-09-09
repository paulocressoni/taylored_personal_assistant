# Makefile — taylored_personal_assistant
# Dev + prod workflow for the local stack (M12) and the GHCR-published
# images (M23). Run from the repo root.
#   make cli            -> dev by default
#   ENV=prod make cli   -> prod config
# Requires GNU make + a POSIX shell (Linux / WSL / Git Bash on Windows).

SHELL := /bin/bash
BACKEND := backend
ENV ?= dev
MSG ?=

# --- App version (single source: backend/app/_version.py) ---------------
# APP_VERSION feeds compose's ${APP_VERSION}. Local dev images fall back to
# `latest`; PROD REQUIRES a concrete version and never deploys :latest
# (golden rule: the git tag IS the image tag IS the deployed version).
APP_VERSION := $(shell sed -n 's/.*__version__ = "\(.*\)".*/\1/p' backend/app/_version.py)
export APP_VERSION

.PHONY: config cli test lint types dev-local dev-up dev-up-light dev-down dev-logs dev-ps prod-pull prod-up prod-down prod-logs prod-ps

config:
    cd $(BACKEND) && ENV=$(ENV) uv run python -m app.core.cli

cli:
    cd $(BACKEND) && ENV=$(ENV) uv run python -m app.graph.cli "$(MSG)"

test:
    cd $(BACKEND) && ENV=$(ENV) uv run pytest

lint:
    cd $(BACKEND) && uv run ruff check .
    cd $(BACKEND) && uv run ruff format --check .
    cd frontend && npm run lint
    cd frontend && npx prettier --check .

types:
    cd $(BACKEND) && uv run mypy app

# --- Local (non-Docker) uvicorn — the pre-M12 quick dev loop ---
dev-local:
    cd $(BACKEND) && ENV=$(ENV) uv run uvicorn app.main:app --reload

# --- Compose overlay plumbing (M12, M28) ---------------------------------
# base.yml + an overlay are merged by Compose; --env-file feeds the
# ${VAR:?...} secrets used by the Langfuse services. Each environment reads
# its OWN secrets file, so dev and prod never share values (M28):
#   dev  -> infra/compose/.env.dev   (copy of .env.dev.example)
#   prod -> infra/compose/.env.prod  (copy of .env.prod.example)
#   dev overlay  = docker-compose.dev.yml  -> BUILD from source, hot reload
#   prod overlay = docker-compose.prod.yml -> PULL the GHCR images, never build
COMPOSE_DIR := infra/compose
# Each environment pins its OWN project name via -p (overrides base.yml's
# neutral `name:`), so dev/prod volumes, networks and container names never
# collide and they NEVER share conversation data.
COMPOSE_DEV := docker compose -p taylored-assistant-dev -f $(COMPOSE_DIR)/docker-compose.base.yml -f $(COMPOSE_DIR)/docker-compose.dev.yml --env-file $(COMPOSE_DIR)/.env.dev
COMPOSE_PROD := docker compose -p taylored-assistant-prod -f $(COMPOSE_DIR)/docker-compose.base.yml -f $(COMPOSE_DIR)/docker-compose.prod.yml --env-file $(COMPOSE_DIR)/.env.prod

# ============================ DEV ============================
# THE M12 command: whole stack (backend + frontend + Langfuse + DBs), built
# from source. The frontend builds its `dev` Dockerfile target (hot reload).
dev-up:
    $(COMPOSE_DEV) --profile observability up --build -d --wait

# Same stack WITHOUT Langfuse (lighter/faster; observability skipped).
dev-up-light:
    $(COMPOSE_DEV) up --build -d --wait

dev-down:
    $(COMPOSE_DEV) down

dev-logs:
    $(COMPOSE_DEV) logs -f

dev-ps:
    $(COMPOSE_DEV) ps

# ============================ PROD ============================
# Runs the CI-published GHCR images pinned to :$(APP_VERSION) — nothing is
# built from source. Requires the release images to exist on GHCR (a tagged
# release was published) and this box to be logged in to ghcr.io.
# NOTE: dev/prod run under SEPARATE project names (taylored-assistant-dev /
# taylored-assistant-prod, set above) so volumes/network/containers never
# collide and conversation data is never shared. Both can even run at once
# (prod publishes :8080 + :3000; dev uses 8000/5173/3000).
# NOTE: local dev images are tagged taylored-assistant-*; prod pulls the
# published taylored-personal-assistant-{backend,frontend} from GHCR.
# NOTE: prod reads its secrets from infra/compose/.env.prod (never .env.dev).

# Pull the exact release images (never :latest). --profile observability
# includes the Langfuse stack (M28) — without it `pull` silently skips those
# services and prod-up would have nothing to start.
prod-pull:
    $(COMPOSE_PROD) --profile observability pull

# Pull + run the release. --no-build guarantees we never compile locally.
# --profile observability brings up the full Langfuse stack on prod (M28).
prod-up:
    $(COMPOSE_PROD) --profile observability pull
    $(COMPOSE_PROD) --profile observability up -d --no-build --wait

prod-down:
    $(COMPOSE_PROD) down

prod-logs:
    $(COMPOSE_PROD) logs -f

prod-ps:
    $(COMPOSE_PROD) ps
