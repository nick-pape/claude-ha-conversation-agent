"""Tests for mcp_manager.py – MCPManager class."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio  # noqa: F401

from custom_components.claude_conversation_agent.mcp_manager import (
    MCPManager,
    MCPServerConnection,
)
from custom_components.claude_conversation_agent.const import (
    MCP_CONNECT_TIMEOUT,
    MCP_TOOL_TIMEOUT,
)


# ===================================================================
# Helpers
# ===================================================================


def _make_mcp_tool(name: str, description: str = "", input_schema: dict | None = None) -> SimpleNamespace:
    """Create a mock MCP Tool object."""
    return SimpleNamespace(
        name=name,
        description=description,
        inputSchema=input_schema or {"type": "object", "properties": {}},
    )


def _make_tool_result(*texts: str) -> SimpleNamespace:
    """Create a mock MCP tool call result with text content items."""
    items = [SimpleNamespace(text=t) for t in texts]
    return SimpleNamespace(content=items)


def _make_non_text_tool_result() -> SimpleNamespace:
    """Create a mock MCP tool result with non-text items (no .text attr)."""
    item = SimpleNamespace(kind="image", data="binary_data")
    return SimpleNamespace(content=[item])


def _make_list_tools_result(tools: list) -> SimpleNamespace:
    """Create a mock result from session.list_tools()."""
    return SimpleNamespace(tools=tools)


def _patch_transports(mock_session: AsyncMock):
    """Return a context manager that patches both streamable_http_client and sse_client.

    The streamable_http_client returns (read, write, _) and the session is created
    via ClientSession(read, write).
    """
    mock_read = AsyncMock()
    mock_write = AsyncMock()

    @asynccontextmanager
    async def fake_streamable_http_client(url, headers=None):
        yield mock_read, mock_write, None

    @asynccontextmanager
    async def fake_sse_client(url, headers=None):
        yield mock_read, mock_write

    @asynccontextmanager
    async def fake_client_session(read, write):
        yield mock_session

    patches = [
        patch(
            "custom_components.claude_conversation_agent.mcp_manager.streamable_http_client",
            side_effect=fake_streamable_http_client,
        ),
        patch(
            "custom_components.claude_conversation_agent.mcp_manager.sse_client",
            side_effect=fake_sse_client,
        ),
        patch(
            "custom_components.claude_conversation_agent.mcp_manager.ClientSession",
            side_effect=fake_client_session,
        ),
    ]
    return patches


# ===================================================================
# MCPManager.connect
# ===================================================================


class TestMCPManagerConnect:
    """Tests for connecting to an MCP server."""

    @pytest.mark.asyncio
    async def test_connect_success(self, mcp_manager: MCPManager, mock_mcp_session: AsyncMock):
        tools = [_make_mcp_tool("turn_on"), _make_mcp_tool("get_state")]
        mock_mcp_session.list_tools.return_value = _make_list_tools_result(tools)

        patches = _patch_transports(mock_mcp_session)
        with patches[0], patches[1], patches[2]:
            await mcp_manager.connect("ha", "http://localhost:8080/mcp")

        assert "ha" in mcp_manager.connected_servers
        claude_tools = mcp_manager.get_claude_tools()
        assert len(claude_tools) == 2
        assert claude_tools[0]["name"] == "ha__turn_on"
        assert claude_tools[1]["name"] == "ha__get_state"

    @pytest.mark.asyncio
    async def test_connect_with_token(self, mcp_manager: MCPManager, mock_mcp_session: AsyncMock):
        """Token should be passed as Bearer header."""
        mock_mcp_session.list_tools.return_value = _make_list_tools_result([])

        captured_headers: dict[str, str] = {}

        @asynccontextmanager
        async def fake_streamable(url, headers=None):
            captured_headers.update(headers or {})
            yield AsyncMock(), AsyncMock(), None

        @asynccontextmanager
        async def fake_session(r, w):
            yield mock_mcp_session

        with (
            patch(
                "custom_components.claude_conversation_agent.mcp_manager.streamable_http_client",
                side_effect=fake_streamable,
            ),
            patch(
                "custom_components.claude_conversation_agent.mcp_manager.sse_client",
            ),
            patch(
                "custom_components.claude_conversation_agent.mcp_manager.ClientSession",
                side_effect=fake_session,
            ),
        ):
            await mcp_manager.connect("ha", "http://localhost:8080/mcp", token="secret-token")

        assert captured_headers.get("Authorization") == "Bearer secret-token"

    @pytest.mark.asyncio
    async def test_connect_list_tools_failure_yields_empty_tools(
        self, mcp_manager: MCPManager, mock_mcp_session: AsyncMock
    ):
        """If list_tools() fails, the server is connected but with zero tools."""
        mock_mcp_session.list_tools.side_effect = RuntimeError("list_tools failed")

        patches = _patch_transports(mock_mcp_session)
        with patches[0], patches[1], patches[2]:
            await mcp_manager.connect("ha", "http://localhost:8080/mcp")

        assert "ha" in mcp_manager.connected_servers
        assert mcp_manager.get_claude_tools() == []

    @pytest.mark.asyncio
    async def test_connect_timeout_raises_connection_error(
        self, mcp_manager: MCPManager
    ):
        """If the transport connection times out, a ConnectionError is raised."""

        @asynccontextmanager
        async def slow_streamable(url, headers=None):
            await asyncio.sleep(MCP_CONNECT_TIMEOUT + 5)
            yield AsyncMock(), AsyncMock(), None  # pragma: no cover

        with (
            patch(
                "custom_components.claude_conversation_agent.mcp_manager.streamable_http_client",
                side_effect=slow_streamable,
            ),
            pytest.raises(ConnectionError, match="Timeout connecting"),
        ):
            await mcp_manager.connect("ha", "http://localhost:8080/mcp")

    @pytest.mark.asyncio
    async def test_connect_multiple_servers(self, mcp_manager: MCPManager, mock_mcp_session: AsyncMock):
        """Connect two servers; tools from both are namespaced correctly."""
        tools_a = [_make_mcp_tool("tool_x")]
        tools_b = [_make_mcp_tool("tool_y")]

        call_count = 0

        async def list_tools_side_effect():
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                return _make_list_tools_result(tools_a)
            return _make_list_tools_result(tools_b)

        mock_mcp_session.list_tools = AsyncMock(side_effect=list_tools_side_effect)

        patches = _patch_transports(mock_mcp_session)
        with patches[0], patches[1], patches[2]:
            await mcp_manager.connect("server_a", "http://a:8080/mcp")
            await mcp_manager.connect("server_b", "http://b:8080/mcp")

        assert sorted(mcp_manager.connected_servers) == ["server_a", "server_b"]
        claude_tools = mcp_manager.get_claude_tools()
        names = [t["name"] for t in claude_tools]
        assert "server_a__tool_x" in names
        assert "server_b__tool_y" in names

    @pytest.mark.asyncio
    async def test_sse_fallback_on_405(self, mcp_manager: MCPManager, mock_mcp_session: AsyncMock):
        """When streamable_http_client returns 405, fall back to sse_client."""
        import httpx
        mock_mcp_session.list_tools.return_value = _make_list_tools_result([_make_mcp_tool("sse_tool")])

        mock_response = MagicMock()
        mock_response.status_code = 405
        http_405 = httpx.HTTPStatusError(
            "Method Not Allowed",
            request=MagicMock(),
            response=mock_response,
        )

        sse_called = False

        @asynccontextmanager
        async def failing_streamable(url, headers=None):
            raise http_405
            yield  # pragma: no cover – needed to make this an async generator

        @asynccontextmanager
        async def working_sse(url, headers=None):
            nonlocal sse_called
            sse_called = True
            yield AsyncMock(), AsyncMock()

        @asynccontextmanager
        async def fake_session(r, w):
            yield mock_mcp_session

        with (
            patch(
                "custom_components.claude_conversation_agent.mcp_manager.streamable_http_client",
                new=failing_streamable,
            ),
            patch(
                "custom_components.claude_conversation_agent.mcp_manager.sse_client",
                new=working_sse,
            ),
            patch(
                "custom_components.claude_conversation_agent.mcp_manager.ClientSession",
                new=fake_session,
            ),
        ):
            await mcp_manager.connect("ha", "http://localhost:8080/mcp")

        assert sse_called
        assert "ha" in mcp_manager.connected_servers
        tools = mcp_manager.get_claude_tools()
        assert len(tools) == 1
        assert tools[0]["name"] == "ha__sse_tool"


# ===================================================================
# MCPManager.disconnect
# ===================================================================


class TestMCPManagerDisconnect:
    """Tests for disconnecting from MCP servers."""

    @pytest.mark.asyncio
    async def test_disconnect_removes_server(self, mcp_manager: MCPManager, mock_mcp_session: AsyncMock):
        mock_mcp_session.list_tools.return_value = _make_list_tools_result(
            [_make_mcp_tool("tool_a")]
        )

        patches = _patch_transports(mock_mcp_session)
        with patches[0], patches[1], patches[2]:
            await mcp_manager.connect("ha", "http://localhost:8080/mcp")

        assert "ha" in mcp_manager.connected_servers

        await mcp_manager.disconnect("ha")

        assert "ha" not in mcp_manager.connected_servers
        assert mcp_manager.get_claude_tools() == []

    @pytest.mark.asyncio
    async def test_disconnect_removes_tools_from_registry(
        self, mcp_manager: MCPManager, mock_mcp_session: AsyncMock
    ):
        mock_mcp_session.list_tools.return_value = _make_list_tools_result(
            [_make_mcp_tool("t1"), _make_mcp_tool("t2")]
        )

        patches = _patch_transports(mock_mcp_session)
        with patches[0], patches[1], patches[2]:
            await mcp_manager.connect("ha", "http://localhost:8080/mcp")

        # Verify tools are registered
        result = await mcp_manager.call_tool("ha__t1", {})
        assert "error" not in result or "Unknown tool" not in result

        await mcp_manager.disconnect("ha")

        # Now tool call should return "Unknown tool"
        result = await mcp_manager.call_tool("ha__t1", {})
        parsed = json.loads(result)
        assert "Unknown tool" in parsed["error"]

    @pytest.mark.asyncio
    async def test_disconnect_nonexistent_is_noop(self, mcp_manager: MCPManager):
        await mcp_manager.disconnect("nonexistent")  # should not raise


# ===================================================================
# MCPManager.disconnect_all
# ===================================================================


class TestMCPManagerDisconnectAll:

    @pytest.mark.asyncio
    async def test_disconnect_all_clears_everything(
        self, mcp_manager: MCPManager, mock_mcp_session: AsyncMock
    ):
        mock_mcp_session.list_tools.return_value = _make_list_tools_result(
            [_make_mcp_tool("t1")]
        )

        patches = _patch_transports(mock_mcp_session)
        with patches[0], patches[1], patches[2]:
            await mcp_manager.connect("s1", "http://a/mcp")
            await mcp_manager.connect("s2", "http://b/mcp")

        assert len(mcp_manager.connected_servers) == 2

        await mcp_manager.disconnect_all()

        assert mcp_manager.connected_servers == []
        assert mcp_manager.get_claude_tools() == []

    @pytest.mark.asyncio
    async def test_disconnect_all_handles_errors(self, mcp_manager: MCPManager):
        """disconnect_all should not raise even if individual disconnects fail."""
        # Manually inject a connection that will fail to close
        conn = MCPServerConnection(name="broken", url="http://broken/mcp")
        conn._exit_stack = AsyncMock()
        conn._exit_stack.aclose = AsyncMock(side_effect=RuntimeError("close failed"))
        conn.session = AsyncMock()
        mcp_manager._connections["broken"] = conn

        # Should not raise
        await mcp_manager.disconnect_all()


# ===================================================================
# MCPManager.get_claude_tools – namespacing
# ===================================================================


class TestGetClaudeTools:

    @pytest.mark.asyncio
    async def test_namespacing_format(self, mcp_manager: MCPManager, mock_mcp_session: AsyncMock):
        tools = [
            _make_mcp_tool("turn_on", "Turn on entity", {"type": "object", "properties": {"entity_id": {"type": "string"}}}),
            _make_mcp_tool("get_state", "Get entity state"),
        ]
        mock_mcp_session.list_tools.return_value = _make_list_tools_result(tools)

        patches = _patch_transports(mock_mcp_session)
        with patches[0], patches[1], patches[2]:
            await mcp_manager.connect("homeassistant", "http://ha/mcp")

        claude_tools = mcp_manager.get_claude_tools()

        assert claude_tools[0]["name"] == "homeassistant__turn_on"
        assert claude_tools[0]["description"] == "Turn on entity"
        assert claude_tools[0]["input_schema"] == {
            "type": "object",
            "properties": {"entity_id": {"type": "string"}},
        }

        assert claude_tools[1]["name"] == "homeassistant__get_state"
        assert claude_tools[1]["description"] == "Get entity state"

    @pytest.mark.asyncio
    async def test_empty_description_defaults_to_empty_string(
        self, mcp_manager: MCPManager, mock_mcp_session: AsyncMock
    ):
        tool = SimpleNamespace(
            name="no_desc",
            description=None,
            inputSchema={"type": "object"},
        )
        mock_mcp_session.list_tools.return_value = _make_list_tools_result([tool])

        patches = _patch_transports(mock_mcp_session)
        with patches[0], patches[1], patches[2]:
            await mcp_manager.connect("s", "http://s/mcp")

        claude_tools = mcp_manager.get_claude_tools()
        # description is None but code does `tool.description or ""` → empty string
        assert claude_tools[0]["description"] == ""

    def test_no_connections_returns_empty(self, mcp_manager: MCPManager):
        assert mcp_manager.get_claude_tools() == []

    @pytest.mark.asyncio
    async def test_disconnected_session_excluded(self, mcp_manager: MCPManager):
        """A connection with session=None should not contribute tools."""
        conn = MCPServerConnection(
            name="dead",
            url="http://dead/mcp",
            session=None,
            tools=[_make_mcp_tool("zombie_tool")],
        )
        mcp_manager._connections["dead"] = conn

        assert mcp_manager.get_claude_tools() == []


# ===================================================================
# MCPManager.call_tool – dispatch and results
# ===================================================================


class TestCallTool:

    @pytest.mark.asyncio
    async def test_call_tool_success_text(self, mcp_manager: MCPManager, mock_mcp_session: AsyncMock):
        mock_mcp_session.list_tools.return_value = _make_list_tools_result(
            [_make_mcp_tool("get_state")]
        )
        mock_mcp_session.call_tool.return_value = _make_tool_result('{"state": "on"}')

        patches = _patch_transports(mock_mcp_session)
        with patches[0], patches[1], patches[2]:
            await mcp_manager.connect("ha", "http://ha/mcp")

        result = await mcp_manager.call_tool("ha__get_state", {"entity_id": "light.kitchen"})
        assert result == '{"state": "on"}'
        mock_mcp_session.call_tool.assert_awaited_once_with(
            "get_state", {"entity_id": "light.kitchen"}
        )

    @pytest.mark.asyncio
    async def test_call_tool_multi_text_parts(self, mcp_manager: MCPManager, mock_mcp_session: AsyncMock):
        """Multiple text content parts are joined with newlines."""
        mock_mcp_session.list_tools.return_value = _make_list_tools_result(
            [_make_mcp_tool("multi")]
        )
        mock_mcp_session.call_tool.return_value = _make_tool_result("line1", "line2", "line3")

        patches = _patch_transports(mock_mcp_session)
        with patches[0], patches[1], patches[2]:
            await mcp_manager.connect("ha", "http://ha/mcp")

        result = await mcp_manager.call_tool("ha__multi", {})
        assert result == "line1\nline2\nline3"

    @pytest.mark.asyncio
    async def test_call_tool_non_text_content(self, mcp_manager: MCPManager, mock_mcp_session: AsyncMock):
        """Non-text content items fall back to str()."""
        mock_mcp_session.list_tools.return_value = _make_list_tools_result(
            [_make_mcp_tool("img")]
        )
        mock_mcp_session.call_tool.return_value = _make_non_text_tool_result()

        patches = _patch_transports(mock_mcp_session)
        with patches[0], patches[1], patches[2]:
            await mcp_manager.connect("ha", "http://ha/mcp")

        result = await mcp_manager.call_tool("ha__img", {})
        # Should contain the string representation
        assert "image" in result or "binary_data" in result

    @pytest.mark.asyncio
    async def test_call_tool_empty_result_returns_ok(
        self, mcp_manager: MCPManager, mock_mcp_session: AsyncMock
    ):
        """Empty content list should return 'OK'."""
        mock_mcp_session.list_tools.return_value = _make_list_tools_result(
            [_make_mcp_tool("noop")]
        )
        mock_mcp_session.call_tool.return_value = SimpleNamespace(content=[])

        patches = _patch_transports(mock_mcp_session)
        with patches[0], patches[1], patches[2]:
            await mcp_manager.connect("ha", "http://ha/mcp")

        result = await mcp_manager.call_tool("ha__noop", {})
        assert result == "OK"

    @pytest.mark.asyncio
    async def test_call_tool_unknown_tool(self, mcp_manager: MCPManager):
        """Calling an unregistered tool returns a JSON error."""
        result = await mcp_manager.call_tool("ha__nonexistent", {})
        parsed = json.loads(result)
        assert "Unknown tool" in parsed["error"]
        assert "ha__nonexistent" in parsed["error"]

    @pytest.mark.asyncio
    async def test_call_tool_server_not_connected(self, mcp_manager: MCPManager):
        """If the server connection's session is None, return an error."""
        # Manually register a tool but with session=None
        conn = MCPServerConnection(name="ha", url="http://ha/mcp", session=None)
        mcp_manager._connections["ha"] = conn
        mcp_manager._tool_registry["ha__orphan"] = ("ha", "orphan")

        result = await mcp_manager.call_tool("ha__orphan", {})
        parsed = json.loads(result)
        assert "not connected" in parsed["error"]


