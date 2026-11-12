FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.28 /uv /uvx /usr/local/bin/

WORKDIR /app
ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH" \
    MEM_PLUGIN_HOST=0.0.0.0 \
    MEM_PLUGIN_PORT=8000 \
    MEM_PLUGIN_MCP_URL=http://127.0.0.1:8000/mcp

COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/
COPY alembic.ini ./
COPY migrations/ ./migrations/
RUN uv sync --frozen --no-dev --extra mcp --extra postgres --no-editable
RUN useradd --uid 10001 --create-home app && mkdir -p /app/data/archive && chown app:app /app/data/archive && chmod 700 /app/data/archive
USER app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"
CMD ["mem-plugin-api"]
