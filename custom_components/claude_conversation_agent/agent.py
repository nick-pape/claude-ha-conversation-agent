"""Claude agent loop using the Claude Agent SDK."""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any

from .const import MAX_TOOL_ITERATIONS

_LOGGER = logging.getLogger(__name__)


@dataclass
class ConversationState:
    """Stores session ID for Agent SDK conversation continuity."""

    session_id: str | None = None
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
    api_key: str,
    mcp_server_url: str | None,
    mcp_server_token: str | None,
    system_prompt: str,
    user_text: str,
    conversation_state: ConversationState,
    model: str,
    max_tokens: int,
    temperature: float,
) -> AsyncGenerator[dict[str, Any]]:
    """Run the Claude agent loop via the Agent SDK, yielding text deltas.

    Yields AssistantContentDeltaDict dicts with keys:
      - {"role": "assistant"} -- start of new assistant message
      - {"content": "text chunk"} -- text delta for TTS

    The Agent SDK handles tool discovery, execution, and the multi-turn
    agent loop internally. We only extract streaming text deltas.
    """
    from claude_agent_sdk import (  # noqa: C0415
        ClaudeAgentOptions,
        ResultMessage,
        StreamEvent,
        SystemMessage,
    )
    from claude_agent_sdk import query as sdk_query

    # Build MCP server config for the Agent SDK
    mcp_servers: dict[str, Any] = {}
    allowed_tools: list[str] = []
    if mcp_server_url:
        server_config: dict[str, Any] = {
            "type": "sse",
            "url": mcp_server_url,
        }
        if mcp_server_token:
            server_config["headers"] = {
                "Authorization": f"Bearer {mcp_server_token}"
            }
        mcp_servers["ha"] = server_config
        allowed_tools = ["mcp__ha__*"]

    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        model=model,
        max_turns=MAX_TOOL_ITERATIONS,
        mcp_servers=mcp_servers,
        allowed_tools=allowed_tools,
        permission_mode="bypassPermissions",
        include_partial_messages=True,
        env={"ANTHROPIC_API_KEY": api_key},
    )

    # Resume previous conversation if we have a session ID
    if conversation_state.session_id:
        options.resume = conversation_state.session_id
        _LOGGER.debug(
            "Resuming Agent SDK session: %s", conversation_state.session_id
        )

    _LOGGER.debug(
        "Starting agent loop: model=%s, mcp_servers=%s, allowed_tools=%s",
        model,
        list(mcp_servers.keys()),
        allowed_tools,
    )

    text_started = False

    async for message in sdk_query(prompt=user_text, options=options):
        # Capture session metadata from init message
        if isinstance(message, SystemMessage) and message.subtype == "init":
            mcp_status = message.data.get("mcp_servers", [])
            _LOGGER.info(
                "Agent SDK initialized. MCP servers: %s", mcp_status
            )
            # Check for MCP connection failures
            for server in mcp_status:
                if isinstance(server, dict) and server.get("status") != "connected":
                    _LOGGER.warning(
                        "MCP server '%s' failed to connect: %s",
                        server.get("name", "unknown"),
                        server.get("status", "unknown"),
                    )

        # Extract streaming text deltas from raw API events
        if isinstance(message, StreamEvent):
            event = message.event
            event_type = event.get("type")

            if event_type == "content_block_start":
                content_block = event.get("content_block", {})
                if content_block.get("type") == "text":
                    if not text_started:
                        yield {"role": "assistant"}
                        text_started = True

            elif event_type == "content_block_delta":
                delta = event.get("delta", {})
                if delta.get("type") == "text_delta":
                    if not text_started:
                        yield {"role": "assistant"}
                        text_started = True
                    yield {"content": delta["text"]}

        # Capture session ID from result for conversation continuity
        if isinstance(message, ResultMessage):
            conversation_state.session_id = message.session_id
            _LOGGER.debug(
                "Agent loop complete: session=%s, turns=%s, cost=$%s",
                message.session_id,
                message.num_turns,
                message.total_cost_usd,
            )
            if message.is_error:
                _LOGGER.error(
                    "Agent SDK reported error: %s", message.result
                )
