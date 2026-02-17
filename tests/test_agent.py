"""Tests for agent.py – ConversationStateManager and run_agent_loop."""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

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
        assert state.messages == []
        assert isinstance(state.created, float)
        assert isinstance(state.last_accessed, float)

    def test_messages_are_independent(self):
        s1 = ConversationState()
        s2 = ConversationState()
        s1.messages.append({"role": "user", "content": "hi"})
        assert s2.messages == []


# ===================================================================
# ConversationStateManager
# ===================================================================


class TestConversationStateManager:
    """TTL cache semantics."""

    def test_get_or_create_new(self, state_manager: ConversationStateManager):
        state = state_manager.get_or_create("conv-1")
        assert isinstance(state, ConversationState)
        assert state.messages == []

    def test_get_or_create_returns_same(self, state_manager: ConversationStateManager):
        s1 = state_manager.get_or_create("conv-1")
        s1.messages.append({"role": "user", "content": "hello"})
        s2 = state_manager.get_or_create("conv-1")
        assert s1 is s2
        assert len(s2.messages) == 1

    def test_different_ids_different_states(self, state_manager: ConversationStateManager):
        s1 = state_manager.get_or_create("conv-1")
        s2 = state_manager.get_or_create("conv-2")
        assert s1 is not s2

    def test_last_accessed_updated_on_get(self, state_manager: ConversationStateManager):
        s = state_manager.get_or_create("conv-1")
        original = s.last_accessed
        # Patch monotonic to return a later time
        with patch("time.monotonic", return_value=original + 5.0):
            s2 = state_manager.get_or_create("conv-1")
        assert s2.last_accessed == original + 5.0

    def test_ttl_expiry(self):
        mgr = ConversationStateManager(ttl_seconds=10.0)
        base = 1000.0

        with patch("time.monotonic", return_value=base):
            mgr.get_or_create("conv-1")

        # Advance beyond TTL
        with patch("time.monotonic", return_value=base + 11.0):
            s = mgr.get_or_create("conv-1")
        # Should be a *new* state with empty messages
        assert s.messages == []

    def test_ttl_not_expired_if_recently_accessed(self):
        mgr = ConversationStateManager(ttl_seconds=10.0)
        base = 1000.0

        with patch("time.monotonic", return_value=base):
            s1 = mgr.get_or_create("conv-1")
            s1.messages.append({"role": "user", "content": "ping"})

        # Access at base+8 (within TTL)
        with patch("time.monotonic", return_value=base + 8.0):
            s2 = mgr.get_or_create("conv-1")
        assert s2 is s1
        assert len(s2.messages) == 1

        # Now at base+17: 17-8=9 < 10 TTL, so still alive
        with patch("time.monotonic", return_value=base + 17.0):
            s3 = mgr.get_or_create("conv-1")
        assert s3 is s1

    def test_ttl_expires_after_last_access(self):
        mgr = ConversationStateManager(ttl_seconds=10.0)
        base = 1000.0

        with patch("time.monotonic", return_value=base):
            s1 = mgr.get_or_create("conv-1")
            s1.messages.append({"role": "user", "content": "ping"})

        # Access at base+5
        with patch("time.monotonic", return_value=base + 5.0):
            mgr.get_or_create("conv-1")

        # At base+16: 16-5=11 > 10 TTL => expired
        with patch("time.monotonic", return_value=base + 16.0):
            s2 = mgr.get_or_create("conv-1")
        assert s2 is not s1
        assert s2.messages == []

    def test_remove(self, state_manager: ConversationStateManager):
        state_manager.get_or_create("conv-1")
        state_manager.remove("conv-1")
        s = state_manager.get_or_create("conv-1")
        assert s.messages == []

    def test_remove_nonexistent_is_noop(self, state_manager: ConversationStateManager):
        state_manager.remove("does-not-exist")  # should not raise

    def test_clear(self, state_manager: ConversationStateManager):
        state_manager.get_or_create("conv-1")
        state_manager.get_or_create("conv-2")
        state_manager.clear()
        # Both should be new (empty) after clear
        s1 = state_manager.get_or_create("conv-1")
        s2 = state_manager.get_or_create("conv-2")
        assert s1.messages == []
        assert s2.messages == []

    def test_cleanup_only_removes_expired(self):
        mgr = ConversationStateManager(ttl_seconds=10.0)
        base = 1000.0

        with patch("time.monotonic", return_value=base):
            s1 = mgr.get_or_create("old")
            s1.messages.append({"role": "user", "content": "old"})

        with patch("time.monotonic", return_value=base + 8.0):
            s2 = mgr.get_or_create("new")
            s2.messages.append({"role": "user", "content": "new"})

        # At base+12: old is 12s stale (>10), new is 4s stale (<10)
        with patch("time.monotonic", return_value=base + 12.0):
            old = mgr.get_or_create("old")
            new = mgr.get_or_create("new")

        assert old.messages == []  # expired => recreated
        assert new is s2  # still alive


