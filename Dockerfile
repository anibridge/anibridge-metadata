FROM python:3.14-alpine AS python-builder

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /uvx /bin/

ENV UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/venv \
    UV_PYTHON_PREFERENCE=only-system \
    VIRTUAL_ENV=/venv

WORKDIR /app

COPY ./src ./LICENSE ./README.md /app/

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock,ro \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml,ro \
    uv sync --frozen --no-dev && \
    find /venv -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null; true

FROM python:3.14-alpine

RUN apk add --no-cache git shadow su-exec tzdata

LABEL maintainer="Elias Benbourenane <eliasbenbourenane@gmail.com>" \
    org.opencontainers.image.title="anibridge-metadata" \
    org.opencontainers.image.description="Metadata caching middleware for the AniBridge project" \
    org.opencontainers.image.authors="Elias Benbourenane <eliasbenbourenane@gmail.com>" \
    org.opencontainers.image.url="https://anibridge.eliasbenb.dev" \
    org.opencontainers.image.documentation="https://anibridge.eliasbenb.dev" \
    org.opencontainers.image.source="https://github.com/anibridge/anibridge-metadata" \
    org.opencontainers.image.licenses="MIT"

ENV PATH=/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHON_JIT=1 \
    PUID=1000 \
    PGID=1000 \
    UMASK=022

WORKDIR /app

COPY . /app

COPY --from=python-builder /venv /venv

RUN mkdir -p /config

VOLUME ["/config"]

EXPOSE 4849

CMD ["python", "/app/main.py"]
