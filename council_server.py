"""Council MCP Server v2.0 — entry point."""
from council.tools import mcp_server

if __name__ == "__main__":
    mcp_server.run(transport="stdio")