# ===================================================================
# Helpers for run_agent_loop tests
# ===================================================================


def _make_text_block(text: str) -> SimpleNamespace:
    """Create a mock content block of type 'text'."""
    block = SimpleNamespace(type="text", text=text)
    block.model_dump = lambda: {"type": "text", "text": text}
    return block


def _make_tool_use_block(
    tool_id: str, name: str, input_args: dict[str, Any]
) -> SimpleNamespace:
    """Create a mock content block of type 'tool_use'."""
    block = SimpleNamespace(type="tool_use", id=tool_id, name=name, input=input_args)
    block.model_dump = lambda: {
        "type": "tool_use",
        "id": tool_id,
        "name": name,
        "input": input_args,
    }
    return block


def _make_final_message(
    stop_reason: str, content_blocks: list
) -> SimpleNamespace:
    """Create a mock final message returned by stream.get_final_message()."""
    return SimpleNamespace(stop_reason=stop_reason, content=content_blocks)


def _content_block_start_event(block: SimpleNamespace) -> SimpleNamespace:
    """Wrap a content block as a content_block_start event."""
    return SimpleNamespace(type="content_block_start", content_block=block)


def _text_delta_event(text: str) -> SimpleNamespace:
    """Create a content_block_delta event with a text_delta."""
    return SimpleNamespace(
        type="content_block_delta",
        delta=SimpleNamespace(type="text_delta", text=text),
    )


def _tool_use_delta_event() -> SimpleNamespace:
    """Create a content_block_delta for a tool_use (should NOT be yielded)."""
    return SimpleNamespace(
        type="content_block_delta",
        delta=SimpleNamespace(type="input_json_delta", partial_json="{}"),
    )


class MockStream:
    """A mock async-iterable stream that also has get_final_message().

    Usage:
        stream = MockStream(events=[...], final_message=msg)
        async with stream:  # works as async context manager
            async for event in stream:
                ...
            final = await stream.get_final_message()
    """

    def __init__(
        self,
        events: list,
        final_message: SimpleNamespace,
    ) -> None:
        self._events = events
        self._final_message = final_message

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def __aiter__(self):
        return self._aiter_events()

    async def _aiter_events(self):
        for event in self._events:
            yield event

    async def get_final_message(self):
        return self._final_message


def _setup_client_single_response(
    mock_client: AsyncMock,
    events: list,
    final_message: SimpleNamespace,
) -> None:
    """Configure mock_client.messages.stream() for a single response cycle."""
    stream = MockStream(events, final_message)
    mock_client.messages.stream = MagicMock(return_value=stream)


def _setup_client_multi_response(
    mock_client: AsyncMock,
    responses: list[tuple[list, SimpleNamespace]],
) -> None:
    """Configure mock_client.messages.stream() for multiple sequential calls.

    Each element of ``responses`` is (events_list, final_message).
    """
    streams = [MockStream(events, fm) for events, fm in responses]
    mock_client.messages.stream = MagicMock(side_effect=streams)


# ===================================================================
# run_agent_loop tests
# ===================================================================