class TestCallToolTimeout:

    @pytest.mark.asyncio
    async def test_call_tool_timeout(self, mcp_manager: MCPManager, mock_mcp_session: AsyncMock):
        """Tool that exceeds MCP_TOOL_TIMEOUT returns a timeout error."""
        mock_mcp_session.list_tools.return_value = _make_list_tools_result(
            [_make_mcp_tool("slow")]
        )

        async def slow_tool(name, args):
            await asyncio.sleep(MCP_TOOL_TIMEOUT + 10)

        mock_mcp_session.call_tool = slow_tool

        patches = _patch_transports(mock_mcp_session)
        with patches[0], patches[1], patches[2]:
            await mcp_manager.connect("ha", "http://ha/mcp")

        result = await mcp_manager.call_tool("ha__slow", {})
        parsed = json.loads(result)
        assert "timed out" in parsed["error"]
        assert str(MCP_TOOL_TIMEOUT) in parsed["error"]


class TestCallToolError:

    @pytest.mark.asyncio
    async def test_call_tool_exception(self, mcp_manager: MCPManager, mock_mcp_session: AsyncMock):
        """Exception during tool execution returns a JSON error with type and message."""
        mock_mcp_session.list_tools.return_value = _make_list_tools_result(
            [_make_mcp_tool("broken")]
        )
        mock_mcp_session.call_tool.side_effect = RuntimeError("Something broke")

        patches = _patch_transports(mock_mcp_session)
        with patches[0], patches[1], patches[2]:
            await mcp_manager.connect("ha", "http://ha/mcp")

        result = await mcp_manager.call_tool("ha__broken", {})
        parsed = json.loads(result)
        assert parsed["error"] == "RuntimeError"
        assert parsed["error_text"] == "Something broke"


