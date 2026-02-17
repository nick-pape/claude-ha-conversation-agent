"""Config flow for Claude Conversation Agent."""

from __future__ import annotations

import logging
from typing import Any

import anthropic
import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.const import CONF_API_KEY, CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers.httpx_client import get_async_client
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    TemplateSelector,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_CHAT_MODEL,
    CONF_MAX_TOKENS,
    CONF_MCP_SERVER_TOKEN,
    CONF_MCP_SERVER_URL,
    CONF_PROMPT,
    CONF_RECOMMENDED,
    CONF_TEMPERATURE,
    DEFAULT_CHAT_MODEL,
    DEFAULT_CONVERSATION_NAME,
    DEFAULT_CONVERSATION_OPTIONS,
    DEFAULT_MAX_TOKENS,
    DEFAULT_TEMPERATURE,
    DOMAIN,
    RECOMMENDED_MODELS,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_API_KEY): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
    }
)


async def _validate_api_key(
    hass: Any, api_key: str
) -> None:
    """Validate the API key by listing models."""
    client = anthropic.AsyncAnthropic(
        api_key=api_key,
        http_client=get_async_client(hass),
    )
    await client.models.list(timeout=10.0)


class ClaudeConversationAgentConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Claude Conversation Agent."""

    VERSION = 1
    MINOR_VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial API key step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._async_abort_entries_match(user_input)
            try:
                await _validate_api_key(self.hass, user_input[CONF_API_KEY])
            except anthropic.APITimeoutError:
                errors["base"] = "timeout_connect"
            except anthropic.APIConnectionError:
                errors["base"] = "cannot_connect"
            except anthropic.APIStatusError as err:
                if (
                    isinstance(err.body, dict)
                    and isinstance(err.body.get("error"), dict)
                    and err.body["error"].get("type") == "authentication_error"
                ):
                    errors["base"] = "authentication_error"
                else:
                    errors["base"] = "unknown"
            except Exception:
                _LOGGER.exception("Unexpected exception during setup")
                errors["base"] = "unknown"
            else:
                if self.source == "reauth":
                    return self.async_update_reload_and_abort(
                        self._get_reauth_entry(), data_updates=user_input
                    )
                return self.async_create_entry(
                    title="Claude",
                    data=user_input,
                    subentries=[
                        {
                            "subentry_type": "conversation",
                            "data": DEFAULT_CONVERSATION_OPTIONS,
                            "title": DEFAULT_CONVERSATION_NAME,
                            "unique_id": None,
                        },
                    ],
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors or None,
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Handle reauth."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm reauth dialog."""
        if user_input is not None:
            return await self.async_step_user(user_input)

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_USER_DATA_SCHEMA,
        )

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Return supported subentry types."""
        return {
            "conversation": ConversationSubentryFlowHandler,
        }


class ConversationSubentryFlowHandler(ConfigSubentryFlow):
    """Handle conversation subentry flow."""

    options: dict[str, Any]

    @property
    def _is_new(self) -> bool:
        return self.source == "user"

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Add a new conversation agent."""
        self.options = dict(DEFAULT_CONVERSATION_OPTIONS)
        return await self.async_step_init()

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Reconfigure an existing conversation agent."""
        self.options = dict(self._get_reconfigure_subentry().data)
        return await self.async_step_init()

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Basic settings: name, prompt, recommended toggle."""
        if user_input is not None:
            self.options.update(user_input)
            if user_input.get(CONF_RECOMMENDED, True):
                return await self.async_step_mcp()
            return await self.async_step_advanced()

        step_schema: dict[Any, Any] = {}
        if self._is_new:
            step_schema[
                vol.Required(
                    CONF_NAME, default=DEFAULT_CONVERSATION_NAME
                )
            ] = TextSelector()

        step_schema[vol.Optional(CONF_PROMPT)] = TemplateSelector()
        step_schema[
            vol.Required(
                CONF_RECOMMENDED,
                default=self.options.get(CONF_RECOMMENDED, True),
            )
        ] = bool

        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(step_schema), self.options
            ),
        )

    async def async_step_advanced(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Advanced settings: model, max tokens, temperature."""
        if user_input is not None:
            self.options.update(user_input)
            return await self.async_step_mcp()

        model_options = [
            SelectOptionDict(label=m, value=m) for m in RECOMMENDED_MODELS
        ]

        step_schema = vol.Schema(
            {
                vol.Optional(
                    CONF_CHAT_MODEL,
                    default=self.options.get(
                        CONF_CHAT_MODEL, DEFAULT_CHAT_MODEL
                    ),
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=model_options,
                        custom_value=True,
                    )
                ),
                vol.Optional(
                    CONF_MAX_TOKENS,
                    default=self.options.get(
                        CONF_MAX_TOKENS, DEFAULT_MAX_TOKENS
                    ),
                ): NumberSelector(
                    NumberSelectorConfig(min=1, max=32768, step=1, mode="box")
                ),
                vol.Optional(
                    CONF_TEMPERATURE,
                    default=self.options.get(
                        CONF_TEMPERATURE, DEFAULT_TEMPERATURE
                    ),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=0.0, max=2.0, step=0.05, mode="slider"
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="advanced",
            data_schema=self.add_suggested_values_to_schema(
                step_schema, self.options
            ),
        )

    async def async_step_mcp(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """MCP server settings."""
        if user_input is not None:
            self.options.update(user_input)
            if self._is_new:
                name = self.options.pop(CONF_NAME, DEFAULT_CONVERSATION_NAME)
                return self.async_create_entry(
                    title=name,
                    data=self.options,
                )
            return self.async_update_and_abort(
                self._get_entry(),
                self._get_reconfigure_subentry(),
                data=self.options,
            )

        step_schema = vol.Schema(
            {
                vol.Optional(
                    CONF_MCP_SERVER_URL,
                    default=self.options.get(CONF_MCP_SERVER_URL, ""),
                ): TextSelector(TextSelectorConfig(type=TextSelectorType.URL)),
                vol.Optional(
                    CONF_MCP_SERVER_TOKEN,
                    default=self.options.get(CONF_MCP_SERVER_TOKEN, ""),
                ): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.PASSWORD)
                ),
            }
        )

        return self.async_show_form(
            step_id="mcp",
            data_schema=self.add_suggested_values_to_schema(
                step_schema, self.options
            ),
            last_step=True,
        )
