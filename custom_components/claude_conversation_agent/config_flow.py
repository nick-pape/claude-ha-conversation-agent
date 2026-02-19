"""Config flow for Claude Conversation Agent."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
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
    AUTH_MODE_API_KEY,
    AUTH_MODE_MAX,
    CONF_ADDON_URL,
    CONF_AUTH_MODE,
    CONF_CHAT_MODEL,
    CONF_MAX_TOKENS,
    CONF_PROMPT,
    CONF_RECOMMENDED,
    CONF_TEMPERATURE,
    DEFAULT_ADDON_URL,
    DEFAULT_CHAT_MODEL,
    DEFAULT_CONVERSATION_NAME,
    DEFAULT_CONVERSATION_OPTIONS,
    DEFAULT_MAX_TOKENS,
    DEFAULT_TEMPERATURE,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


class ClaudeConversationAgentConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Claude Conversation Agent."""

    VERSION = 1
    MINOR_VERSION = 2

    _addon_url: str = DEFAULT_ADDON_URL

    async def async_step_hassio(
        self, discovery_info: dict[str, Any]
    ) -> ConfigFlowResult:
        """Handle Supervisor add-on discovery."""
        await self.async_set_unique_id("claude_agent")
        self._abort_if_unique_id_configured()
        self._addon_url = f"http://{discovery_info.get('host', '2af94f27-claude-agent')}:{discovery_info.get('port', 3000)}"
        return await self.async_step_auth()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle manual setup — ask for add-on URL and MCP URL."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._addon_url = user_input.get(CONF_ADDON_URL, DEFAULT_ADDON_URL)

            # Verify add-on is reachable
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        f"{self._addon_url}/api/health",
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        if resp.status != 200:
                            errors["base"] = "cannot_connect"
                        else:
                            data = await resp.json()
                            if data.get("status") != "ok":
                                errors["base"] = "cannot_connect"
            except aiohttp.ClientError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error connecting to add-on")
                errors["base"] = "unknown"

            if not errors:
                return await self.async_step_auth()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_ADDON_URL, default=DEFAULT_ADDON_URL
                    ): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.URL)
                    ),
                }
            ),
            errors=errors or None,
        )

    async def async_step_auth(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose authentication mode."""
        if user_input is not None:
            auth_mode = user_input.get(CONF_AUTH_MODE, AUTH_MODE_API_KEY)
            entry_data: dict[str, Any] = {
                CONF_ADDON_URL: self._addon_url,
                CONF_AUTH_MODE: auth_mode,
            }

            if auth_mode == AUTH_MODE_API_KEY:
                api_key = user_input.get(CONF_API_KEY, "")
                if not api_key:
                    return self.async_show_form(
                        step_id="auth",
                        data_schema=self._auth_schema(),
                        errors={"base": "authentication_error"},
                    )
                entry_data[CONF_API_KEY] = api_key

            return self.async_create_entry(
                title="Claude",
                data=entry_data,
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
            step_id="auth",
            data_schema=self._auth_schema(),
        )

    def _auth_schema(self) -> vol.Schema:
        """Build the auth step schema."""
        return vol.Schema(
            {
                vol.Required(
                    CONF_AUTH_MODE, default=AUTH_MODE_API_KEY
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            SelectOptionDict(
                                label="API Key", value=AUTH_MODE_API_KEY
                            ),
                            SelectOptionDict(
                                label="Max Subscription", value=AUTH_MODE_MAX
                            ),
                        ],
                        mode="dropdown",
                    )
                ),
                vol.Optional(CONF_API_KEY): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.PASSWORD)
                ),
            }
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
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(),
                data_updates={CONF_API_KEY: user_input[CONF_API_KEY]},
            )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_API_KEY): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    ),
                }
            ),
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
                return self._create_or_update()
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
            return self._create_or_update()

        step_schema = vol.Schema(
            {
                vol.Optional(
                    CONF_CHAT_MODEL,
                    default=self.options.get(
                        CONF_CHAT_MODEL, DEFAULT_CHAT_MODEL
                    ),
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            SelectOptionDict(
                                label="Claude Sonnet 4.5",
                                value="claude-sonnet-4-5",
                            ),
                            SelectOptionDict(
                                label="Claude Opus 4.6",
                                value="claude-opus-4-6",
                            ),
                            SelectOptionDict(
                                label="Claude Haiku 4.5",
                                value="claude-haiku-4-5",
                            ),
                        ],
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
            last_step=True,
        )

    def _create_or_update(self) -> SubentryFlowResult:
        """Create new subentry or update existing one."""
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