# ===================================================================
# MCPManager.refresh_tools
# ===================================================================


class TestRefreshTools:

    @pytest.mark.asyncio
    async def test_refresh_updates_tools(self, mcp_manager: MCPManager, mock_mcp_session: AsyncMock):
        # Initial tools
        mock_mcp_session.list_tools.return_value = _make_list_tools_result(
            [_make_mcp_tool("old_tool")]
        )

        patches = _patch_transports(mock_mcp_session)
        with patches[0], patches[1], patches[2]:
            await mcp_manager.connect("ha", "http://ha/mcp")

        assert len(mcp_manager.get_claude_tools()) == 1
        assert mcp_manager.get_claude_tools()[0]["name"] == "ha__old_tool"

        # Now server exposes different tools
        mock_mcp_session.list_tools.return_value = _make_list_tools_result(
            [_make_mcp_tool("new_tool_a"), _make_mcp_tool("new_tool_b")]
        )

        await mcp_manager.refresh_tools("ha")

        tools = mcp_manager.get_claude_tools()
        assert len(tools) == 2
        names = {t["name"] for t in tools}
        assert names == {"ha__new_tool_a", "ha__new_tool_b"}

    @pytest.mark.asyncio
    async def test_refresh_removes_old_registry_entries(
        self, mcp_manager: MCPManager, mock_mcp_session: AsyncMock
    ):
        mock_mcp_session.list_tools.return_value = _make_list_tools_result(
            [_make_mcp_tool("removed_tool")]
        )

        patches = _patch_transports(mock_mcp_session)
        with patches[0], patches[1], patches[2]:
            await mcp_manager.connect("ha", "http://ha/mcp")

        # tool should be callable
        mock_mcp_session.call_tool.return_value = _make_tool_result("ok")
        result = await mcp_manager.call_tool("ha__removed_tool", {})
        assert "error" not in result.lower()

        # Refresh with no tools
        mock_mcp_session.list_tools.return_value = _make_list_tools_result([])
        await mcp_manager.refresh_tools("ha")

        # Old tool should be gone from registry
        result = await mcp_manager.call_tool("ha__removed_tool", {})
        parsed = json.loads(result)
        assert "Unknown tool" in parsed["error"]

    @pytest.mark.asyncio
    async def test_refresh_nonexistent_server_is_noop(self, mcp_manager: MCPManager):
        await mcp_manager.refresh_tools("nonexistent")  # should not raise

    @pytest.mark.asyncio
    async def test_refresh_disconnected_session_is_noop(self, mcp_manager: MCPManager):
        """If session is None, refresh_tools returns without error."""
        conn = MCPServerConnection(name="dead", url="http://dead/mcp", session=None)
        mcp_manager._connections["dead"] = conn

        await mcp_manager.refresh_tools("dead")  # should not raise

    @pytest.mark.asyncio
    async def test_refresh_failure_preserves_connection(
        self, mcp_manager: MCPManager, mock_mcp_session: AsyncMock
    ):
        """If list_tools fails during refresh, old tools are gone but server stays connected."""
        mock_mcp_session.list_tools.return_value = _make_list_tools_result(
            [_make_mcp_tool("tool_x")]
        )

        patches = _patch_transports(mock_mcp_session)
        with patches[0], patches[1], patches[2]:
            await mcp_manager.connect("ha", "http://ha/mcp")

        assert len(mcp_manager.get_claude_tools()) == 1

        # Refresh fails
        mock_mcp_session.list_tools.side_effect = RuntimeError("refresh broke")
        await mcp_manager.refresh_tools("ha")

        # Server is still in connected_servers but old tools were removed
        # (the code removes old entries first, then tries list_tools; on failure it returns)
        assert "ha" in mcp_manager.connected_servers

    @pytest.mark.asyncio
    async def test_refresh_does_not_affect_other_servers(
        self, mcp_manager: MCPManager, mock_mcp_session: AsyncMock
    ):
        """Refreshing one server doesn't touch another server's tools."""
        call_count = 0

        async def list_tools_sequence():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _make_list_tools_result([_make_mcp_tool("a_tool")])
            elif call_count == 2:
                return _make_list_tools_result([_make_mcp_tool("b_tool")])
            else:
                return _make_list_tools_result([_make_mcp_tool("a_new_tool")])

        mock_mcp_session.list_tools = AsyncMock(side_effect=list_tools_sequence)

        patches = _patch_transports(mock_mcp_session)
        with patches[0], patches[1], patches[2]:
            await mcp_manager.connect("server_a", "http://a/mcp")
            await mcp_manager.connect("server_b", "http://b/mcp")

        # Refresh server_a
        await mcp_manager.refresh_tools("server_a")

        tools = mcp_manager.get_claude_tools()
        names = {t["name"] for t in tools}
        assert "server_b__b_tool" in names  # server_b untouched
        assert "server_a__a_new_tool" in names  # server_a refreshed


