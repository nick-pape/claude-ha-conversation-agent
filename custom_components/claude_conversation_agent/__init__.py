"""The Claude Conversation Agent integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import CONF_ADDON_URL, DEFAULT_ADDON_URL, DOMAIN

_LOGGER = logging.getLogger(__name__)

PLATFORMS = (Platform.CONVERSATION,)
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


@dataclass
class ClaudeAgentRuntimeData:
    """Runtime data for the Claude Agent integration."""

    addon_url: str


type ClaudeAgentConfigEntry = ConfigEntry[ClaudeAgentRuntimeData]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Claude Conversation Agent integration."""
    return True


async def async_setup_entry(
    hass: HomeAssistant, entry: ClaudeAgentConfigEntry
) -> bool:
    """Set up Claude Conversation Agent from a config entry."""
    addon_url = entry.data.get(CONF_ADDON_URL, DEFAULT_ADDON_URL)

    # Health-check the add-on
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{addon_url}/api/health",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    raise ConfigEntryNotReady(
                        f"Add-on health check failed: HTTP {resp.status}"
                    )
                data = await resp.json()
                if data.get("status") != "ok":
                    raise ConfigEntryNotReady(
                        f"Add-on not healthy: {data}"
                    )
    except aiohttp.ClientError as err:
        raise ConfigEntryNotReady(
            f"Cannot connect to Claude Agent add-on at {addon_url}: {err}"
        ) from err

    entry.runtime_data = ClaudeAgentRuntimeData(addon_url=addon_url)

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
