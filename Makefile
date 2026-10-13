.PHONY: install test lint format env up down ps faults fault reset investigate

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

env:
	uv run python -m faults init

up: env
	IMAGE_TAG=$$(git rev-parse --short HEAD) $(COMPOSE) up -d --build

down: env
	$(COMPOSE) down

ps: env
	$(COMPOSE) ps

faults:
	uv run python -m faults list

fault: env
	@test -n "$(NAME)" -a -n "$(VARIANT)" || (echo "usage: make fault NAME=bad_deploy VARIANT=a" && exit 1)
	uv run python -m faults inject $(NAME)/$(VARIANT)

reset:
	uv run python -m faults reset

investigate:
	uv run python -m sentinelops.investigate $(ARGS)
