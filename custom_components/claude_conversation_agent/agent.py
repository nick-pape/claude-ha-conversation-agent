"""HTTP+SSE client for the Claude Agent add-on."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from .const import MAX_TOOL_ITERATIONS

_LOGGER = logging.getLogger(__name__)


@dataclass
class ConversationState:
    """Stores session ID for conversation continuity."""

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
    addon_url: str,
    system_prompt: str,
    user_text: str,
    conversation_state: ConversationState,
    model: str,
    max_tokens: int,
    temperature: float,
    auth_mode: str = "api_key",
    api_key: str | None = None,
) -> AsyncGenerator[dict[str, Any]]:
    """Call the Claude Agent add-on and yield text deltas.

    Yields dicts with keys:
      - {"role": "assistant"} -- start of new assistant message
      - {"content": "text chunk"} -- text delta for TTS

    Communicates with the add-on via HTTP POST + SSE streaming.
    """
    payload: dict[str, Any] = {
        "system_prompt": system_prompt,
        "user_text": user_text,
        "model": model,
        "max_turns": MAX_TOOL_ITERATIONS,
        "auth_mode": auth_mode,
    }

    if api_key:
        payload["api_key"] = api_key

    if conversation_state.session_id:
        payload["session_id"] = conversation_state.session_id
        _LOGGER.debug(
            "Resuming session: %s", conversation_state.session_id
        )

    _LOGGER.debug(
        "Sending chat request to add-on: model=%s, auth_mode=%s",
        model,
        auth_mode,
    )

    timeout = aiohttp.ClientTimeout(total=120, sock_read=120)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            f"{addon_url}/api/chat",
            json=payload,
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(
                    f"Add-on returned HTTP {resp.status}: {body}"
                )

            # Read SSE stream
            async for line in resp.content:
                line_str = line.decode("utf-8").strip()

                if not line_str.startswith("data: "):
                    continue

                try:
                    data = json.loads(line_str[6:])
                except json.JSONDecodeError:
                    _LOGGER.warning("Invalid SSE data: %s", line_str)
                    continue

                event_type = data.get("type")

                if event_type == "init":
                    session_id = data.get("session_id")
                    if session_id:
                        conversation_state.session_id = session_id
                    _LOGGER.debug(
                        "Agent initialized: session=%s, mcp=%s",
                        session_id,
                        data.get("mcp_servers", []),
                    )

                elif event_type == "role":
                    yield {"role": data.get("role", "assistant")}

                elif event_type == "delta":
                    content = data.get("content", "")
                    if content:
                        yield {"content": content}

                elif event_type == "tool_start":
                    _LOGGER.debug(
                        "Tool call: %s", data.get("tool", "unknown")
                    )

                elif event_type == "tool_done":
                    _LOGGER.debug("Tool complete: %s", data.get("tool_id"))

                elif event_type == "result":
                    session_id = data.get("session_id")
                    if session_id:
                        conversation_state.session_id = session_id
                    is_error = data.get("is_error", False)
                    if is_error:
                        error_msg = data.get("error", "Unknown error")
                        _LOGGER.error(
                            "Agent loop error: %s", error_msg
                        )
                    _LOGGER.debug(
                        "Agent loop complete: session=%s",
                        session_id,
                    )
