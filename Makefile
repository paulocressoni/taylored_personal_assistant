# Makefile — taylored_personal_assistant
# PHASE 0 targets. Run from the repo root.
#   make cli            -> dev by default
#   ENV=prod make cli   -> prod config
# Requires GNU make + a POSIX shell (Linux / WSL / Git Bash on Windows).

SHELL := /bin/bash
BACKEND := backend
ENV ?= dev
MSG ?=

.PHONY: dev-up dev-up-light dev-down dev-logs dev-ps dev-local test lint cli types config

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

# --- Full local dev stack (M12): Compose OVERLAYS ---
# base.yml + dev.yml are merged by Compose; --env-file feeds the
# ${VAR:?...} secrets used by the Langfuse services (infra/compose/.env).
COMPOSE_DIR := infra/compose
ENV_FILE := $(COMPOSE_DIR)/.env
COMPOSE := docker compose -f $(COMPOSE_DIR)/docker-compose.base.yml -f $(COMPOSE_DIR)/docker-compose.dev.yml --env-file $(ENV_FILE)

# THE M12 command: whole stack (backend + frontend + Langfuse + DBs).
# `--profile observability` enables the optional Langfuse services.
dev-up:
	$(COMPOSE) --profile observability up --build -d --wait

# Same stack WITHOUT Langfuse (lighter/faster; observability skipped).
dev-up-light:
	$(COMPOSE) up --build -d --wait

dev-down:
	$(COMPOSE) down

dev-logs:
	$(COMPOSE) logs -f

dev-ps:
	$(COMPOSE) ps