class TestRunAgentLoopSimpleText:
    """Claude responds with plain text (no tool use)."""

    @pytest.mark.asyncio
    async def test_yields_role_then_content(
        self, mock_anthropic_client, conversation_state
    ):
        text_block = _make_text_block("Hello there!")
        events = [
            _content_block_start_event(text_block),
            _text_delta_event("Hello "),
            _text_delta_event("there!"),
        ]
        final = _make_final_message("end_turn", [text_block])
        _setup_client_single_response(mock_anthropic_client, events, final)

        mcp = MagicMock()
        mcp.get_claude_tools = MagicMock(return_value=[])

        collected: list[dict] = []
        async for delta in run_agent_loop(
            client=mock_anthropic_client,
            mcp_manager=mcp,
            system_prompt="You are helpful.",
            user_text="Hi",
            conversation_state=conversation_state,
            model="claude-sonnet-4-5",
            max_tokens=1024,
            temperature=1.0,
        ):
            collected.append(delta)

        # First yield is role marker
        assert collected[0] == {"role": "assistant"}
        # Then text deltas
        assert collected[1] == {"content": "Hello "}
        assert collected[2] == {"content": "there!"}

    @pytest.mark.asyncio
    async def test_user_message_appended_to_state(
        self, mock_anthropic_client, conversation_state
    ):
        text_block = _make_text_block("Reply")
        events = [
            _content_block_start_event(text_block),
            _text_delta_event("Reply"),
        ]
        final = _make_final_message("end_turn", [text_block])
        _setup_client_single_response(mock_anthropic_client, events, final)

        mcp = MagicMock()
        mcp.get_claude_tools = MagicMock(return_value=[])

        async for _ in run_agent_loop(
            client=mock_anthropic_client,
            mcp_manager=mcp,
            system_prompt="sys",
            user_text="Hello",
            conversation_state=conversation_state,
            model="claude-sonnet-4-5",
            max_tokens=512,
            temperature=1.0,
        ):
            pass

        # conversation_state.messages should contain user + assistant
        assert conversation_state.messages[0] == {
            "role": "user",
            "content": "Hello",
        }
        assert conversation_state.messages[1]["role"] == "assistant"

    @pytest.mark.asyncio
    async def test_temperature_not_passed_when_default(
        self, mock_anthropic_client, conversation_state
    ):
        """When temperature == 1.0, it should NOT appear in api_args."""
        text_block = _make_text_block("ok")
        events = [_content_block_start_event(text_block), _text_delta_event("ok")]
        final = _make_final_message("end_turn", [text_block])
        _setup_client_single_response(mock_anthropic_client, events, final)

        mcp = MagicMock()
        mcp.get_claude_tools = MagicMock(return_value=[])

        async for _ in run_agent_loop(
            client=mock_anthropic_client,
            mcp_manager=mcp,
            system_prompt="sys",
            user_text="Hi",
            conversation_state=conversation_state,
            model="claude-sonnet-4-5",
            max_tokens=512,
            temperature=1.0,
        ):
            pass

        call_kwargs = mock_anthropic_client.messages.stream.call_args[1]
        assert "temperature" not in call_kwargs

    @pytest.mark.asyncio
    async def test_temperature_passed_when_non_default(
        self, mock_anthropic_client, conversation_state
    ):
        text_block = _make_text_block("ok")
        events = [_content_block_start_event(text_block), _text_delta_event("ok")]
        final = _make_final_message("end_turn", [text_block])
        _setup_client_single_response(mock_anthropic_client, events, final)

        mcp = MagicMock()
        mcp.get_claude_tools = MagicMock(return_value=[])

        async for _ in run_agent_loop(
            client=mock_anthropic_client,
            mcp_manager=mcp,
            system_prompt="sys",
            user_text="Hi",
            conversation_state=conversation_state,
            model="claude-sonnet-4-5",
            max_tokens=512,
            temperature=0.5,
        ):
            pass

        call_kwargs = mock_anthropic_client.messages.stream.call_args[1]
        assert call_kwargs["temperature"] == 0.5

    @pytest.mark.asyncio
    async def test_tools_passed_when_available(
        self, mock_anthropic_client, conversation_state
    ):
        text_block = _make_text_block("ok")
        events = [_content_block_start_event(text_block), _text_delta_event("ok")]
        final = _make_final_message("end_turn", [text_block])
        _setup_client_single_response(mock_anthropic_client, events, final)

        tool_defs = [
            {
                "name": "ha__turn_on",
                "description": "Turn on a device",
                "input_schema": {"type": "object", "properties": {}},
            }
        ]
        mcp = MagicMock()
        mcp.get_claude_tools = MagicMock(return_value=tool_defs)

        async for _ in run_agent_loop(
            client=mock_anthropic_client,
            mcp_manager=mcp,
            system_prompt="sys",
            user_text="Hi",
            conversation_state=conversation_state,
            model="claude-sonnet-4-5",
            max_tokens=512,
            temperature=1.0,
        ):
            pass

        call_kwargs = mock_anthropic_client.messages.stream.call_args[1]
        assert call_kwargs["tools"] == tool_defs

    @pytest.mark.asyncio
    async def test_tools_not_passed_when_empty(
        self, mock_anthropic_client, conversation_state
    ):
        text_block = _make_text_block("ok")
        events = [_content_block_start_event(text_block), _text_delta_event("ok")]
        final = _make_final_message("end_turn", [text_block])
        _setup_client_single_response(mock_anthropic_client, events, final)

        mcp = MagicMock()
        mcp.get_claude_tools = MagicMock(return_value=[])

        async for _ in run_agent_loop(
            client=mock_anthropic_client,
            mcp_manager=mcp,
            system_prompt="sys",
            user_text="Hi",
            conversation_state=conversation_state,
            model="claude-sonnet-4-5",
            max_tokens=512,
            temperature=1.0,
        ):
            pass

        call_kwargs = mock_anthropic_client.messages.stream.call_args[1]
        assert "tools" not in call_kwargs


