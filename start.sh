#!/usr/bin/env bash
# Run the local memory service; configuration is loaded by Python, never sourced.
set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd -- "$project_dir"

if ! command -v uv >/dev/null 2>&1; then
    echo "未找到 uv，请先安装 uv。" >&2
    exit 1
fi

if [[ ! -f .env ]]; then
    echo "缺少 .env，请先配置 .env，再启动服务。" >&2
    exit 1
fi

# Always use this checkout's environment, including during setup and migration.
unset VIRTUAL_ENV
export UV_PROJECT_ENVIRONMENT="$project_dir/.venv"

if [[ "${1:-}" == "--with-postgres" ]]; then
    shift
    uv sync --locked --extra mcp --extra postgres
    docker compose --env-file .env \
        -f deploy/docker-compose.yml -f deploy/docker-compose.dev.yml \
        up -d --wait --pull missing postgres
    uv run --no-sync alembic upgrade head
fi

if [[ ! -x .venv/bin/python ]]; then
    echo "缺少本地 Python 环境，请先执行：uv sync --locked --extra mcp --extra postgres" >&2
    exit 1
fi

exec uv run --project "$project_dir" --no-sync --python "$project_dir/.venv/bin/python" \
    python -m mem_plugin start "$@"
