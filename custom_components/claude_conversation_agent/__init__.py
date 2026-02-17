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

type ClaudeAgentConfigEntry = ConfigEntry[anthropic.AsyncAnthropic]

PLATFORMS = (Platform.CONVERSATION,)
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Claude Conversation Agent integration."""
    return True


async def async_setup_entry(
    hass: HomeAssistant, entry: ClaudeAgentConfigEntry
) -> bool:
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

    entry.async_on_unload(entry.add_update_listener(_async_update_options))

    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: ClaudeAgentConfigEntry
) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_options(
    hass: HomeAssistant, entry: ClaudeAgentConfigEntry
) -> None:
    """Update options triggers reload."""
    await hass.config_entries.async_reload(entry.entry_id)
