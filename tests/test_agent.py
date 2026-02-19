"""Tests for agent.py – ConversationStateManager and run_agent_loop.

The agent loop now communicates with the Claude Agent add-on over HTTP+SSE.
Tests mock aiohttp.ClientSession to return fake SSE streams.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio  # noqa: F401 – enables async tests

from custom_components.claude_conversation_agent.agent import (
    ConversationState,
    ConversationStateManager,
    run_agent_loop,
)


# ===================================================================
# ConversationState
# ===================================================================


class TestConversationState:
    """Basic ConversationState dataclass behaviour."""

    def test_defaults(self):
        state = ConversationState()
        assert state.session_id is None
        assert isinstance(state.created, float)
        assert isinstance(state.last_accessed, float)

    def test_session_id_stored(self):
        state = ConversationState()
        state.session_id = "test-session-123"
        assert state.session_id == "test-session-123"


# ===================================================================
# ConversationStateManager
# ===================================================================


class TestConversationStateManager:
    """TTL cache semantics."""

    def test_get_or_create_new(self, state_manager: ConversationStateManager):
        state = state_manager.get_or_create("conv-1")
        assert isinstance(state, ConversationState)
        assert state.session_id is None

    def test_get_or_create_returns_same(self, state_manager: ConversationStateManager):
        s1 = state_manager.get_or_create("conv-1")
        s1.session_id = "session-abc"
        s2 = state_manager.get_or_create("conv-1")
        assert s1 is s2
        assert s2.session_id == "session-abc"

    def test_different_ids_different_states(self, state_manager: ConversationStateManager):
        s1 = state_manager.get_or_create("conv-1")
        s2 = state_manager.get_or_create("conv-2")
        assert s1 is not s2

    def test_last_accessed_updated_on_get(self, state_manager: ConversationStateManager):
        s = state_manager.get_or_create("conv-1")
        original = s.last_accessed
        with patch("time.monotonic", return_value=original + 5.0):
            s2 = state_manager.get_or_create("conv-1")
        assert s2.last_accessed == original + 5.0

    def test_ttl_expiry(self):
        mgr = ConversationStateManager(ttl_seconds=10.0)
        base = 1000.0

        with patch("time.monotonic", return_value=base):
            s1 = mgr.get_or_create("conv-1")
            s1.session_id = "old-session"

        # Advance beyond TTL
        with patch("time.monotonic", return_value=base + 11.0):
            s = mgr.get_or_create("conv-1")
        # Should be a *new* state with no session_id
        assert s.session_id is None

    def test_ttl_not_expired_if_recently_accessed(self):
        mgr = ConversationStateManager(ttl_seconds=10.0)
        base = 1000.0

        with patch("time.monotonic", return_value=base):
            s1 = mgr.get_or_create("conv-1")
            s1.session_id = "active-session"

        # Access at base+8 (within TTL)
        with patch("time.monotonic", return_value=base + 8.0):
            s2 = mgr.get_or_create("conv-1")
        assert s2 is s1
        assert s2.session_id == "active-session"

        # Now at base+17: 17-8=9 < 10 TTL, so still alive
        with patch("time.monotonic", return_value=base + 17.0):
            s3 = mgr.get_or_create("conv-1")
        assert s3 is s1

    def test_remove(self, state_manager: ConversationStateManager):
        state_manager.get_or_create("conv-1")
        state_manager.remove("conv-1")
        s = state_manager.get_or_create("conv-1")
        assert s.session_id is None

    def test_remove_nonexistent_is_noop(self, state_manager: ConversationStateManager):
        state_manager.remove("does-not-exist")  # should not raise

    def test_clear(self, state_manager: ConversationStateManager):
        state_manager.get_or_create("conv-1")
        state_manager.get_or_create("conv-2")
        state_manager.clear()
        s1 = state_manager.get_or_create("conv-1")
        s2 = state_manager.get_or_create("conv-2")
        assert s1.session_id is None
        assert s2.session_id is None


# ===================================================================
# Helpers for run_agent_loop tests
# ===================================================================


def _sse_line(data: dict[str, Any]) -> bytes:
    """Encode a dict as an SSE data line."""
    return f"data: {json.dumps(data)}\n\n".encode("utf-8")


class FakeSSEContent:
    """Async iterable that yields SSE lines, mimicking aiohttp response.content."""

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._lines = [_sse_line(e) for e in events]

    def __aiter__(self):
        return self._async_gen()

    async def _async_gen(self):
        for line in self._lines:
            yield line


def _mock_session_for_events(events: list[dict[str, Any]], captured: dict | None = None):
    """Create a mock aiohttp.ClientSession that returns the given SSE events.

    If `captured` is provided, the request payload will be stored in it.
    """
    # Build response mock with real async iterable content
    response = MagicMock()
    response.status = 200
    response.content = FakeSSEContent(events)
    response.text = AsyncMock(return_value="")

    # Build the async context manager for post()
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=response)
    cm.__aexit__ = AsyncMock(return_value=False)

    if captured is not None:
        def capturing_post(url, **kwargs):
            captured["url"] = url
            captured["json"] = kwargs.get("json", {})
            return cm

        post_fn = MagicMock(side_effect=capturing_post)
    else:
        post_fn = MagicMock(return_value=cm)

    session_mock = MagicMock()
    session_mock.post = post_fn
    session_mock.__aenter__ = AsyncMock(return_value=session_mock)
    session_mock.__aexit__ = AsyncMock(return_value=False)

    return session_mock


# Default args shared across tests
_DEFAULT_LOOP_ARGS: dict[str, Any] = {
    "addon_url": "http://localhost:3000",
    "system_prompt": "You are helpful.",
    "user_text": "Hi",
    "model": "claude-sonnet-4-5",
    "max_tokens": 1024,
    "temperature": 1.0,
    "auth_mode": "api_key",
    "api_key": "test-api-key",
}


# ===================================================================
# run_agent_loop tests
# ===================================================================


class TestRunAgentLoopSimpleText:
    """Add-on responds with plain text (no tool use)."""

    @pytest.mark.asyncio
    async def test_yields_role_then_content(self, conversation_state):
        events = [
            {"type": "init", "session_id": "test-session", "mcp_servers": []},
            {"type": "role", "role": "assistant"},
            {"type": "delta", "content": "Hello "},
            {"type": "delta", "content": "there!"},
            {"type": "result", "session_id": "test-session", "is_error": False},
        ]

        session_mock = _mock_session_for_events(events)

        with patch("aiohttp.ClientSession", return_value=session_mock):
            collected: list[dict] = []
            async for delta in run_agent_loop(
                **_DEFAULT_LOOP_ARGS,
                conversation_state=conversation_state,
            ):
                collected.append(delta)

        # First yield is role marker
        assert collected[0] == {"role": "assistant"}
        # Then text deltas
        assert collected[1] == {"content": "Hello "}
        assert collected[2] == {"content": "there!"}

    @pytest.mark.asyncio
    async def test_session_id_stored_in_state(self, conversation_state):
        events = [
            {"type": "init", "session_id": "session-xyz", "mcp_servers": []},
            {"type": "role", "role": "assistant"},
            {"type": "delta", "content": "Hi"},
            {"type": "result", "session_id": "session-xyz", "is_error": False},
        ]

        session_mock = _mock_session_for_events(events)

        with patch("aiohttp.ClientSession", return_value=session_mock):
            async for _ in run_agent_loop(
                **_DEFAULT_LOOP_ARGS,
                conversation_state=conversation_state,
            ):
                pass

        assert conversation_state.session_id == "session-xyz"

    @pytest.mark.asyncio
    async def test_resume_session_id_sent_in_payload(self, conversation_state):
        """When conversation_state has a session_id, it should be sent in the request."""
        conversation_state.session_id = "prev-session"

        events = [
            {"type": "init", "session_id": "new-session", "mcp_servers": []},
            {"type": "role", "role": "assistant"},
            {"type": "delta", "content": "Continued"},
            {"type": "result", "session_id": "new-session", "is_error": False},
        ]

        captured: dict[str, Any] = {}
        session_mock = _mock_session_for_events(events, captured=captured)

        with patch("aiohttp.ClientSession", return_value=session_mock):
            async for _ in run_agent_loop(
                **_DEFAULT_LOOP_ARGS,
                conversation_state=conversation_state,
            ):
                pass

        assert captured["json"]["session_id"] == "prev-session"
        # session_id updated to new session
        assert conversation_state.session_id == "new-session"


class TestRunAgentLoopStreamingDeltas:
    """Verify the shape / ordering of yielded deltas."""

    @pytest.mark.asyncio
    async def test_empty_content_not_yielded(self, conversation_state):
        """Empty content strings should not produce deltas."""
        events = [
            {"type": "init", "session_id": "s1", "mcp_servers": []},
            {"type": "role", "role": "assistant"},
            {"type": "delta", "content": ""},
            {"type": "delta", "content": "hello"},
            {"type": "result", "session_id": "s1", "is_error": False},
        ]

        session_mock = _mock_session_for_events(events)

        with patch("aiohttp.ClientSession", return_value=session_mock):
            collected = []
            async for delta in run_agent_loop(
                **_DEFAULT_LOOP_ARGS,
                conversation_state=conversation_state,
            ):
                collected.append(delta)

        assert collected == [{"role": "assistant"}, {"content": "hello"}]

    @pytest.mark.asyncio
    async def test_no_output_when_no_deltas(self, conversation_state):
        """If the add-on only produces init and result, nothing is yielded."""
        events = [
            {"type": "init", "session_id": "s1", "mcp_servers": []},
            {"type": "result", "session_id": "s1", "is_error": False},
        ]

        session_mock = _mock_session_for_events(events)

        with patch("aiohttp.ClientSession", return_value=session_mock):
            collected = []
            async for delta in run_agent_loop(
                **_DEFAULT_LOOP_ARGS,
                conversation_state=conversation_state,
            ):
                collected.append(delta)

        assert collected == []


class TestRunAgentLoopPayload:
    """Verify that the correct payload is sent to the add-on."""

    @pytest.mark.asyncio
    async def test_payload_contains_all_fields(self, conversation_state):
        events = [
            {"type": "init", "session_id": "s1", "mcp_servers": []},
            {"type": "role", "role": "assistant"},
            {"type": "delta", "content": "ok"},
            {"type": "result", "session_id": "s1", "is_error": False},
        ]

        captured: dict[str, Any] = {}
        session_mock = _mock_session_for_events(events, captured=captured)

        with patch("aiohttp.ClientSession", return_value=session_mock):
            async for _ in run_agent_loop(
                addon_url="http://my-addon:3000",
                system_prompt="You are a smart home assistant.",
                user_text="Turn on the lights",
                conversation_state=conversation_state,
                model="claude-opus-4-6",
                max_tokens=2048,
                temperature=0.5,
                auth_mode="api_key",
                api_key="sk-ant-test-key-123",
            ):
                pass

        assert captured["url"] == "http://my-addon:3000/api/chat"
        payload = captured["json"]
        assert payload["system_prompt"] == "You are a smart home assistant."
        assert payload["user_text"] == "Turn on the lights"
        assert payload["model"] == "claude-opus-4-6"
        assert payload["auth_mode"] == "api_key"
        assert payload["api_key"] == "sk-ant-test-key-123"

    @pytest.mark.asyncio
    async def test_max_auth_no_api_key(self, conversation_state):
        """In max auth mode, no api_key should be in the payload."""
        events = [
            {"type": "init", "session_id": "s1", "mcp_servers": []},
            {"type": "role", "role": "assistant"},
            {"type": "delta", "content": "ok"},
            {"type": "result", "session_id": "s1", "is_error": False},
        ]

        captured: dict[str, Any] = {}
        session_mock = _mock_session_for_events(events, captured=captured)

        with patch("aiohttp.ClientSession", return_value=session_mock):
            async for _ in run_agent_loop(
                addon_url="http://my-addon:3000",
                system_prompt="sys",
                user_text="Hi",
                conversation_state=conversation_state,
                model="claude-sonnet-4-5",
                max_tokens=1024,
                temperature=1.0,
                auth_mode="max",
                api_key=None,
            ):
                pass

        payload = captured["json"]
        assert payload["auth_mode"] == "max"
        assert "api_key" not in payload


class TestRunAgentLoopErrorHandling:
    """Verify error reporting from the add-on."""

    @pytest.mark.asyncio
    async def test_error_result_handled(self, conversation_state):
        """When result has is_error=True, it should still complete gracefully."""
        events = [
            {"type": "init", "session_id": "s1", "mcp_servers": []},
            {"type": "result", "session_id": "s1", "is_error": True, "error": "Connection failed"},
        ]

        session_mock = _mock_session_for_events(events)

        with patch("aiohttp.ClientSession", return_value=session_mock):
            collected = []
            async for delta in run_agent_loop(
                **_DEFAULT_LOOP_ARGS,
                conversation_state=conversation_state,
            ):
                collected.append(delta)

        # No text deltas since there was an error
        assert collected == []
        # Session ID should still be stored
        assert conversation_state.session_id == "s1"

    @pytest.mark.asyncio
    async def test_http_error_raises(self, conversation_state):
        """Non-200 HTTP status should raise RuntimeError."""
        response = AsyncMock()
        response.status = 500
        response.text = AsyncMock(return_value="Internal Server Error")

        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=response)
        cm.__aexit__ = AsyncMock(return_value=False)

        session_mock = AsyncMock()
        session_mock.post = MagicMock(return_value=cm)
        session_mock.__aenter__ = AsyncMock(return_value=session_mock)
        session_mock.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=session_mock):
            with pytest.raises(RuntimeError, match="HTTP 500"):
                async for _ in run_agent_loop(
                    **_DEFAULT_LOOP_ARGS,
                    conversation_state=conversation_state,
                ):
                    pass

    @pytest.mark.asyncio
    async def test_mcp_connection_failure_in_init(self, conversation_state):
        """MCP server failure in init message should be handled gracefully."""
        events = [
            {
                "type": "init",
                "session_id": "s1",
                "mcp_servers": [
                    {"name": "ha", "status": "failed", "error": "Connection refused"}
                ],
            },
            {"type": "role", "role": "assistant"},
            {"type": "delta", "content": "I couldn't connect to tools."},
            {"type": "result", "session_id": "s1", "is_error": False},
        ]

        session_mock = _mock_session_for_events(events)

        with patch("aiohttp.ClientSession", return_value=session_mock):
            collected = []
            async for delta in run_agent_loop(
                **_DEFAULT_LOOP_ARGS,
                conversation_state=conversation_state,
            ):
                collected.append(delta)

        # Should still yield text even if MCP failed
        assert {"role": "assistant"} in collected
        assert {"content": "I couldn't connect to tools."} in collected
