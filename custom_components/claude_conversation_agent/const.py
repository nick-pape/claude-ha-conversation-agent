"""Constants for the Claude Conversation Agent integration."""

from __future__ import annotations

import logging

DOMAIN = "claude_conversation_agent"
LOGGER = logging.getLogger(__package__)

DEFAULT_CONVERSATION_NAME = "Claude Conversation Agent"

# Config keys - parent entry
CONF_ADDON_URL = "addon_url"
CONF_AUTH_MODE = "auth_mode"

# Config keys - subentry
CONF_RECOMMENDED = "recommended"
CONF_PROMPT = "prompt"
CONF_CHAT_MODEL = "chat_model"
CONF_MAX_TOKENS = "max_tokens"
CONF_TEMPERATURE = "temperature"

# Auth modes
AUTH_MODE_API_KEY = "api_key"
AUTH_MODE_MAX = "max"

# Defaults
DEFAULT_ADDON_URL = "http://f5f163bb-claude-agent:3000"
DEFAULT_CHAT_MODEL = "claude-sonnet-4-5"
DEFAULT_MAX_TOKENS = 1024
DEFAULT_TEMPERATURE = 1.0

DEFAULT_PROMPT = (
    "You are a helpful voice assistant for a smart home. "
    "Keep responses concise and conversational. "
    "When performing actions, briefly confirm what you did."
)

DEFAULT_CONVERSATION_OPTIONS: dict[str, object] = {
    CONF_RECOMMENDED: True,
    CONF_PROMPT: DEFAULT_PROMPT,
}

# Limits
MAX_TOOL_ITERATIONS = 10
CONVERSATION_STATE_TTL = 300  # 5 minutes, matches HA session timeout
