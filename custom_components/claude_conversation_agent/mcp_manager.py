"""MCP server connection and tool management."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import Tool as MCPTool

from .const import MCP_CONNECT_TIMEOUT, MCP_TOOL_TIMEOUT

_LOGGER = logging.getLogger(__name__)


@dataclass
class MCPServerConnection:
    """A single MCP server connection."""

    name: str
    url: str
    token: str | None = None
    session: ClientSession | None = None
    tools: list[MCPTool] = field(default_factory=list)
    _exit_stack: AsyncExitStack = field(default_factory=AsyncExitStack)


class MCPManager:
    """Manages connections to multiple MCP servers."""

    def __init__(self) -> None:
        """Initialize the MCP manager."""
        self._connections: dict[str, MCPServerConnection] = {}
        self._tool_registry: dict[str, tuple[str, str]] = {}

    @property
    def connected_servers(self) -> list[str]:
        """Return names of connected servers."""
        return [
            name
            for name, conn in self._connections.items()
            if conn.session is not None
        ]

    async def connect(
        self, name: str, url: str, token: str | None = None
    ) -> None:
        """Connect to an MCP server and discover tools."""
        conn = MCPServerConnection(name=name, url=url, token=token)
        conn._exit_stack = AsyncExitStack()

        headers: dict[str, str] = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        try:
            async with asyncio.timeout(MCP_CONNECT_TIMEOUT):
                await self._connect_transport(conn, url, headers)
        except TimeoutError as err:
            await conn._exit_stack.aclose()
            raise ConnectionError(
                f"Timeout connecting to MCP server '{name}' at {url}"
            ) from err
        except Exception:
            await conn._exit_stack.aclose()
            raise

        # Discover tools
        try:
            result = await conn.session.list_tools()  # type: ignore[union-attr]
            conn.tools = result.tools
        except Exception:
            _LOGGER.warning(
                "Failed to discover tools from MCP server '%s'", name
            )
            conn.tools = []

        # Register tools with namespacing
        for tool in conn.tools:
            namespaced = f"{name}__{tool.name}"
            self._tool_registry[namespaced] = (name, tool.name)

        self._connections[name] = conn
        _LOGGER.info(
            "Connected to MCP server '%s' with %d tools",
            name,
            len(conn.tools),
        )

    async def _connect_transport(
        self,
        conn: MCPServerConnection,
        url: str,
        headers: dict[str, str],
    ) -> None:
        """Connect using Streamable HTTP, falling back to SSE."""
        try:
            read, write, _ = await conn._exit_stack.enter_async_context(
                streamable_http_client(url=url, headers=headers)
            )
        except (ExceptionGroup, httpx.HTTPStatusError) as err:
            # Check if it's a 405 (Method Not Allowed) → fall back to SSE
            should_fallback = False
            if isinstance(err, ExceptionGroup):
                for exc in err.exceptions:
                    if (
                        isinstance(exc, httpx.HTTPStatusError)
                        and exc.response.status_code == 405
                    ):
                        should_fallback = True
                        break
                if not should_fallback:
                    raise err.exceptions[0] from err
            else:
                if err.response.status_code == 405:
                    should_fallback = True
                else:
                    raise

            if should_fallback:
                _LOGGER.debug(
                    "Streamable HTTP not supported for '%s', falling back to SSE",
                    conn.name,
                )
                # Reset exit stack for clean SSE connection
                await conn._exit_stack.aclose()
                conn._exit_stack = AsyncExitStack()
                read, write = await conn._exit_stack.enter_async_context(
                    sse_client(url=url, headers=headers)
                )

        conn.session = await conn._exit_stack.enter_async_context(
            ClientSession(read, write)
        )
        await conn.session.initialize()

    async def disconnect(self, name: str) -> None:
        """Disconnect from a specific MCP server."""
        conn = self._connections.pop(name, None)
        if conn is None:
            return

        # Remove tools from registry
        to_remove = [
            key
            for key, (server, _) in self._tool_registry.items()
            if server == name
        ]
        for key in to_remove:
            del self._tool_registry[key]

        await conn._exit_stack.aclose()
        conn.session = None
        _LOGGER.info("Disconnected from MCP server '%s'", name)

    async def disconnect_all(self) -> None:
        """Disconnect from all MCP servers."""
        names = list(self._connections.keys())
        for name in names:
            try:
                await self.disconnect(name)
            except Exception:
                _LOGGER.exception(
                    "Error disconnecting from MCP server '%s'", name
                )

    async def refresh_tools(self, name: str) -> None:
        """Re-discover tools from a connected server."""
        conn = self._connections.get(name)
        if not conn or not conn.session:
            return

        # Remove old tools from registry
        to_remove = [
            key
            for key, (server, _) in self._tool_registry.items()
            if server == name
        ]
        for key in to_remove:
            del self._tool_registry[key]

        try:
            result = await conn.session.list_tools()
            conn.tools = result.tools
        except Exception:
            _LOGGER.warning(
                "Failed to refresh tools from MCP server '%s'", name
            )
            return

        for tool in conn.tools:
            namespaced = f"{name}__{tool.name}"
            self._tool_registry[namespaced] = (name, tool.name)

        _LOGGER.debug(
            "Refreshed %d tools from MCP server '%s'",
            len(conn.tools),
            name,
        )

    def get_claude_tools(self) -> list[dict[str, Any]]:
        """Get all MCP tools formatted for the Claude API."""
        tools: list[dict[str, Any]] = []
        for conn in self._connections.values():
            if not conn.session:
                continue
            for tool in conn.tools:
                tools.append(
                    {
                        "name": f"{conn.name}__{tool.name}",
                        "description": tool.description or "",
                        "input_schema": tool.inputSchema,
                    }
                )
        return tools

    async def call_tool(
        self, namespaced_name: str, arguments: dict[str, Any]
    ) -> str:
        """Execute a tool call via the appropriate MCP server."""
        if namespaced_name not in self._tool_registry:
            return json.dumps({"error": f"Unknown tool: {namespaced_name}"})

        server_name, tool_name = self._tool_registry[namespaced_name]
        conn = self._connections.get(server_name)

        if not conn or not conn.session:
            return json.dumps(
                {"error": f"MCP server '{server_name}' not connected"}
            )

        try:
            result = await asyncio.wait_for(
                conn.session.call_tool(tool_name, arguments),
                timeout=MCP_TOOL_TIMEOUT,
            )
            # Convert MCP result to string for Claude
            content_parts: list[str] = []
            for item in result.content:
                if hasattr(item, "text"):
                    content_parts.append(item.text)
                else:
                    content_parts.append(str(item))
            return "\n".join(content_parts) if content_parts else "OK"

        except TimeoutError:
            _LOGGER.warning(
                "Tool '%s' on server '%s' timed out after %ss",
                tool_name,
                server_name,
                MCP_TOOL_TIMEOUT,
            )
            return json.dumps(
                {"error": f"Tool '{tool_name}' timed out after {MCP_TOOL_TIMEOUT}s"}
            )
        except Exception as err:
            _LOGGER.warning(
                "Tool '%s' on server '%s' failed: %s",
                tool_name,
                server_name,
                err,
            )
            return json.dumps(
                {
                    "error": type(err).__name__,
                    "error_text": str(err),
                }
            )

    async def ensure_connected(self, name: str) -> bool:
        """Ensure a server connection is active, reconnecting if needed."""
        conn = self._connections.get(name)
        if not conn:
            return False

        if not conn.session:
            try:
                await self.connect(name, conn.url, conn.token)
                return True
            except Exception as err:
                _LOGGER.error(
                    "Failed to reconnect to MCP server '%s': %s", name, err
                )
                return False

        try:
            await asyncio.wait_for(conn.session.list_tools(), timeout=5.0)
            return True
        except Exception:
            _LOGGER.warning(
                "MCP server '%s' connection lost, reconnecting", name
            )
            await conn._exit_stack.aclose()
            conn.session = None
            try:
                await self.connect(name, conn.url, conn.token)
                return True
            except Exception as err:
                _LOGGER.error(
                    "Failed to reconnect to MCP server '%s': %s", name, err
                )
                return False
