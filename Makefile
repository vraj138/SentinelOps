.PHONY: install test lint format up down ps

COMPOSE := docker compose -f infra/docker-compose.yml

install:
	uv sync

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff format .
	uv run ruff check --fix .

up:
	APP_VERSION=$$(git rev-parse --short HEAD) $(COMPOSE) up -d --build

down:
	$(COMPOSE) down

ps:
	$(COMPOSE) ps
