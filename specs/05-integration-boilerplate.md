# 05 - Integration Boilerplate: Home Assistant Custom Conversation Agent

This document captures all patterns, file structures, and code needed to build a minimal
Home Assistant custom conversation agent integration distributed via HACS. It is based on
analysis of the official **Anthropic** and **OpenAI** core integrations (as of the
`dev` branch, February 2026) and HACS publishing requirements.

---

## Table of Contents

1. [Repository & File Structure](#1-repository--file-structure)
2. [HACS Requirements](#2-hacs-requirements)
3. [manifest.json](#3-manifestjson)
4. [const.py](#4-constpy)
5. [__init__.py](#5-initpy)
6. [config_flow.py](#6-config_flowpy)
7. [conversation.py](#7-conversationpy)
8. [entity.py (Base LLM Entity)](#8-entitypy-base-llm-entity)
9. [strings.json & translations/en.json](#9-stringsjson--translationsenjson)
10. [Config Flow Design for Our Integration](#10-config-flow-design-for-our-integration)
11. [Subentry Architecture](#11-subentry-architecture)
12. [Key Patterns & Conventions](#12-key-patterns--conventions)

---

## 1. Repository & File Structure

### Required repository layout for HACS distribution

```
claude-ha-conversation-agent/          # Repository root
├── hacs.json                          # HACS metadata (required at root)
├── README.md                          # Repository description (required by HACS)
├── LICENSE                            # License file
├── custom_components/
│   └── claude_conversation_agent/     # Integration directory
│       ├── __init__.py                # Integration setup & teardown
│       ├── manifest.json              # HA integration manifest (required)
│       ├── config_flow.py             # Config flow & subentry flows
│       ├── conversation.py            # ConversationEntity platform
│       ├── entity.py                  # Base LLM entity class
│       ├── const.py                   # Domain constants
│       ├── strings.json               # UI strings (primary)
│       └── translations/
│           └── en.json                # English translations (copy of strings.json)
```

### Rules

- **One integration per repository.** Only one subdirectory under `custom_components/`.
- **All files** required for the integration to run must be inside
  `custom_components/claude_conversation_agent/`.
- The `hacs.json` file must be at the **repository root**.

---

## 2. HACS Requirements

### hacs.json (repository root)

```json
{
  "name": "Claude Conversation Agent",
  "render_readme": true,
  "homeassistant": "2025.7.0"
}
```

**Supported keys in hacs.json:**

| Key                    | Required | Description                                                        |
|------------------------|----------|--------------------------------------------------------------------|
| `name`                 | Yes      | Display name in HACS UI                                            |
| `render_readme`        | No       | If `true`, render README.md instead of info.md in HACS UI          |
| `homeassistant`        | No       | Minimum HA version required (e.g. `"2025.7.0"` or `"2025.7.0b0"`) |
| `content_in_root`      | No       | If files are in root rather than `custom_components/`              |
| `country`              | No       | Country code(s) to filter availability                             |
| `persistent_directory` | No       | Directory to persist between upgrades                              |

### Additional HACS requirements

1. **Public GitHub repository** with a description and topics.
2. **Releases are preferred** (but not required). HACS shows the 5 latest releases
   plus the default branch when downloading/upgrading.
3. **home-assistant/brands** submission is required for proper UI icons and branding.
4. The `manifest.json` inside the integration must include: `domain`, `documentation`,
   `issue_tracker`, `codeowners`, `name`, and `version`.

### Python package dependencies

Dependencies are specified in `manifest.json` under the `requirements` key. Format
uses pip-compatible version strings:

```json
{
  "requirements": ["anthropic==0.78.0", "mcp>=1.0.0"]
}
```

Home Assistant will install these packages automatically. Custom integrations should
not list packages already in HA core's `requirements.txt`.

**References:**
- [HACS Integration Publishing](https://www.hacs.xyz/docs/publish/integration/)
- [HACS General Requirements](https://hacs.xyz/docs/publish/start/)

---

## 3. manifest.json

### Anthropic core integration pattern

```json
{
  "domain": "anthropic",
  "name": "Anthropic",
  "after_dependencies": ["assist_pipeline", "intent"],
  "codeowners": ["@Shulyaka"],
  "config_flow": true,
  "dependencies": ["conversation"],
  "documentation": "https://www.home-assistant.io/integrations/anthropic",
  "integration_type": "service",
  "iot_class": "cloud_polling",
  "requirements": ["anthropic==0.78.0"]
}
```

### OpenAI core integration pattern

```json
{
  "domain": "openai_conversation",
  "name": "OpenAI",
  "after_dependencies": ["assist_pipeline", "intent"],
  "codeowners": [],
  "config_flow": true,
  "dependencies": ["conversation"],
  "documentation": "https://www.home-assistant.io/integrations/openai_conversation",
  "integration_type": "service",
  "iot_class": "cloud_polling",
  "quality_scale": "bronze",
  "requirements": ["openai==2.21.0"]
}
```

### Our integration manifest.json

```json
{
  "domain": "claude_conversation_agent",
  "name": "Claude Conversation Agent with MCP",
  "after_dependencies": ["assist_pipeline", "intent"],
  "codeowners": ["@your-github-username"],
  "config_flow": true,
  "dependencies": ["conversation"],
  "documentation": "https://github.com/your-org/claude-ha-conversation-agent",
  "integration_type": "service",
  "iot_class": "cloud_polling",
  "issue_tracker": "https://github.com/your-org/claude-ha-conversation-agent/issues",
  "requirements": ["anthropic==0.78.0"],
  "version": "0.1.0"
}
```

**Key differences for custom (HACS) integrations vs core:**
- `version` field is **required** for custom integrations (omitted in core).
- `issue_tracker` is required by HACS.
- `quality_scale` is only for core integrations.

### All manifest.json fields reference

| Field                | Required | Description                                                  |
|----------------------|----------|--------------------------------------------------------------|
| `domain`             | Yes      | Unique identifier (lowercase, underscores only)              |
| `name`               | Yes      | Human-readable name                                          |
| `codeowners`         | Yes      | GitHub usernames prefixed with `@`                           |
| `dependencies`       | Yes      | HA integrations this depends on                              |
| `documentation`      | Yes      | URL to docs                                                  |
| `integration_type`   | Yes      | `"service"` for cloud API integrations                       |
| `iot_class`          | Yes      | `"cloud_polling"` for cloud API services                     |
| `requirements`       | Yes      | Python pip packages                                          |
| `version`            | Custom   | Required for custom components (semver string)               |
| `config_flow`        | No       | `true` if UI-configurable                                    |
| `after_dependencies` | No       | Non-critical deps to load first                              |
| `issue_tracker`      | No       | URL for bug reports (required by HACS)                       |
| `loggers`            | No       | Logger names used by requirements                            |

---

## 4. const.py

### Anthropic pattern

```python
"""Constants for the Anthropic integration."""

import logging

DOMAIN = "anthropic"
LOGGER = logging.getLogger(__package__)

DEFAULT_CONVERSATION_NAME = "Claude conversation"
DEFAULT_AI_TASK_NAME = "Claude AI Task"

CONF_RECOMMENDED = "recommended"
CONF_PROMPT = "prompt"
CONF_CHAT_MODEL = "chat_model"
CONF_MAX_TOKENS = "max_tokens"
CONF_TEMPERATURE = "temperature"
CONF_THINKING_BUDGET = "thinking_budget"
CONF_THINKING_EFFORT = "thinking_effort"
CONF_WEB_SEARCH = "web_search"
CONF_WEB_SEARCH_USER_LOCATION = "user_location"
CONF_WEB_SEARCH_MAX_USES = "web_search_max_uses"
CONF_WEB_SEARCH_CITY = "city"
CONF_WEB_SEARCH_REGION = "region"
CONF_WEB_SEARCH_COUNTRY = "country"
CONF_WEB_SEARCH_TIMEZONE = "timezone"

DATA_REPAIR_DEFER_RELOAD = "repair_defer_reload"

DEFAULT = {
    CONF_CHAT_MODEL: "claude-haiku-4-5",
    CONF_MAX_TOKENS: 3000,
    CONF_TEMPERATURE: 1.0,
    CONF_THINKING_BUDGET: 0,
    CONF_THINKING_EFFORT: "low",
    CONF_WEB_SEARCH: False,
    CONF_WEB_SEARCH_USER_LOCATION: False,
    CONF_WEB_SEARCH_MAX_USES: 5,
}

MIN_THINKING_BUDGET = 1024
```

### Our integration const.py (planned)

```python
"""Constants for the Claude Conversation Agent integration."""

import logging

DOMAIN = "claude_conversation_agent"
LOGGER = logging.getLogger(__package__)

DEFAULT_CONVERSATION_NAME = "Claude Conversation Agent"

# Config keys
CONF_RECOMMENDED = "recommended"
CONF_PROMPT = "prompt"
CONF_CHAT_MODEL = "chat_model"
CONF_MAX_TOKENS = "max_tokens"
CONF_TEMPERATURE = "temperature"
CONF_THINKING_EFFORT = "thinking_effort"

# MCP-specific config keys
CONF_MCP_SERVERS = "mcp_servers"
CONF_MCP_SERVER_URL = "url"
CONF_MCP_SERVER_NAME = "name"
CONF_MCP_SERVER_TOKEN = "token"
CONF_MCP_SERVER_TRANSPORT = "transport"

# Defaults
DEFAULT = {
    CONF_CHAT_MODEL: "claude-sonnet-4-5",
    CONF_MAX_TOKENS: 4096,
    CONF_TEMPERATURE: 1.0,
    CONF_THINKING_EFFORT: "low",
}
```

---

## 5. __init__.py

### Anthropic core pattern (simplified for reference)

```python
"""The Anthropic integration."""

from __future__ import annotations

import anthropic

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.httpx_client import get_async_client
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN, LOGGER

PLATFORMS = (Platform.CONVERSATION,)
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type AnthropicConfigEntry = ConfigEntry[anthropic.AsyncClient]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the integration."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: AnthropicConfigEntry) -> bool:
    """Set up from a config entry."""
    client = anthropic.AsyncAnthropic(
        api_key=entry.data[CONF_API_KEY],
        http_client=get_async_client(hass),
    )
    try:
        await client.models.list(timeout=10.0)
    except anthropic.AuthenticationError as err:
        raise ConfigEntryAuthFailed(err) from err
    except anthropic.AnthropicError as err:
        raise ConfigEntryNotReady(err) from err

    entry.runtime_data = client

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: AnthropicConfigEntry
) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
```

### Key patterns

1. **`ConfigEntry` type alias with runtime data**: `type AnthropicConfigEntry = ConfigEntry[anthropic.AsyncClient]`
   - This uses Python 3.12 type alias syntax to attach typed runtime data to config entries.
2. **API key validation** in `async_setup_entry` via `client.models.list()`.
3. **Exception mapping**: `AuthenticationError` -> `ConfigEntryAuthFailed`, other errors -> `ConfigEntryNotReady`.
4. **Platform forwarding**: `async_forward_entry_setups(entry, PLATFORMS)`.
5. **`CONFIG_SCHEMA`**: Using `cv.config_entry_only_config_schema(DOMAIN)` to indicate this
   integration is only configurable via config entries (no YAML).

### Our integration __init__.py (planned structure)

```python
"""The Claude Conversation Agent integration."""

from __future__ import annotations

import anthropic

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.httpx_client import get_async_client
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN, LOGGER

PLATFORMS = (Platform.CONVERSATION,)
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type ClaudeConfigEntry = ConfigEntry[anthropic.AsyncClient]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Claude Conversation Agent integration."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ClaudeConfigEntry) -> bool:
    """Set up Claude Conversation Agent from a config entry."""
    client = anthropic.AsyncAnthropic(
        api_key=entry.data[CONF_API_KEY],
        http_client=get_async_client(hass),
    )
    try:
        await client.models.list(timeout=10.0)
    except anthropic.AuthenticationError as err:
        raise ConfigEntryAuthFailed(err) from err
    except anthropic.AnthropicError as err:
        raise ConfigEntryNotReady(err) from err

    entry.runtime_data = client

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(async_update_options))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ClaudeConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_update_options(
    hass: HomeAssistant, entry: ClaudeConfigEntry
) -> None:
    """Update options -- triggers reload."""
    await hass.config_entries.async_reload(entry.entry_id)
```

---

## 6. config_flow.py

### Anthropic core pattern (key parts)

The config flow has two levels:

#### Level 1: Main ConfigFlow (API key)

```python
class AnthropicConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Anthropic."""

    VERSION = 2
    MINOR_VERSION = 3

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._async_abort_entries_match(user_input)
            try:
                await validate_input(self.hass, user_input)
            except anthropic.APITimeoutError:
                errors["base"] = "timeout_connect"
            except anthropic.APIConnectionError:
                errors["base"] = "cannot_connect"
            except anthropic.APIStatusError as e:
                errors["base"] = "unknown"
                if (
                    isinstance(e.body, dict)
                    and (error := e.body.get("error"))
                    and error.get("type") == "authentication_error"
                ):
                    errors["base"] = "authentication_error"
            except Exception:
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                if self.source == SOURCE_REAUTH:
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
```

**Key pattern**: On successful API key validation, `async_create_entry` is called with
`subentries=` to create a default conversation subentry alongside the main config entry.

#### Level 2: Subentry Flow (conversation settings)

```python
    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: AnthropicConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Return subentries supported by this integration."""
        return {
            "conversation": ConversationSubentryFlowHandler,
        }
```

```python
class ConversationSubentryFlowHandler(ConfigSubentryFlow):
    """Flow for managing conversation subentries."""

    options: dict[str, Any]

    @property
    def _is_new(self) -> bool:
        return self.source == "user"

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Add a subentry."""
        self.options = DEFAULT_CONVERSATION_OPTIONS.copy()
        return await self.async_step_init()

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Handle reconfiguration of a subentry."""
        self.options = self._get_reconfigure_subentry().data.copy()
        return await self.async_step_init()

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Set initial options."""
        if self._get_entry().state != ConfigEntryState.LOADED:
            return self.async_abort(reason="entry_not_loaded")

        # Build form schema with name, prompt, LLM API, recommended toggle
        # ...

        if user_input is not None:
            if user_input[CONF_RECOMMENDED]:
                if self._is_new:
                    return self.async_create_entry(
                        title=user_input.pop(CONF_NAME),
                        data=user_input,
                    )
                return self.async_update_and_abort(
                    self._get_entry(),
                    self._get_reconfigure_subentry(),
                    data=user_input,
                )
            else:
                self.options.update(user_input)
                return await self.async_step_advanced()

        return self.async_show_form(step_id="init", ...)

    async def async_step_advanced(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Manage advanced options (model, tokens, temperature)."""
        # ...
        if user_input is not None:
            self.options.update(user_input)
            return await self.async_step_model()

        return self.async_show_form(step_id="advanced", ...)

    async def async_step_model(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Manage model-specific options (thinking, web search)."""
        # ...
        if user_input is not None:
            self.options.update(user_input)
            if self._is_new:
                return self.async_create_entry(
                    title=self.options.pop(CONF_NAME),
                    data=self.options,
                )
            return self.async_update_and_abort(
                self._get_entry(),
                self._get_reconfigure_subentry(),
                data=self.options,
            )

        return self.async_show_form(step_id="model", ..., last_step=True)
```

### Multi-step flow pattern

Both Anthropic and OpenAI use the same multi-step pattern:

```
Step 1 (init):  Name + System Prompt + LLM API selection + "Use recommended?" toggle
    |
    +--[recommended=True]--> Create/update entry immediately
    |
    +--[recommended=False]--> Step 2 (advanced)
                                |
                                Step 2 (advanced): Model + Max tokens + Temperature
                                    |
                                    Step 3 (model): Model-specific options
                                        |
                                        Create/update entry
```

### Validation function pattern

```python
async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> None:
    """Validate the user input allows us to connect."""
    client = anthropic.AsyncAnthropic(
        api_key=data[CONF_API_KEY],
        http_client=get_async_client(hass),
    )
    await client.models.list(timeout=10.0)
```

### Reauth flow pattern

```python
async def async_step_reauth(
    self, entry_data: Mapping[str, Any]
) -> ConfigFlowResult:
    """Perform reauth upon an API authentication error."""
    return await self.async_step_reauth_confirm()

async def async_step_reauth_confirm(
    self, user_input: dict[str, Any] | None = None
) -> ConfigFlowResult:
    """Dialog that informs the user that reauth is required."""
    if not user_input:
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_USER_DATA_SCHEMA,
        )
    return await self.async_step_user(user_input)
```

### Form selectors used

Both integrations use these selector types from `homeassistant.helpers.selector`:

| Selector             | Use Case                          |
|----------------------|-----------------------------------|
| `TextSelector`       | Text input (API keys, URLs)       |
| `TemplateSelector`   | Jinja2 template input (prompts)   |
| `NumberSelector`     | Numeric values (temperature)      |
| `SelectSelector`     | Dropdowns (model selection)       |

---

## 7. conversation.py

### Anthropic conversation.py (complete)

```python
"""Conversation support for Anthropic."""

from typing import Literal

from homeassistant.components import conversation
from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import CONF_LLM_HASS_API, MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AnthropicConfigEntry
from .const import CONF_PROMPT, DOMAIN
from .entity import AnthropicBaseLLMEntity


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: AnthropicConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up conversation entities."""
    for subentry in config_entry.subentries.values():
        if subentry.subentry_type != "conversation":
            continue
        async_add_entities(
            [AnthropicConversationEntity(config_entry, subentry)],
            config_subentry_id=subentry.subentry_id,
        )


class AnthropicConversationEntity(
    conversation.ConversationEntity,
    conversation.AbstractConversationAgent,
    AnthropicBaseLLMEntity,
):
    """Anthropic conversation agent."""

    _attr_supports_streaming = True

    def __init__(
        self, entry: AnthropicConfigEntry, subentry: ConfigSubentry
    ) -> None:
        """Initialize the agent."""
        super().__init__(entry, subentry)
        if self.subentry.data.get(CONF_LLM_HASS_API):
            self._attr_supported_features = (
                conversation.ConversationEntityFeature.CONTROL
            )

    @property
    def supported_languages(self) -> list[str] | Literal["*"]:
        """Return a list of supported languages."""
        return MATCH_ALL

    async def _async_handle_message(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
    ) -> conversation.ConversationResult:
        """Call the API."""
        options = self.subentry.data

        try:
            await chat_log.async_provide_llm_data(
                user_input.as_llm_context(DOMAIN),
                options.get(CONF_LLM_HASS_API),
                options.get(CONF_PROMPT),
                user_input.extra_system_prompt,
            )
        except conversation.ConverseError as err:
            return err.as_conversation_result()

        await self._async_handle_chat_log(chat_log)

        return conversation.async_get_result_from_chat_log(user_input, chat_log)
```

### Key patterns in conversation.py

1. **Platform setup**: `async_setup_entry` iterates over subentries and creates one entity
   per `"conversation"` subentry, passing `config_subentry_id`.

2. **Triple inheritance**: The entity inherits from:
   - `conversation.ConversationEntity` -- the HA entity base
   - `conversation.AbstractConversationAgent` -- the conversation agent interface
   - `AnthropicBaseLLMEntity` -- custom base with API logic

3. **`_async_handle_message`** (not `async_process`): This is the **current recommended
   method** (as of HA 2025+). It receives `ConversationInput` and `ChatLog` and returns
   `ConversationResult`.

4. **`supported_languages`**: Return `MATCH_ALL` for all-language support.

5. **`_attr_supports_streaming = True`**: Enables streaming responses.

6. **CONTROL feature**: Set `_attr_supported_features` to
   `ConversationEntityFeature.CONTROL` when LLM has access to HA APIs.

7. **LLM data provision**: `chat_log.async_provide_llm_data(...)` sets up the system
   prompt, LLM API tools, etc.

8. **Result creation**: `conversation.async_get_result_from_chat_log(user_input, chat_log)`
   creates the standard result.

---

## 8. entity.py (Base LLM Entity)

### Anthropic entity.py pattern (simplified)

```python
"""Base entity for Anthropic."""

from homeassistant.components import conversation
from homeassistant.config_entries import ConfigSubentry
from homeassistant.helpers import device_registry as dr, llm
from homeassistant.helpers.entity import Entity

from . import AnthropicConfigEntry
from .const import CONF_CHAT_MODEL, DEFAULT, DOMAIN

MAX_TOOL_ITERATIONS = 10


class AnthropicBaseLLMEntity(Entity):
    """Anthropic base LLM entity."""

    _attr_has_entity_name = True
    _attr_name = None

    def __init__(
        self, entry: AnthropicConfigEntry, subentry: ConfigSubentry
    ) -> None:
        """Initialize the entity."""
        self.entry = entry
        self.subentry = subentry
        self._attr_unique_id = subentry.subentry_id
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, subentry.subentry_id)},
            name=subentry.title,
            manufacturer="Anthropic",
            model=subentry.data.get(CONF_CHAT_MODEL, DEFAULT[CONF_CHAT_MODEL]),
            entry_type=dr.DeviceEntryType.SERVICE,
        )

    async def _async_handle_chat_log(
        self,
        chat_log: conversation.ChatLog,
    ) -> None:
        """Generate an answer for the chat log."""
        options = self.subentry.data
        client = self.entry.runtime_data

        # Build system prompt from chat_log.content[0]
        # Convert chat_log content to API message format
        # Build model args (model, max_tokens, temperature, thinking, tools)
        # Loop up to MAX_TOOL_ITERATIONS:
        #   stream = await client.messages.create(**model_args)
        #   process stream into chat_log
        #   break if no unresponded tool results
        ...
```

### Key patterns

1. **`_attr_has_entity_name = True`** and **`_attr_name = None`**: The entity name
   is derived from the device name.
2. **`_attr_unique_id = subentry.subentry_id`**: Each subentry gets its own entity.
3. **`DeviceInfo`**: Each conversation agent registers as a device with
   `entry_type=dr.DeviceEntryType.SERVICE`.
4. **`entry.runtime_data`**: The authenticated API client stored during setup.
5. **Tool loop**: Both integrations iterate up to 10 times for tool calls, checking
   `chat_log.unresponded_tool_results` to determine if another iteration is needed.

---

## 9. strings.json & translations/en.json

### Anthropic strings.json pattern (simplified)

```json
{
  "config": {
    "abort": {
      "already_configured": "[%key:common::config_flow::abort::already_configured_service%]",
      "reauth_successful": "[%key:common::config_flow::abort::reauth_successful%]"
    },
    "error": {
      "authentication_error": "[%key:common::config_flow::error::invalid_auth%]",
      "cannot_connect": "[%key:common::config_flow::error::cannot_connect%]",
      "timeout_connect": "[%key:common::config_flow::error::timeout_connect%]",
      "unknown": "[%key:common::config_flow::error::unknown%]"
    },
    "step": {
      "user": {
        "data": {
          "api_key": "[%key:common::config_flow::data::api_key%]"
        },
        "data_description": {
          "api_key": "Your Anthropic API key."
        },
        "description": "Set up by providing your API key."
      },
      "reauth_confirm": {
        "data": {
          "api_key": "[%key:common::config_flow::data::api_key%]"
        },
        "description": "Reauthentication required."
      }
    }
  },
  "config_subentries": {
    "conversation": {
      "abort": {
        "entry_not_loaded": "Cannot add things while the configuration is disabled.",
        "reconfigure_successful": "[%key:common::config_flow::abort::reconfigure_successful%]"
      },
      "entry_type": "Conversation agent",
      "initiate_flow": {
        "reconfigure": "Reconfigure conversation agent",
        "user": "Add conversation agent"
      },
      "step": {
        "init": {
          "data": {
            "name": "[%key:common::config_flow::data::name%]",
            "prompt": "[%key:common::config_flow::data::prompt%]",
            "llm_hass_api": "[%key:common::config_flow::data::llm_hass_api%]",
            "recommended": "Recommended model settings"
          },
          "data_description": {
            "name": "The name of this configuration",
            "prompt": "Instruct how the LLM should respond. This can be a template.",
            "llm_hass_api": "Allow the LLM to control Home Assistant.",
            "recommended": "Use default configuration"
          },
          "title": "Basic settings"
        },
        "advanced": {
          "data": {
            "chat_model": "[%key:common::generic::model%]",
            "max_tokens": "Maximum tokens to return in response",
            "temperature": "Temperature"
          },
          "data_description": {
            "chat_model": "The model to serve the responses.",
            "max_tokens": "Limit the number of response tokens.",
            "temperature": "Control randomness."
          },
          "title": "Advanced settings"
        },
        "model": {
          "data": {
            "thinking_budget": "Thinking budget",
            "thinking_effort": "Thinking effort",
            "web_search": "Enable web search"
          },
          "title": "Model-specific options"
        }
      }
    }
  }
}
```

### Structure rules

- **`config`** section: For the main `ConfigFlow` (API key entry).
- **`config_subentries`** section: For `ConfigSubentryFlow` steps, keyed by
  subentry type (e.g. `"conversation"`).
- **`[%key:...]`** references: Common HA translations reused across integrations.
- **`translations/en.json`**: For custom integrations, this is a **copy** of
  `strings.json` with all `[%key:...]` references resolved to actual English text.
  Core integrations auto-generate translations; custom integrations must provide them.

### translations/en.json for custom integrations

For custom components, `translations/en.json` must contain the fully resolved strings
(no `[%key:...]` references):

```json
{
  "config": {
    "abort": {
      "already_configured": "Service is already configured",
      "reauth_successful": "Re-authentication was successful"
    },
    "error": {
      "authentication_error": "Invalid authentication",
      "cannot_connect": "Failed to connect",
      "timeout_connect": "Timeout establishing connection",
      "unknown": "Unexpected error"
    },
    "step": {
      "user": {
        "data": {
          "api_key": "API Key"
        },
        "data_description": {
          "api_key": "Your Anthropic API key."
        },
        "description": "Set up by providing your Anthropic API key."
      }
    }
  },
  "config_subentries": {
    "conversation": {
      "abort": {
        "entry_not_loaded": "Cannot configure while integration is disabled.",
        "reconfigure_successful": "Options updated successfully"
      },
      "entry_type": "Conversation agent",
      "initiate_flow": {
        "reconfigure": "Reconfigure conversation agent",
        "user": "Add conversation agent"
      },
      "step": {
        "init": {
          "data": {
            "name": "Name",
            "prompt": "System prompt",
            "llm_hass_api": "Control Home Assistant",
            "recommended": "Recommended model settings"
          },
          "data_description": {
            "name": "The name of this configuration",
            "prompt": "Instruct how the LLM should respond. This can be a template.",
            "llm_hass_api": "Allow the LLM to interact with Home Assistant.",
            "recommended": "Use default configuration"
          },
          "title": "Basic settings"
        },
        "advanced": {
          "data": {
            "chat_model": "Model",
            "max_tokens": "Maximum tokens to return in response",
            "temperature": "Temperature"
          },
          "data_description": {
            "chat_model": "The model used to generate responses.",
            "max_tokens": "Limit the number of response tokens.",
            "temperature": "Control randomness of the response."
          },
          "title": "Advanced settings"
        }
      }
    }
  }
}
```

---

## 10. Config Flow Design for Our Integration

### What configuration does the user need to provide?

| Setting                | Where              | Required | Description                          |
|------------------------|--------------------|----------|--------------------------------------|
| Anthropic API Key      | Main config entry  | Yes      | API key for Claude                   |
| Name                   | Subentry (init)    | Yes      | Display name for the agent           |
| System Prompt          | Subentry (init)    | No       | Custom instructions template         |
| LLM HA API access      | Subentry (init)    | No       | Which HA APIs the LLM can use        |
| Use Recommended?       | Subentry (init)    | Yes      | Toggle for default settings          |
| Chat Model             | Subentry (advanced)| No       | e.g. claude-sonnet-4-5               |
| Max Tokens             | Subentry (advanced)| No       | Response token limit                 |
| Temperature            | Subentry (advanced)| No       | Creativity vs coherence              |
| Thinking Effort        | Subentry (model)   | No       | Extended thinking control             |
| MCP Server configs     | Subentry or custom | No       | MCP server URLs, auth, transport     |

### Multi-step flow design

#### Main ConfigFlow (Step 1: API Key)

```
async_step_user:
  - Input: API key
  - Validate: client.models.list() with 10s timeout
  - On success: Create entry + default conversation subentry
  - Error handling: cannot_connect, authentication_error, timeout_connect, unknown
```

#### Conversation Subentry Flow

```
async_step_init (Basic Settings):
  - Input: Name, System Prompt (TemplateSelector), LLM API (SelectSelector),
           "Use recommended?" (bool)
  - If recommended=True: save and finish
  - If recommended=False: proceed to advanced

async_step_advanced (Advanced Settings):
  - Input: Chat Model (SelectSelector), Max Tokens (int),
           Temperature (NumberSelector 0-1)
  - On submit: proceed to model-specific step

async_step_model (Model-Specific Options):
  - Input: Thinking effort, MCP server settings
  - On submit: create/update subentry
  - last_step=True
```

### Options flow vs Subentry reconfigure

The modern pattern (Anthropic/OpenAI in 2025+) uses **subentry reconfigure** rather
than a separate options flow. Each subentry has its own `async_step_reconfigure` that
loads the existing data and walks through the same steps.

```python
async def async_step_reconfigure(
    self, user_input: dict[str, Any] | None = None
) -> SubentryFlowResult:
    """Handle reconfiguration of a subentry."""
    self.options = self._get_reconfigure_subentry().data.copy()
    return await self.async_step_init()
```

### Reauth flow

Both core integrations implement reauth for when API keys expire or become invalid.
The pattern redirects to `async_step_user` with the reauth source:

```python
if self.source == SOURCE_REAUTH:
    return self.async_update_reload_and_abort(
        self._get_reauth_entry(), data_updates=user_input
    )
```

---

## 11. Subentry Architecture

### What are subentries?

Subentries allow a single config entry (e.g., one API key) to support **multiple
conversation agents** with different settings. The Anthropic integration uses this
to allow multiple Claude conversation agents with different models, prompts, etc.

### How subentries work

1. **Parent entry** stores: `{ "api_key": "sk-ant-..." }` -- shared credentials.
2. **Subentries** store per-agent config: model, prompt, temperature, etc.
3. Each subentry creates its own **entity** and **device**.
4. Subentry types registered via `async_get_supported_subentry_types()`.

### Creating subentries on initial setup

```python
return self.async_create_entry(
    title="Claude",
    data=user_input,  # Just the API key
    subentries=[
        {
            "subentry_type": "conversation",
            "data": {
                "recommended": True,
                "llm_hass_api": ["assist"],
                "prompt": "...",
            },
            "title": "Claude Conversation Agent",
            "unique_id": None,
        },
    ],
)
```

### Adding subentries later

Users can add more conversation agents through the UI. This triggers the
`ConversationSubentryFlowHandler.async_step_user()` method.

### ConfigSubentryFlow API (post-March 2025)

Key methods available in `ConfigSubentryFlow`:
- `self._get_entry()` -- get the parent config entry
- `self._get_reconfigure_subentry()` -- get the subentry being reconfigured
- `self._subentry_type` -- the type of subentry being created/edited
- `self.source` -- `"user"` for new, `"reconfigure"` for editing
- `self.async_create_entry(title, data)` -- create a new subentry
- `self.async_update_and_abort(entry, subentry, data=data)` -- update existing

**References:**
- [ConfigSubentryFlow changes (March 2025)](https://developers.home-assistant.io/blog/2025/03/24/config-subentry-flow-changes/)

---

## 12. Key Patterns & Conventions

### Import conventions

```python
# HA core imports
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult
from homeassistant.config_entries import ConfigSubentryFlow, SubentryFlowResult
from homeassistant.const import CONF_API_KEY, CONF_LLM_HASS_API, CONF_NAME, MATCH_ALL
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import llm
from homeassistant.helpers.httpx_client import get_async_client

# Selectors
from homeassistant.helpers.selector import (
    NumberSelector, NumberSelectorConfig,
    SelectOptionDict, SelectSelector, SelectSelectorConfig, SelectSelectorMode,
    TemplateSelector,
)

# Voluptuous for schema validation
import voluptuous as vol
from homeassistant.helpers.typing import VolDictType
```

### LLM API integration

The `llm` module provides:

```python
from homeassistant.helpers import llm

# Get available LLM APIs for selector
hass_apis = [
    SelectOptionDict(label=api.name, value=api.id)
    for api in llm.async_get_apis(self.hass)
]

# Default instructions prompt
llm.DEFAULT_INSTRUCTIONS_PROMPT

# Default Assist API
llm.LLM_API_ASSIST
```

### Error handling in setup

```python
# AuthenticationError -> triggers reauth flow in UI
raise ConfigEntryAuthFailed(err) from err

# Other API errors -> shows "retry" in UI
raise ConfigEntryNotReady(err) from err
```

### httpx client

Always use HA's shared httpx client for HTTP requests:

```python
from homeassistant.helpers.httpx_client import get_async_client

client = anthropic.AsyncAnthropic(
    api_key=api_key,
    http_client=get_async_client(hass),
)
```

### Form schema building

```python
step_schema: VolDictType = {}

# Required field with default
step_schema[vol.Required(CONF_NAME, default="Claude")] = str

# Optional field with description for suggested value
step_schema[vol.Optional(CONF_PROMPT)] = TemplateSelector()

# Number selector with range
step_schema[vol.Optional(CONF_TEMPERATURE, default=1.0)] = NumberSelector(
    NumberSelectorConfig(min=0, max=1, step=0.05)
)

# Dropdown selector
step_schema[vol.Optional(CONF_CHAT_MODEL, default="claude-sonnet-4-5")] = SelectSelector(
    SelectSelectorConfig(
        options=[...],
        custom_value=True,  # Allow typing custom model names
    )
)

# Show form with suggested values from existing options
return self.async_show_form(
    step_id="init",
    data_schema=self.add_suggested_values_to_schema(
        vol.Schema(step_schema), self.options
    ),
)
```

### Conversation entity registration pattern

The entity is automatically registered as a conversation agent when:
1. It inherits from `conversation.ConversationEntity`
2. It's added via `async_add_entities` in the `conversation` platform
3. The `conversation` dependency is declared in `manifest.json`

No manual agent registration is needed in modern HA (the older pattern of
`conversation.async_set_agent` / `conversation.async_unset_agent` is handled
by the entity lifecycle).

### Version numbering

For ConfigFlow:
```python
VERSION = 2        # Major schema version
MINOR_VERSION = 3  # Minor schema version
```

Both Anthropic and OpenAI are currently at `VERSION = 2` with various minor versions.
For a new custom integration, start at `VERSION = 1, MINOR_VERSION = 1`.

---

## Summary: Complete File Checklist

| File                                           | Purpose                                    | Required |
|------------------------------------------------|--------------------------------------------|----------|
| `hacs.json`                                    | HACS metadata (name, HA version)           | Yes      |
| `README.md`                                    | Repository description                     | Yes      |
| `custom_components/claude_conversation_agent/` | Integration root directory                 | Yes      |
| `__init__.py`                                  | Setup, teardown, entry management          | Yes      |
| `manifest.json`                                | HA integration metadata + dependencies     | Yes      |
| `config_flow.py`                               | Config flow + subentry flow                | Yes      |
| `conversation.py`                              | ConversationEntity platform                | Yes      |
| `entity.py`                                    | Base entity with API call logic            | Yes      |
| `const.py`                                     | Domain, config keys, defaults              | Yes      |
| `strings.json`                                 | UI strings (with [%key:] references)       | Yes      |
| `translations/en.json`                         | Resolved English translations              | Yes      |

### Files NOT needed for a minimal integration

- `ai_task.py` -- Only needed if providing AI Task platform
- `services.yaml` -- Only needed if providing custom services
- `sensor.py` / other platforms -- Not needed for conversation-only
- `diagnostics.py` -- Optional, for debug info download
