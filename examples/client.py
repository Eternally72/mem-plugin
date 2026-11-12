"""Run against a local server: uv run --extra mcp python examples/client.py."""

import asyncio

from fastmcp import Client

from mem_plugin.config.settings import load_settings


async def main() -> None:
    settings = load_settings()
    async with Client(settings.mcp_url, auth=settings.mcp_token) as client:
        result = await client.list_tools()
        print("Tools:", [tool.name for tool in result])
        recalled = await client.call_tool(
            "memory.search", {"query": "咖啡", "namespace": settings.namespaces[0]}
        )
        print("Search succeeded; records:", len(recalled.structured_content["memories"]))


if __name__ == "__main__":
    asyncio.run(main())