class TestRunAgentLoopToolUse:
    """Claude calls a tool, gets the result, then responds with text."""

    @pytest.mark.asyncio
    async def test_single_tool_call_then_text(
        self, mock_anthropic_client, conversation_state
    ):
        # --- Iteration 1: tool_use ---
        tool_block = _make_tool_use_block(
            "tu_1", "ha__get_state", {"entity_id": "light.kitchen"}
        )
        iter1_events = [
            _content_block_start_event(tool_block),
            _tool_use_delta_event(),
        ]
        iter1_final = _make_final_message("tool_use", [tool_block])

        # --- Iteration 2: text ---
        text_block = _make_text_block("The kitchen light is on.")
        iter2_events = [
            _content_block_start_event(text_block),
            _text_delta_event("The kitchen light is on."),
        ]
        iter2_final = _make_final_message("end_turn", [text_block])

        _setup_client_multi_response(
            mock_anthropic_client,
            [(iter1_events, iter1_final), (iter2_events, iter2_final)],
        )

        mcp = MagicMock()
        mcp.get_claude_tools = MagicMock(
            return_value=[
                {
                    "name": "ha__get_state",
                    "description": "Get entity state",
                    "input_schema": {"type": "object"},
                }
            ]
        )
        mcp.call_tool = AsyncMock(return_value='{"state": "on"}')

        collected: list[dict] = []
        async for delta in run_agent_loop(
            client=mock_anthropic_client,
            mcp_manager=mcp,
            system_prompt="sys",
            user_text="Is the kitchen light on?",
            conversation_state=conversation_state,
            model="claude-sonnet-4-5",
            max_tokens=1024,
            temperature=1.0,
        ):
            collected.append(delta)

        # tool_use iteration should NOT yield any content (no text block)
        # text iteration should yield role + content
        assert {"role": "assistant"} in collected
        assert {"content": "The kitchen light is on."} in collected

        # MCP call_tool was invoked with correct args
        mcp.call_tool.assert_awaited_once_with(
            "ha__get_state", {"entity_id": "light.kitchen"}
        )

    @pytest.mark.asyncio
    async def test_tool_result_appended_to_messages(
        self, mock_anthropic_client, conversation_state
    ):
        tool_block = _make_tool_use_block("tu_1", "ha__turn_on", {"entity_id": "light.kitchen"})
        iter1_events = [_content_block_start_event(tool_block)]
        iter1_final = _make_final_message("tool_use", [tool_block])

        text_block = _make_text_block("Done.")
        iter2_events = [
            _content_block_start_event(text_block),
            _text_delta_event("Done."),
        ]
        iter2_final = _make_final_message("end_turn", [text_block])

        _setup_client_multi_response(
            mock_anthropic_client,
            [(iter1_events, iter1_final), (iter2_events, iter2_final)],
        )

        mcp = MagicMock()
        mcp.get_claude_tools = MagicMock(return_value=[{"name": "ha__turn_on", "description": "", "input_schema": {}}])
        mcp.call_tool = AsyncMock(return_value="OK")

        async for _ in run_agent_loop(
            client=mock_anthropic_client,
            mcp_manager=mcp,
            system_prompt="sys",
            user_text="Turn on kitchen light",
            conversation_state=conversation_state,
            model="claude-sonnet-4-5",
            max_tokens=512,
            temperature=1.0,
        ):
            pass

        msgs = conversation_state.messages
        # msgs: [user, assistant(tool_use), user(tool_result), assistant(text)]
        assert msgs[0]["role"] == "user"
        assert msgs[0]["content"] == "Turn on kitchen light"

        assert msgs[1]["role"] == "assistant"

        # Tool result message
        assert msgs[2]["role"] == "user"
        assert msgs[2]["content"][0]["type"] == "tool_result"
        assert msgs[2]["content"][0]["tool_use_id"] == "tu_1"
        assert msgs[2]["content"][0]["content"] == "OK"

        assert msgs[3]["role"] == "assistant"

    @pytest.mark.asyncio
    async def test_multi_step_tool_use(
        self, mock_anthropic_client, conversation_state
    ):
        """Claude calls tool A, then tool B, then produces text."""
        # Iteration 1: tool A
        tool_a = _make_tool_use_block("tu_a", "ha__get_state", {"entity_id": "sensor.temp"})
        iter1_events = [_content_block_start_event(tool_a)]
        iter1_final = _make_final_message("tool_use", [tool_a])

        # Iteration 2: tool B
        tool_b = _make_tool_use_block("tu_b", "ha__turn_on", {"entity_id": "climate.ac"})
        iter2_events = [_content_block_start_event(tool_b)]
        iter2_final = _make_final_message("tool_use", [tool_b])

        # Iteration 3: text
        text_block = _make_text_block("Done. AC is on, temp was 78F.")
        iter3_events = [
            _content_block_start_event(text_block),
            _text_delta_event("Done. AC is on, temp was 78F."),
        ]
        iter3_final = _make_final_message("end_turn", [text_block])

        _setup_client_multi_response(
            mock_anthropic_client,
            [
                (iter1_events, iter1_final),
                (iter2_events, iter2_final),
                (iter3_events, iter3_final),
            ],
        )

        mcp = MagicMock()
        mcp.get_claude_tools = MagicMock(
            return_value=[
                {"name": "ha__get_state", "description": "", "input_schema": {}},
                {"name": "ha__turn_on", "description": "", "input_schema": {}},
            ]
        )
        mcp.call_tool = AsyncMock(side_effect=["78F", "OK"])

        collected = []
        async for delta in run_agent_loop(
            client=mock_anthropic_client,
            mcp_manager=mcp,
            system_prompt="sys",
            user_text="Cool the house",
            conversation_state=conversation_state,
            model="claude-sonnet-4-5",
            max_tokens=1024,
            temperature=1.0,
        ):
            collected.append(delta)

        # Claude API was called 3 times
        assert mock_anthropic_client.messages.stream.call_count == 3

        # MCP was called twice with correct tool names
        assert mcp.call_tool.await_count == 2
        calls = mcp.call_tool.await_args_list
        assert calls[0].args == ("ha__get_state", {"entity_id": "sensor.temp"})
        assert calls[1].args == ("ha__turn_on", {"entity_id": "climate.ac"})

        # messages: user, asst(tool_a), user(result_a), asst(tool_b), user(result_b), asst(text)
        assert len(conversation_state.messages) == 6

    @pytest.mark.asyncio
    async def test_multiple_tools_in_single_response(
        self, mock_anthropic_client, conversation_state
    ):
        """Claude returns two tool_use blocks in one response (parallel tool calls)."""
        tool_a = _make_tool_use_block("tu_a", "ha__get_state", {"entity_id": "light.a"})
        tool_b = _make_tool_use_block("tu_b", "ha__get_state", {"entity_id": "light.b"})
        iter1_events = [
            _content_block_start_event(tool_a),
            _content_block_start_event(tool_b),
        ]
        iter1_final = _make_final_message("tool_use", [tool_a, tool_b])

        text_block = _make_text_block("Both lights are on.")
        iter2_events = [
            _content_block_start_event(text_block),
            _text_delta_event("Both lights are on."),
        ]
        iter2_final = _make_final_message("end_turn", [text_block])

        _setup_client_multi_response(
            mock_anthropic_client,
            [(iter1_events, iter1_final), (iter2_events, iter2_final)],
        )

        mcp = MagicMock()
        mcp.get_claude_tools = MagicMock(
            return_value=[{"name": "ha__get_state", "description": "", "input_schema": {}}]
        )
        mcp.call_tool = AsyncMock(return_value='{"state": "on"}')

        async for _ in run_agent_loop(
            client=mock_anthropic_client,
            mcp_manager=mcp,
            system_prompt="sys",
            user_text="Are lights on?",
            conversation_state=conversation_state,
            model="claude-sonnet-4-5",
            max_tokens=512,
            temperature=1.0,
        ):
            pass

        # Two tool results should be in a single "user" message
        assert mcp.call_tool.await_count == 2
        tool_result_msg = conversation_state.messages[2]
        assert tool_result_msg["role"] == "user"
        assert len(tool_result_msg["content"]) == 2
        assert tool_result_msg["content"][0]["tool_use_id"] == "tu_a"
        assert tool_result_msg["content"][1]["tool_use_id"] == "tu_b"


