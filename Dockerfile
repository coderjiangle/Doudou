# syntax=docker/dockerfile:1

ARG PYTHON_VERSION=3.12

# ---------------------------------------------------------------- builder
FROM python:${PYTHON_VERSION}-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# 先只拷贝依赖描述，最大化利用层缓存
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev \
    --extra feishu --extra anthropic --extra postgres

COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra feishu --extra anthropic --extra postgres

# ---------------------------------------------------------------- runtime
FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    DOUDOU_RUNTIME_ENV=prod \
    DOUDOU_RUNTIME_DEBUG=false \
    DOUDOU_SERVER_WORKERS=1 \
    DOUDOU_SERVER_HOST=0.0.0.0 \
    DOUDOU_SERVER_PORT=8000 \
    DOUDOU_FILESYSTEM_ROOT_DIR=/data

RUN groupadd --system --gid 1000 doudou \
    && useradd --system --uid 1000 --gid doudou --create-home doudou \
    && mkdir -p /data && chown -R doudou:doudou /data

WORKDIR /app

COPY --from=builder --chown=doudou:doudou /app/.venv /app/.venv
COPY --from=builder --chown=doudou:doudou /app/src /app/src
COPY --chown=doudou:doudou config ./config
COPY --chown=doudou:doudou skills ./skills

USER doudou

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health').read()"

CMD ["python", "-m", "doudou", "serve"]
