FROM alpine:3.23 AS python-builder

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/venv \
    UV_PYTHON_INSTALL_DIR=/python \
    UV_PYTHON_PREFERENCE=only-managed \
    VIRTUAL_ENV=/venv

WORKDIR /app

COPY ./src ./LICENSE ./README.md /app/

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock,ro \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml,ro \
    uv sync --frozen --no-dev

FROM alpine:3.23

RUN apk add --no-cache git shadow su-exec tzdata

ENV PATH=/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHON_JIT=1 \
    PUID=1000 \
    PGID=1000 \
    UMASK=022

WORKDIR /app

COPY . /app
COPY --from=python-builder /python /python
COPY --from=python-builder /venv /venv

RUN mkdir -p /config

VOLUME ["/config"]

EXPOSE 4849

CMD ["python", "/app/main.py"]
