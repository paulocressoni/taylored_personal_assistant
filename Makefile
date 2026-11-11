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

.PHONY: config cli test lint types dev-local dev-up dev-up-light dev-down dev-logs dev-ps prod-pull prod-up prod-down prod-logs prod-ps soak-up soak-stop soak-status soak-meta soak-report

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

# IMPORTANT: every lifecycle target carries --profile observability, including
# the ones that STOP things. `docker compose down` only acts on the services in
# the active model, so without the profile it removes backend/frontend/app-db
# and leaves the six Langfuse containers running. `ps` shows them either way,
# which is what makes the mistake invisible. (Leftovers from a profile-less
# run: add --remove-orphans once.)
dev-down:
	$(COMPOSE_DEV) --profile observability down

dev-logs:
	$(COMPOSE_DEV) --profile observability logs -f

dev-ps:
	$(COMPOSE_DEV) --profile observability ps

# ============================ SOAK ============================
# A soak runs THIS WORKING TREE's backend behind the prod URL, because the
# flashed satellite's URI is compile-time and reflashing it would end the
# baseline the soak exists to compare against. See docs/soak.md and
# infra/compose/docker-compose.soak.yml.
#
# The frontend stays the released image: it owns :8080 and already proxies
# /ws. Only the backend is rebuilt.
#
# WARNING: soak-up RECREATES the backend container, so it also ends any capture
# already following it. That is fine as the first step of a soak and wrong in
# the middle of one — check `make soak-status` before you run it.
SOAK_DIR ?= $(HOME)/wake-soak
SOAK_IMAGE := taylored-assistant-backend:soak-local
SOAK_BACKEND := taylored-assistant-prod-backend-1
COMPOSE_SOAK := docker compose -p taylored-assistant-prod \
  -f $(COMPOSE_DIR)/docker-compose.base.yml \
  -f $(COMPOSE_DIR)/docker-compose.prod.yml \
  -f $(COMPOSE_DIR)/docker-compose.soak.yml \
  --env-file $(COMPOSE_DIR)/.env.prod
# The access logs print the shared key on every /ws/voice URL (the satellite
# uses the ?api_key= fallback), so every capture is filtered on the way in.
SOAK_REDACT := s/\\(api_key=\\)[^ &"]*/\\1REDACTED/g

soak-up:
	@if docker ps --format '{{.Names}}' | grep -q '^taylored-assistant-dev-'; then \
	  echo "soak-up: the dev stack is running — 'make dev-down' first."; \
	  echo "         Both stacks publish langfuse-web on :3000."; exit 1; fi
	@test -f $(COMPOSE_DIR)/.env.prod || \
	  { echo "soak-up: missing $(COMPOSE_DIR)/.env.prod (copy .env.prod.example)"; exit 1; }
	@mkdir -p $(SOAK_DIR)
	$(COMPOSE_SOAK) build backend
	$(COMPOSE_SOAK) --profile observability up -d --no-build --wait
	@$(MAKE) --no-print-directory soak-meta
	@bash $(BACKEND)/scripts/soak_collectors.sh start

soak-stop:
	@bash $(BACKEND)/scripts/soak_collectors.sh stop

soak-status:
	@echo "repo     : $(shell git rev-parse --short HEAD)$(shell git diff --quiet || echo ' (dirty)')"
	@echo "soak dir : $(SOAK_DIR)"
	@echo "backend  : $(shell docker inspect -f '{{.Id}}' $(SOAK_BACKEND) 2>/dev/null | cut -c1-12 || echo 'not running')"
	@echo "image    : $(shell docker inspect -f '{{.Image}}' $(SOAK_BACKEND) 2>/dev/null | cut -c8-19 || echo '-')"
	@test -f $(SOAK_DIR)/META && sed 's/^/meta     : /' $(SOAK_DIR)/META || echo "meta     : none (run make soak-up)"
	@bash $(BACKEND)/scripts/soak_collectors.sh status

soak-meta:
	@mkdir -p $(SOAK_DIR)
	@bash $(BACKEND)/scripts/soak_collectors.sh meta > $(SOAK_DIR)/META
	@echo "META written to $(SOAK_DIR)/META"

soak-report:
	@test -n "$(UNTIL)" || \
	  { echo "soak-report: set UNTIL=<ISO-8601 UTC>, e.g. make soak-report UNTIL=2026-11-11T17:45:00Z"; \
	    exit 1; }
	cd $(BACKEND) && uv run python scripts/soak_report.py --dir $(SOAK_DIR) --until "$(UNTIL)"

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

# See the note above dev-down: `down` MUST carry the profile or the Langfuse
# containers survive the teardown. This is the prod half of that rule.
prod-down:
	$(COMPOSE_PROD) --profile observability down

prod-logs:
	$(COMPOSE_PROD) --profile observability logs -f

prod-ps:
	$(COMPOSE_PROD) --profile observability ps
