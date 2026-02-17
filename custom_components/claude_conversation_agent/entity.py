"""Base entity for Claude Conversation Agent."""

from __future__ import annotations

from homeassistant.config_entries import ConfigSubentry
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity import Entity

from . import ClaudeAgentConfigEntry
from .const import CONF_CHAT_MODEL, DEFAULT_CHAT_MODEL, DOMAIN


class ClaudeBaseLLMEntity(Entity):
    """Claude Conversation Agent base entity."""

    _attr_has_entity_name = True
    _attr_name = None

    def __init__(
        self, entry: ClaudeAgentConfigEntry, subentry: ConfigSubentry
    ) -> None:
        """Initialize the entity."""
        self.entry = entry
        self.subentry = subentry
        self._attr_unique_id = subentry.subentry_id
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, subentry.subentry_id)},
            name=subentry.title,
            manufacturer="Anthropic",
            model=subentry.data.get(CONF_CHAT_MODEL, DEFAULT_CHAT_MODEL),
            entry_type=dr.DeviceEntryType.SERVICE,
        )
