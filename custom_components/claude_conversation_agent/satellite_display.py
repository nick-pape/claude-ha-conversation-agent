"""Mirror the agent's reply to the chat pane of an alfred-satellite.

Home Assistant tells an ESPHome satellite only the reply's last message (the
speech in INTENT_END) and nothing about tool calls. Our own satellite
(alfred-satellite, on the Fire TV Cube) shows the whole conversation on the TV,
so for that device the streamed text and the tool calls ride extra fields on
INTENT_PROGRESS events, sent through the ESPHome integration's API client:

    alfred_text=<chunk>              part of the reply, including what is said before a tool call
    alfred_tool_start=<tool name>
    alfred_tool_done=<tool name>

Other ESPHome firmware may act on any INTENT_PROGRESS event, so only devices
whose ESPHome project is alfred-satellite get these.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

_LOGGER = logging.getLogger(__name__)

SATELLITE_PROJECT = "nickpape.alfred-satellite"


def satellite_client(hass: HomeAssistant, device_id: str | None) -> Any | None:
    """Return the ESPHome API client of device_id if it is an alfred-satellite, else None."""
    if not device_id:
        return None
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        return None
    for entry_id in device.config_entries:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != "esphome" or entry.state is not ConfigEntryState.LOADED:
            continue
        runtime = getattr(entry, "runtime_data", None)
        info = getattr(runtime, "device_info", None)
        if getattr(info, "project_name", None) == SATELLITE_PROJECT:
            return runtime.client
    return None


def satellite_nodes(hass: HomeAssistant) -> list[tuple[str, str]]:
    """(config entry id, ESPHome node name) of every connected alfred-satellite."""
    return [
        (entry.entry_id, entry.runtime_data.device_info.name)
        for entry in hass.config_entries.async_entries("esphome")
        if entry.state is ConfigEntryState.LOADED
        and getattr(getattr(getattr(entry, "runtime_data", None), "device_info", None), "project_name", None)
        == SATELLITE_PROJECT
    ]


def satellite_node(hass: HomeAssistant, device_id: str | None) -> str | None:
    """ESPHome node name of the alfred-satellite that heard the request.

    A typed request has no device; then the only alfred-satellite in the house,
    if there is exactly one.
    """
    satellites = satellite_nodes(hass)
    if device_id is None:
        return satellites[0][1] if len(satellites) == 1 else None
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        return None
    return next((name for entry_id, name in satellites if entry_id in device.config_entries), None)


async def mirror_to_satellite(
    deltas: AsyncIterator[dict[str, Any]], client: Any
) -> AsyncIterator[dict[str, Any]]:
    """Pass the agent's deltas through, copying its text and tool calls to the satellite."""
    # Ships with Home Assistant's ESPHome integration, which owns the client.
    from aioesphomeapi import VoiceAssistantEventType  # noqa: PLC0415

    async for delta in deltas:
        fields: dict[str, str] = {}
        if content := delta.get("content"):
            fields["alfred_text"] = content
        for call in delta.get("tool_calls") or ():
            fields["alfred_tool_start"] = call.tool_name
        if delta.get("role") == "tool_result":
            fields["alfred_tool_done"] = delta.get("tool_name", "")
        if fields:
            try:
                client.send_voice_assistant_event(
                    VoiceAssistantEventType.VOICE_ASSISTANT_INTENT_PROGRESS, fields
                )
            except Exception:  # noqa: BLE001 - the chat pane must never break the conversation
                _LOGGER.debug("Could not mirror the reply to the satellite", exc_info=True)
        yield delta
