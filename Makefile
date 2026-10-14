.PHONY: install test lint format env up down ps faults fault reset investigate controlplane incidents bench bench-dry bench-pilot report

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

controlplane:
	uv run uvicorn --factory sentinelops.controlplane.web:main_app --host 127.0.0.1 --port 8080

incidents:
	uv run python -m sentinelops.incidents $(ARGS)

bench-dry:
	uv run python -m sentinelops.evals run --dry-run --repeats 1 --results storage/bench-dry/results.jsonl --recordings storage/bench-dry/recordings $(ARGS)

bench-pilot:
	uv run python -m sentinelops.evals run --repeats 1 --scenarios bad_deploy/c --results storage/pilot/results.jsonl --recordings storage/pilot/recordings $(ARGS)

bench:
	uv run python -m sentinelops.evals run $(ARGS)

report:
	uv run python -m sentinelops.evals report
