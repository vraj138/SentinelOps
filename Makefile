.PHONY: install test lint format up down

install:
	uv sync

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff check --fix .
	uv run ruff format .

up:
	@echo "not implemented yet"

down:
	@echo "not implemented yet"
