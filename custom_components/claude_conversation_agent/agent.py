"""Claude agent loop - async generator that yields text deltas."""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any

import anthropic

from .const import MAX_TOOL_ITERATIONS
from .mcp_manager import MCPManager

_LOGGER = logging.getLogger(__name__)


@dataclass
class ConversationState:
    """Stores Claude's conversation history for a session."""

    messages: list[dict[str, Any]] = field(default_factory=list)
    created: float = field(default_factory=time.monotonic)
    last_accessed: float = field(default_factory=time.monotonic)


class ConversationStateManager:
    """TTL cache for conversation states."""

    def __init__(self, ttl_seconds: float = 300.0) -> None:
        """Initialize with a TTL in seconds."""
        self._states: dict[str, ConversationState] = {}
        self._ttl = ttl_seconds

    def get_or_create(self, conversation_id: str) -> ConversationState:
        """Get existing state or create a new one."""
        self._cleanup_expired()
        now = time.monotonic()

        if conversation_id in self._states:
            state = self._states[conversation_id]
            state.last_accessed = now
            return state

        state = ConversationState(created=now, last_accessed=now)
        self._states[conversation_id] = state
        return state

    def remove(self, conversation_id: str) -> None:
        """Remove a specific conversation state."""
        self._states.pop(conversation_id, None)

    def clear(self) -> None:
        """Remove all conversation states."""
        self._states.clear()

    def _cleanup_expired(self) -> None:
        """Remove states that haven't been accessed within TTL."""
        now = time.monotonic()
        expired = [
            cid
            for cid, state in self._states.items()
            if now - state.last_accessed > self._ttl
        ]
        for cid in expired:
            del self._states[cid]


async def run_agent_loop(
    client: anthropic.AsyncAnthropic,
    mcp_manager: MCPManager,
    system_prompt: str,
    user_text: str,
    conversation_state: ConversationState,
    model: str,
    max_tokens: int,
    temperature: float,
) -> AsyncGenerator[dict[str, Any]]:
    """Run the Claude agent loop, yielding text deltas.

    Yields AssistantContentDeltaDict dicts with keys:
      - {"role": "assistant"} — start of new assistant message
      - {"content": "text chunk"} — text delta for TTS

    Never yields: tool_calls, thinking_content, native.

    Side effects:
      - Modifies conversation_state.messages in place
      - Calls mcp_manager.call_tool() for tool execution

    Terminates when:
      - Claude returns stop_reason == "end_turn"
      - MAX_TOOL_ITERATIONS reached
      - An exception occurs (propagated to caller)
    """
    tools = mcp_manager.get_claude_tools()

    messages = conversation_state.messages
    messages.append({"role": "user", "content": user_text})

    for _iteration in range(MAX_TOOL_ITERATIONS):
        api_args: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system_prompt,
            "messages": messages,
        }
        if temperature != 1.0:
            api_args["temperature"] = temperature
        if tools:
            api_args["tools"] = tools

        # Call Claude with streaming
        async with client.messages.stream(**api_args) as stream:
            text_started = False

            async for event in stream:
                if event.type == "content_block_start":
                    if hasattr(event, "content_block") and event.content_block.type == "text":
                        if not text_started:
                            yield {"role": "assistant"}
                            text_started = True

                elif event.type == "content_block_delta":
                    if hasattr(event, "delta") and event.delta.type == "text_delta":
                        yield {"content": event.delta.text}

                # tool_use events are consumed but NOT yielded to chat_log

            final_message = await stream.get_final_message()

        # Add complete assistant response to our conversation history
        messages.append(
            {
                "role": "assistant",
                "content": [
                    block.model_dump() for block in final_message.content
                ],
            }
        )

        _LOGGER.debug(
            "Claude iteration %d: stop_reason=%s, content_blocks=%d",
            _iteration + 1,
            final_message.stop_reason,
            len(final_message.content),
        )

        # If no tool calls, we're done
        if final_message.stop_reason == "end_turn":
            break

        # Execute tool calls via MCP
        if final_message.stop_reason == "tool_use":
            tool_results: list[dict[str, Any]] = []
            for block in final_message.content:
                if block.type == "tool_use":
                    _LOGGER.debug(
                        "Executing tool: %s with args: %s",
                        block.name,
                        block.input,
                    )
                    result = await mcp_manager.call_tool(
                        block.name, block.input
                    )
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
                        }
                    )

            messages.append({"role": "user", "content": tool_results})

        # Next iteration: reset for new Claude response
        text_started = False
    else:
        _LOGGER.warning(
            "Agent loop reached maximum iterations (%d)", MAX_TOOL_ITERATIONS
        )
