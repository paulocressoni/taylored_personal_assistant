# Makefile — taylored_personal_assistant
# PHASE 0 targets. Run from the repo root.
#   make cli            -> dev by default
#   ENV=prod make cli   -> prod config
# Requires GNU make + a POSIX shell (Linux / WSL / Git Bash on Windows).

SHELL := /bin/bash
BACKEND := backend
ENV ?= dev

.PHONY: dev-up test lint cli types

cli:
	cd $(BACKEND) && ENV=$(ENV) uv run python -m app.core.cli

test:
	cd $(BACKEND) && ENV=$(ENV) uv run pytest

lint:
	cd $(BACKEND) && uv run ruff check .
	cd $(BACKEND) && uv run ruff format --check .

types:
	cd $(BACKEND) && uv run mypy app

dev-up:
	cd $(BACKEND) && ENV=$(ENV) uv run uvicorn app.main:app --reload