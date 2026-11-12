FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.23 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Cài thư viện trước (tận dụng cache khi chỉ sửa code)
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev

RUN useradd --uid 1000 --create-home app && mkdir -p /app/data && chown app:app /app/data
USER app

ENV PATH=/app/.venv/bin:$PATH \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000 \
    DB_PATH=/app/data/reports.db

EXPOSE 8000
VOLUME ["/app/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)" || exit 1

CMD ["email-report-mcp", "serve"]