class TestRunAgentLoopMaxIterations:
    """Agent should stop after MAX_TOOL_ITERATIONS even if Claude keeps calling tools."""

    @pytest.mark.asyncio
    async def test_max_iterations_reached(
        self, mock_anthropic_client, conversation_state
    ):
        # Every iteration returns a tool_use; never "end_turn"
        tool_block = _make_tool_use_block("tu_loop", "ha__noop", {})
        responses = []
        for _ in range(MAX_TOOL_ITERATIONS):
            events = [_content_block_start_event(tool_block)]
            final = _make_final_message("tool_use", [tool_block])
            responses.append((events, final))

        _setup_client_multi_response(mock_anthropic_client, responses)

        mcp = MagicMock()
        mcp.get_claude_tools = MagicMock(
            return_value=[{"name": "ha__noop", "description": "", "input_schema": {}}]
        )
        mcp.call_tool = AsyncMock(return_value="OK")

        collected = []
        async for delta in run_agent_loop(
            client=mock_anthropic_client,
            mcp_manager=mcp,
            system_prompt="sys",
            user_text="Loop forever",
            conversation_state=conversation_state,
            model="claude-sonnet-4-5",
            max_tokens=512,
            temperature=1.0,
        ):
            collected.append(delta)

        # Should have called the API exactly MAX_TOOL_ITERATIONS times
        assert mock_anthropic_client.messages.stream.call_count == MAX_TOOL_ITERATIONS
        # Should have called the tool each iteration
        assert mcp.call_tool.await_count == MAX_TOOL_ITERATIONS


