"""Tests for agent.py – ConversationStateManager and run_agent_loop.

The agent loop now uses the Claude Agent SDK (claude_agent_sdk.query).
Tests mock the SDK's query() function and verify that streaming text
deltas are correctly extracted from StreamEvent messages.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio  # noqa: F401 – enables async tests

from custom_components.claude_conversation_agent.agent import (
    ConversationState,
    ConversationStateManager,
    run_agent_loop,
)
from custom_components.claude_conversation_agent.const import MAX_TOOL_ITERATIONS


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


def _make_init_message(session_id: str = "test-session", mcp_servers: list | None = None):
    """Create a SystemMessage(subtype='init') from the Agent SDK."""
    from claude_agent_sdk import SystemMessage
    return SystemMessage(
        subtype="init",
        data={"mcp_servers": mcp_servers or [], "session_id": session_id},
        session_id=session_id,
    )


def _make_result_message(
    session_id: str = "test-session",
    num_turns: int = 1,
    is_error: bool = False,
    result: str | None = None,
):
    """Create a ResultMessage from the Agent SDK."""
    from claude_agent_sdk import ResultMessage
    return ResultMessage(
        subtype="success" if not is_error else "error",
        duration_ms=100,
        duration_api_ms=80,
        is_error=is_error,
        num_turns=num_turns,
        session_id=session_id,
        total_cost_usd=0.001,
        result=result,
    )


def _make_stream_event(event: dict[str, Any], session_id: str = "test-session"):
    """Create a StreamEvent wrapping a raw API event."""
    from claude_agent_sdk import StreamEvent
    return StreamEvent(
        uuid="evt-1",
        session_id=session_id,
        event=event,
    )


def _text_block_start_event(session_id: str = "test-session"):
    """StreamEvent for content_block_start with type=text."""
    return _make_stream_event(
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        session_id,
    )


def _text_delta_event(text: str, session_id: str = "test-session"):
    """StreamEvent for content_block_delta with text_delta."""
    return _make_stream_event(
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
        session_id,
    )


def _tool_use_block_start_event(session_id: str = "test-session"):
    """StreamEvent for content_block_start with type=tool_use (should NOT yield text)."""
    return _make_stream_event(
        {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "tu_1", "name": "ha__test"}},
        session_id,
    )


def _input_json_delta_event(session_id: str = "test-session"):
    """StreamEvent for content_block_delta with input_json_delta (should NOT yield text)."""
    return _make_stream_event(
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "{}"}},
        session_id,
    )


async def _fake_query_from_messages(messages: list):
    """Create an async generator from a list of SDK messages."""
    for msg in messages:
        yield msg


# ===================================================================
# run_agent_loop tests
# ===================================================================

# Default args shared across tests
_DEFAULT_LOOP_ARGS: dict[str, Any] = {
    "api_key": "test-api-key",
    "mcp_server_url": None,
    "mcp_server_token": None,
    "system_prompt": "You are helpful.",
    "user_text": "Hi",
    "model": "claude-sonnet-4-5",
    "max_tokens": 1024,
    "temperature": 1.0,
}


class TestRunAgentLoopSimpleText:
    """Claude responds with plain text (no tool use)."""

    @pytest.mark.asyncio
    async def test_yields_role_then_content(self, conversation_state):
        messages = [
            _make_init_message(),
            _text_block_start_event(),
            _text_delta_event("Hello "),
            _text_delta_event("there!"),
            _make_result_message(),
        ]

        with patch(
            "claude_agent_sdk.query",
            return_value=_fake_query_from_messages(messages),
        ):
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
        messages = [
            _make_init_message(session_id="session-xyz"),
            _text_block_start_event(session_id="session-xyz"),
            _text_delta_event("Hi", session_id="session-xyz"),
            _make_result_message(session_id="session-xyz"),
        ]

        with patch(
            "claude_agent_sdk.query",
            return_value=_fake_query_from_messages(messages),
        ):
            async for _ in run_agent_loop(
                **_DEFAULT_LOOP_ARGS,
                conversation_state=conversation_state,
            ):
                pass

        assert conversation_state.session_id == "session-xyz"

    @pytest.mark.asyncio
    async def test_resume_passed_when_session_exists(self, conversation_state):
        """When conversation_state has a session_id, options.resume should be set."""
        conversation_state.session_id = "prev-session"

        messages = [
            _make_init_message(session_id="new-session"),
            _text_block_start_event(),
            _text_delta_event("Continued"),
            _make_result_message(session_id="new-session"),
        ]

        captured_options = {}

        async def capturing_query(prompt, options=None):
            captured_options["resume"] = options.resume if options else None
            async for msg in _fake_query_from_messages(messages):
                yield msg

        with patch(
            "claude_agent_sdk.query",
            side_effect=capturing_query,
        ):
            async for _ in run_agent_loop(
                **_DEFAULT_LOOP_ARGS,
                conversation_state=conversation_state,
            ):
                pass

        assert captured_options["resume"] == "prev-session"
        # session_id updated to new session
        assert conversation_state.session_id == "new-session"


class TestRunAgentLoopMCPConfig:
    """Verify MCP server configuration is passed correctly to the SDK."""

    @pytest.mark.asyncio
    async def test_mcp_server_configured(self, conversation_state):
        messages = [
            _make_init_message(),
            _text_block_start_event(),
            _text_delta_event("ok"),
            _make_result_message(),
        ]

        captured_options = {}

        async def capturing_query(prompt, options=None):
            captured_options["mcp_servers"] = options.mcp_servers if options else {}
            captured_options["allowed_tools"] = options.allowed_tools if options else []
            async for msg in _fake_query_from_messages(messages):
                yield msg

        with patch(
            "claude_agent_sdk.query",
            side_effect=capturing_query,
        ):
            async for _ in run_agent_loop(
                api_key="test-key",
                mcp_server_url="http://localhost:8123/mcp",
                mcp_server_token="secret-token",
                system_prompt="sys",
                user_text="Hi",
                conversation_state=conversation_state,
                model="claude-sonnet-4-5",
                max_tokens=1024,
                temperature=1.0,
            ):
                pass

        # MCP server should be configured as SSE with auth header
        assert "ha" in captured_options["mcp_servers"]
        ha_config = captured_options["mcp_servers"]["ha"]
        assert ha_config["type"] == "sse"
        assert ha_config["url"] == "http://localhost:8123/mcp"
        assert ha_config["headers"]["Authorization"] == "Bearer secret-token"

        # Allowed tools should include wildcard for HA MCP
        assert captured_options["allowed_tools"] == ["mcp__ha__*"]

    @pytest.mark.asyncio
    async def test_no_mcp_when_url_not_set(self, conversation_state):
        messages = [
            _make_init_message(),
            _text_block_start_event(),
            _text_delta_event("ok"),
            _make_result_message(),
        ]

        captured_options = {}

        async def capturing_query(prompt, options=None):
            captured_options["mcp_servers"] = options.mcp_servers if options else {}
            captured_options["allowed_tools"] = options.allowed_tools if options else []
            async for msg in _fake_query_from_messages(messages):
                yield msg

        with patch(
            "claude_agent_sdk.query",
            side_effect=capturing_query,
        ):
            async for _ in run_agent_loop(
                **_DEFAULT_LOOP_ARGS,
                conversation_state=conversation_state,
            ):
                pass

        assert captured_options["mcp_servers"] == {}
        assert captured_options["allowed_tools"] == []

    @pytest.mark.asyncio
    async def test_mcp_without_token(self, conversation_state):
        messages = [
            _make_init_message(),
            _text_block_start_event(),
            _text_delta_event("ok"),
            _make_result_message(),
        ]

        captured_options = {}

        async def capturing_query(prompt, options=None):
            captured_options["mcp_servers"] = options.mcp_servers if options else {}
            async for msg in _fake_query_from_messages(messages):
                yield msg

        with patch(
            "claude_agent_sdk.query",
            side_effect=capturing_query,
        ):
            async for _ in run_agent_loop(
                api_key="test-key",
                mcp_server_url="http://localhost:8123/mcp",
                mcp_server_token=None,
                system_prompt="sys",
                user_text="Hi",
                conversation_state=conversation_state,
                model="claude-sonnet-4-5",
                max_tokens=1024,
                temperature=1.0,
            ):
                pass

        ha_config = captured_options["mcp_servers"]["ha"]
        assert "headers" not in ha_config


class TestRunAgentLoopStreamingDeltas:
    """Verify the shape / ordering of yielded deltas."""

    @pytest.mark.asyncio
    async def test_tool_use_events_not_yielded(self, conversation_state):
        """tool_use stream events should NOT produce text deltas."""
        messages = [
            _make_init_message(),
            # Tool use block (should not yield)
            _tool_use_block_start_event(),
            _input_json_delta_event(),
            # Then text block
            _text_block_start_event(),
            _text_delta_event("done"),
            _make_result_message(),
        ]

        with patch(
            "claude_agent_sdk.query",
            return_value=_fake_query_from_messages(messages),
        ):
            collected = []
            async for delta in run_agent_loop(
                **_DEFAULT_LOOP_ARGS,
                conversation_state=conversation_state,
            ):
                collected.append(delta)

        # Only role marker + text delta
        assert collected == [{"role": "assistant"}, {"content": "done"}]

    @pytest.mark.asyncio
    async def test_role_only_yielded_once(self, conversation_state):
        """Even with multiple text deltas, role marker is yielded only once."""
        messages = [
            _make_init_message(),
            _text_block_start_event(),
            _text_delta_event("Hello "),
            _text_delta_event("world"),
            _make_result_message(),
        ]

        with patch(
            "claude_agent_sdk.query",
            return_value=_fake_query_from_messages(messages),
        ):
            collected = []
            async for delta in run_agent_loop(
                **_DEFAULT_LOOP_ARGS,
                conversation_state=conversation_state,
            ):
                collected.append(delta)

        role_markers = [d for d in collected if "role" in d]
        assert len(role_markers) == 1

    @pytest.mark.asyncio
    async def test_no_output_when_no_text(self, conversation_state):
        """If the SDK only produces tool use (no text), nothing is yielded."""
        messages = [
            _make_init_message(),
            _tool_use_block_start_event(),
            _input_json_delta_event(),
            _make_result_message(),
        ]

        with patch(
            "claude_agent_sdk.query",
            return_value=_fake_query_from_messages(messages),
        ):
            collected = []
            async for delta in run_agent_loop(
                **_DEFAULT_LOOP_ARGS,
                conversation_state=conversation_state,
            ):
                collected.append(delta)

        assert collected == []


class TestRunAgentLoopOptions:
    """Verify that options are correctly passed to the SDK."""

    @pytest.mark.asyncio
    async def test_model_and_system_prompt_passed(self, conversation_state):
        messages = [
            _make_init_message(),
            _text_block_start_event(),
            _text_delta_event("ok"),
            _make_result_message(),
        ]

        captured_options = {}
        captured_prompt = {}

        async def capturing_query(prompt, options=None):
            captured_prompt["prompt"] = prompt
            captured_options["model"] = options.model if options else None
            captured_options["system_prompt"] = options.system_prompt if options else None
            captured_options["max_turns"] = options.max_turns if options else None
            async for msg in _fake_query_from_messages(messages):
                yield msg

        with patch(
            "claude_agent_sdk.query",
            side_effect=capturing_query,
        ):
            async for _ in run_agent_loop(
                api_key="test-key",
                mcp_server_url=None,
                mcp_server_token=None,
                system_prompt="You are a smart home assistant.",
                user_text="Turn on the lights",
                conversation_state=conversation_state,
                model="claude-opus-4-6",
                max_tokens=2048,
                temperature=0.5,
            ):
                pass

        assert captured_prompt["prompt"] == "Turn on the lights"
        assert captured_options["model"] == "claude-opus-4-6"
        assert captured_options["system_prompt"] == "You are a smart home assistant."
        assert captured_options["max_turns"] == MAX_TOOL_ITERATIONS

    @pytest.mark.asyncio
    async def test_api_key_passed_via_env(self, conversation_state):
        messages = [
            _make_init_message(),
            _text_block_start_event(),
            _text_delta_event("ok"),
            _make_result_message(),
        ]

        captured_env = {}

        async def capturing_query(prompt, options=None):
            captured_env.update(options.env if options else {})
            async for msg in _fake_query_from_messages(messages):
                yield msg

        with patch(
            "claude_agent_sdk.query",
            side_effect=capturing_query,
        ):
            async for _ in run_agent_loop(
                api_key="sk-ant-test-key-123",
                mcp_server_url=None,
                mcp_server_token=None,
                system_prompt="sys",
                user_text="Hi",
                conversation_state=conversation_state,
                model="claude-sonnet-4-5",
                max_tokens=1024,
                temperature=1.0,
            ):
                pass

        assert captured_env["ANTHROPIC_API_KEY"] == "sk-ant-test-key-123"


class TestRunAgentLoopErrorHandling:
    """Verify error reporting from the Agent SDK."""

    @pytest.mark.asyncio
    async def test_error_result_logged(self, conversation_state):
        """When ResultMessage.is_error is True, it should still complete gracefully."""
        messages = [
            _make_init_message(),
            _make_result_message(is_error=True, result="Connection failed"),
        ]

        with patch(
            "claude_agent_sdk.query",
            return_value=_fake_query_from_messages(messages),
        ):
            collected = []
            async for delta in run_agent_loop(
                **_DEFAULT_LOOP_ARGS,
                conversation_state=conversation_state,
            ):
                collected.append(delta)

        # No text deltas since there was an error
        assert collected == []
        # Session ID should still be stored
        assert conversation_state.session_id == "test-session"

    @pytest.mark.asyncio
    async def test_mcp_connection_failure_logged(self, conversation_state):
        """MCP server failure in init message should be handled gracefully."""
        messages = [
            _make_init_message(
                mcp_servers=[
                    {"name": "ha", "status": "failed", "error": "Connection refused"}
                ]
            ),
            _text_block_start_event(),
            _text_delta_event("I couldn't connect to tools."),
            _make_result_message(),
        ]

        with patch(
            "claude_agent_sdk.query",
            return_value=_fake_query_from_messages(messages),
        ):
            collected = []
            async for delta in run_agent_loop(
                **{**_DEFAULT_LOOP_ARGS, "mcp_server_url": "http://ha:8123/mcp"},
                conversation_state=conversation_state,
            ):
                collected.append(delta)

        # Should still yield text even if MCP failed
        assert {"role": "assistant"} in collected
        assert {"content": "I couldn't connect to tools."} in collected