# ===================================================================
# MCPManager.connected_servers
# ===================================================================


class TestConnectedServers:

    def test_empty_initially(self, mcp_manager: MCPManager):
        assert mcp_manager.connected_servers == []

    @pytest.mark.asyncio
    async def test_reflects_connections(self, mcp_manager: MCPManager, mock_mcp_session: AsyncMock):
        mock_mcp_session.list_tools.return_value = _make_list_tools_result([])

        patches = _patch_transports(mock_mcp_session)
        with patches[0], patches[1], patches[2]:
            await mcp_manager.connect("s1", "http://a/mcp")

        assert mcp_manager.connected_servers == ["s1"]

    @pytest.mark.asyncio
    async def test_excludes_disconnected(self, mcp_manager: MCPManager):
        conn = MCPServerConnection(name="dead", url="http://dead/mcp", session=None)
        mcp_manager._connections["dead"] = conn

        assert mcp_manager.connected_servers == []


# ===================================================================
# MCPManager.ensure_connected
# ===================================================================


class TestEnsureConnected:

    @pytest.mark.asyncio
    async def test_returns_false_for_unknown_server(self, mcp_manager: MCPManager):
        result = await mcp_manager.ensure_connected("unknown")
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_true_if_session_healthy(
        self, mcp_manager: MCPManager, mock_mcp_session: AsyncMock
    ):
        mock_mcp_session.list_tools.return_value = _make_list_tools_result(
            [_make_mcp_tool("t1")]
        )

        patches = _patch_transports(mock_mcp_session)
        with patches[0], patches[1], patches[2]:
            await mcp_manager.connect("ha", "http://ha/mcp")

        result = await mcp_manager.ensure_connected("ha")
        assert result is True

    @pytest.mark.asyncio
    async def test_reconnects_when_session_is_none(
        self, mcp_manager: MCPManager, mock_mcp_session: AsyncMock
    ):
        """If session is None, ensure_connected attempts to reconnect."""
        mock_mcp_session.list_tools.return_value = _make_list_tools_result([])

        conn = MCPServerConnection(name="ha", url="http://ha/mcp", session=None)
        mcp_manager._connections["ha"] = conn

        patches = _patch_transports(mock_mcp_session)
        with patches[0], patches[1], patches[2]:
            result = await mcp_manager.ensure_connected("ha")

        assert result is True

    @pytest.mark.asyncio
    async def test_reconnects_when_ping_fails(
        self, mcp_manager: MCPManager, mock_mcp_session: AsyncMock
    ):
        """If the health-check list_tools() fails, ensure_connected reconnects."""
        call_count = 0

        async def list_tools_with_fail():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # initial connect succeeds
                return _make_list_tools_result([_make_mcp_tool("t1")])
            elif call_count == 2:
                # health check fails
                raise RuntimeError("connection lost")
            else:
                # reconnect list_tools succeeds
                return _make_list_tools_result([_make_mcp_tool("t1")])

        mock_mcp_session.list_tools = AsyncMock(side_effect=list_tools_with_fail)

        patches = _patch_transports(mock_mcp_session)
        with patches[0], patches[1], patches[2]:
            await mcp_manager.connect("ha", "http://ha/mcp")
            result = await mcp_manager.ensure_connected("ha")

        assert result is True
