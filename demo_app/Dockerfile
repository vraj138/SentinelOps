# One image for every demo service; Compose picks the service with `command`.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.22 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY demo_app ./demo_app

ARG APP_VERSION=dev
ENV APP_VERSION=$APP_VERSION

RUN useradd --no-create-home --uid 10001 app
USER app

EXPOSE 8000
