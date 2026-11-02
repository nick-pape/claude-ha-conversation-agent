"""Conversation support for Claude Conversation Agent."""

from __future__ import annotations

import logging
from typing import Literal

from homeassistant.components import conversation
from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import CONF_API_KEY, MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import ClaudeAgentConfigEntry, tv_commands
from .agent import ConversationStateManager, run_agent_loop
from .const import (
    AUTH_MODE_API_KEY,
    CONF_AUTH_MODE,
    CONF_CHAT_MODEL,
    CONF_MAX_TOKENS,
    CONF_PROMPT,
    CONF_TEMPERATURE,
    CONVERSATION_STATE_TTL,
    DEFAULT_CHAT_MODEL,
    DEFAULT_MAX_TOKENS,
    DEFAULT_TEMPERATURE,
    DOMAIN,
)
from .entity import ClaudeBaseLLMEntity
from .satellite_display import mirror_to_satellite, satellite_client, satellite_node, satellite_nodes

_LOGGER = logging.getLogger(__name__)

# What an obvious TV command says back: nothing, the TV shows it.
TV_COMMAND_REPLY = ""


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ClaudeAgentConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up conversation entities."""
    for subentry in config_entry.subentries.values():
        if subentry.subentry_type != "conversation":
            continue
        async_add_entities(
            [ClaudeConversationEntity(config_entry, subentry)],
            config_subentry_id=subentry.subentry_id,
        )


class ClaudeConversationEntity(
    conversation.ConversationEntity,
    conversation.AbstractConversationAgent,
    ClaudeBaseLLMEntity,
):
    """Claude conversation agent entity.

    Communicates with the Claude Agent add-on over HTTP+SSE.
    The add-on handles MCP connections, tool discovery, and the agent loop.
    """

    _attr_supports_streaming = True

    def __init__(
        self, entry: ClaudeAgentConfigEntry, subentry: ConfigSubentry
    ) -> None:
        """Initialize the agent."""
        super().__init__(entry, subentry)
        self._attr_supported_features = (
            conversation.ConversationEntityFeature.CONTROL
        )
        self._state_manager = ConversationStateManager(
            ttl_seconds=CONVERSATION_STATE_TTL
        )

    @property
    def supported_languages(self) -> list[str] | Literal["*"]:
        """Return supported languages."""
        return MATCH_ALL

    async def async_will_remove_from_hass(self) -> None:
        """Clean up conversation states when entity is removed."""
        self._state_manager.clear()
        await super().async_will_remove_from_hass()

    async def _async_handle_message(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
    ) -> conversation.ConversationResult:
        """Process a conversation turn."""
        options = self.subentry.data

        # 0. Obvious TV commands go straight to the satellite, without the agent.
        if (command := tv_commands.match(user_input.text)) is not None and await self._async_run_tv_command(
            user_input, command
        ):
            chat_log.async_add_assistant_content_without_tools(
                conversation.AssistantContent(agent_id=self.entity_id, content=TV_COMMAND_REPLY)
            )
            return conversation.async_get_result_from_chat_log(user_input, chat_log)

        # 1. Set up system prompt (no HA native tools)
        try:
            await chat_log.async_provide_llm_data(
                user_input.as_llm_context(DOMAIN),
                user_llm_hass_api=None,
                user_llm_prompt=options.get(CONF_PROMPT),
                user_extra_system_prompt=user_input.extra_system_prompt,
            )
        except conversation.ConverseError as err:
            return err.as_conversation_result()

        # 2. Get or create conversation state
        state = self._state_manager.get_or_create(chat_log.conversation_id)

        # 3. Get system prompt from chat_log
        system_prompt = chat_log.content[0].content

        # 4. Get add-on URL and auth config from entry
        addon_url = self.entry.runtime_data.addon_url
        auth_mode = self.entry.data.get(CONF_AUTH_MODE, AUTH_MODE_API_KEY)
        api_key = self.entry.data.get(CONF_API_KEY)
        # 5. Run agent loop through chat_log's streaming interface
        node = self._tv_node(user_input.device_id) or satellite_node(self.hass, user_input.device_id)
        deltas = run_agent_loop(
            addon_url=addon_url,
            system_prompt=system_prompt,
            user_text=user_input.text,
            conversation_state=state,
            model=options.get(CONF_CHAT_MODEL, DEFAULT_CHAT_MODEL),
            max_tokens=options.get(CONF_MAX_TOKENS, DEFAULT_MAX_TOKENS),
            temperature=options.get(CONF_TEMPERATURE, DEFAULT_TEMPERATURE),
            auth_mode=auth_mode,
            api_key=api_key,
            satellite={"node": node, "tv": self._has_tv(node)} if node else None,
        )
        # An alfred-satellite shows the whole reply and its tool calls on the TV.
        client = satellite_client(self.hass, user_input.device_id)
        if client is not None:
            deltas = mirror_to_satellite(deltas, client)
        try:
            async for _ in chat_log.async_add_delta_content_stream(
                self.entity_id, deltas
            ):
                pass
        except HomeAssistantError:
            raise
        except Exception as err:
            _LOGGER.exception("Error in agent loop")
            raise HomeAssistantError(
                f"Error communicating with Claude Agent add-on: {err}"
            ) from err

        # 6. Return result from chat_log
        return conversation.async_get_result_from_chat_log(user_input, chat_log)

    def _tv_node(self, device_id: str | None) -> str | None:
        """The satellite whose TV a request is about.

        The satellite that heard it; for a typed request (no device), the only
        satellite with TV actions, since that's the only TV it can mean.
        """
        if device_id is not None:
            node = satellite_node(self.hass, device_id)
            return node if self._has_tv(node) else None
        with_tv = [name for _, name in satellite_nodes(self.hass) if self._has_tv(name)]
        return with_tv[0] if len(with_tv) == 1 else None

    def _has_tv(self, node: str | None) -> bool:
        """Whether satellite node offers TV actions (alfred-satellite --tv)."""
        return node is not None and self.hass.services.has_service("esphome", _tv_service(node, "tv_keys"))

    async def _async_run_tv_command(
        self, user_input: conversation.ConversationInput, command: tv_commands.TvCommand
    ) -> bool:
        """Run a TV action on the satellite the request is about; False if it has none."""
        node = self._tv_node(user_input.device_id)
        if node is None:
            return False
        # A satellite offers only the actions it can run (no TV id, no tv_turn_on).
        service = _tv_service(node, command.action)
        if not self.hass.services.has_service("esphome", service):
            return False
        _LOGGER.debug("TV command %r -> esphome.%s %s", user_input.text, service, command.data)
        await self.hass.services.async_call("esphome", service, command.data, blocking=False)
        return True


def _tv_service(node: str, action: str) -> str:
    """The esphome service Home Assistant registers for a satellite's action."""
    return f"{node.replace('-', '_')}_{action}"
