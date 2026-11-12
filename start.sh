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

if [[ ! -x .venv/bin/python ]]; then
    echo "缺少本地 Python 环境，请先执行：uv sync --locked --extra mcp --extra postgres" >&2
    exit 1
fi

# Ignore another project's activated environment and always use this project's uv environment.
unset VIRTUAL_ENV
export UV_PROJECT_ENVIRONMENT="$project_dir/.venv"
exec uv run --project "$project_dir" --no-sync --python "$project_dir/.venv/bin/python" \
    python -m mem_plugin start "$@"