class TestRunAgentLoopStreamingDeltas:
    """Verify the shape / ordering of yielded deltas."""

    @pytest.mark.asyncio
    async def test_no_role_marker_without_text(
        self, mock_anthropic_client, conversation_state
    ):
        """If the response has only a tool_use block (no text), no role marker is yielded."""
        tool_block = _make_tool_use_block("tu_1", "ha__act", {})
        iter1_events = [
            _content_block_start_event(tool_block),
            _tool_use_delta_event(),
        ]
        iter1_final = _make_final_message("tool_use", [tool_block])

        text_block = _make_text_block("done")
        iter2_events = [
            _content_block_start_event(text_block),
            _text_delta_event("done"),
        ]
        iter2_final = _make_final_message("end_turn", [text_block])

        _setup_client_multi_response(
            mock_anthropic_client,
            [(iter1_events, iter1_final), (iter2_events, iter2_final)],
        )

        mcp = MagicMock()
        mcp.get_claude_tools = MagicMock(
            return_value=[{"name": "ha__act", "description": "", "input_schema": {}}]
        )
        mcp.call_tool = AsyncMock(return_value="OK")

        collected = []
        async for delta in run_agent_loop(
            client=mock_anthropic_client,
            mcp_manager=mcp,
            system_prompt="sys",
            user_text="Do thing",
            conversation_state=conversation_state,
            model="claude-sonnet-4-5",
            max_tokens=512,
            temperature=1.0,
        ):
            collected.append(delta)

        # First iteration yields nothing (no text block)
        # Second iteration yields role + content
        assert collected == [{"role": "assistant"}, {"content": "done"}]

    @pytest.mark.asyncio
    async def test_only_text_deltas_yielded(
        self, mock_anthropic_client, conversation_state
    ):
        """Ensure tool_use delta events are never yielded."""
        text_block = _make_text_block("Hi")
        tool_block = _make_tool_use_block("tu_x", "ha__x", {})

        events = [
            _content_block_start_event(text_block),
            _text_delta_event("Hi"),
            # Interleaved tool delta – should NOT be yielded
            _content_block_start_event(tool_block),
            _tool_use_delta_event(),
        ]
        # But stop_reason is end_turn (text + tool in same response, unusual but test the filter)
        final = _make_final_message("end_turn", [text_block, tool_block])

        _setup_client_single_response(mock_anthropic_client, events, final)

        mcp = MagicMock()
        mcp.get_claude_tools = MagicMock(return_value=[{"name": "ha__x", "description": "", "input_schema": {}}])

        collected = []
        async for delta in run_agent_loop(
            client=mock_anthropic_client,
            mcp_manager=mcp,
            system_prompt="sys",
            user_text="Hi",
            conversation_state=conversation_state,
            model="claude-sonnet-4-5",
            max_tokens=512,
            temperature=1.0,
        ):
            collected.append(delta)

        # Only role marker + one text delta
        assert collected == [{"role": "assistant"}, {"content": "Hi"}]

    @pytest.mark.asyncio
    async def test_role_only_yielded_once_per_text_block(
        self, mock_anthropic_client, conversation_state
    ):
        """Even with multiple text delta events, role marker is yielded once."""
        text_block = _make_text_block("Hello world")
        events = [
            _content_block_start_event(text_block),
            _text_delta_event("Hello "),
            _text_delta_event("world"),
        ]
        final = _make_final_message("end_turn", [text_block])
        _setup_client_single_response(mock_anthropic_client, events, final)

        mcp = MagicMock()
        mcp.get_claude_tools = MagicMock(return_value=[])

        collected = []
        async for delta in run_agent_loop(
            client=mock_anthropic_client,
            mcp_manager=mcp,
            system_prompt="sys",
            user_text="Hi",
            conversation_state=conversation_state,
            model="claude-sonnet-4-5",
            max_tokens=512,
            temperature=1.0,
        ):
            collected.append(delta)

        role_markers = [d for d in collected if "role" in d]
        assert len(role_markers) == 1

    @pytest.mark.asyncio
    async def test_assistant_content_dumped_to_messages(
        self, mock_anthropic_client, conversation_state
    ):
        """Verify that model_dump() is called on content blocks for message history."""
        text_block = _make_text_block("Hi there")
        events = [
            _content_block_start_event(text_block),
            _text_delta_event("Hi there"),
        ]
        final = _make_final_message("end_turn", [text_block])
        _setup_client_single_response(mock_anthropic_client, events, final)

        mcp = MagicMock()
        mcp.get_claude_tools = MagicMock(return_value=[])

        async for _ in run_agent_loop(
            client=mock_anthropic_client,
            mcp_manager=mcp,
            system_prompt="sys",
            user_text="Hi",
            conversation_state=conversation_state,
            model="claude-sonnet-4-5",
            max_tokens=512,
            temperature=1.0,
        ):
            pass

        assistant_msg = conversation_state.messages[1]
        assert assistant_msg["content"] == [{"type": "text", "text": "Hi there"}]
