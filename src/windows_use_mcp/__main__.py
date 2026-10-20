"""Allow `python -m windows_use_mcp` to start the MCP server."""

from .server import main

if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
