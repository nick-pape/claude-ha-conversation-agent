"""Tests for satellite_display.py – mirroring the reply to an alfred-satellite's chat pane."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
import pytest_asyncio  # noqa: F401 – enables async tests

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.llm import ToolInput

from custom_components.claude_conversation_agent.satellite_display import (
    SATELLITE_PROJECT,
    mirror_to_satellite,
    satellite_client,
    satellite_node,
    satellite_nodes,
)

INTENT_PROGRESS = 100


async def _stream(deltas: list[dict[str, Any]]):
    for d in deltas:
        yield d


def _hass_with_device(project: str | None, domain: str = "esphome", state=None) -> tuple[MagicMock, Any]:
    """A hass whose device "dev1" has one config entry with the given ESPHome project."""
    client = MagicMock(name="client")
    entry = SimpleNamespace(
        domain=domain,
        state=state or ConfigEntryState.LOADED,
        runtime_data=SimpleNamespace(client=client, device_info=SimpleNamespace(project_name=project)),
    )
    hass = MagicMock()
    hass.config_entries.async_get_entry.side_effect = lambda entry_id: entry if entry_id == "e1" else None
    registry = MagicMock()
    registry.async_get.side_effect = lambda device_id: (
        SimpleNamespace(config_entries={"e1"}) if device_id == "dev1" else None
    )
    dr.async_get.return_value = registry
    return hass, client


class TestSatelliteClient:
    def test_alfred_satellite_gets_its_client(self):
        hass, client = _hass_with_device(SATELLITE_PROJECT)
        assert satellite_client(hass, "dev1") is client

    def test_no_device(self):
        hass, _ = _hass_with_device(SATELLITE_PROJECT)
        assert satellite_client(hass, None) is None
        assert satellite_client(hass, "other") is None

    def test_other_esphome_projects_are_left_alone(self):
        hass, _ = _hass_with_device("esphome.voice-assistant")
        assert satellite_client(hass, "dev1") is None

    def test_unloaded_or_non_esphome_entries_are_skipped(self):
        hass, _ = _hass_with_device(SATELLITE_PROJECT, state=ConfigEntryState.NOT_LOADED)
        assert satellite_client(hass, "dev1") is None
        hass, _ = _hass_with_device(SATELLITE_PROJECT, domain="wyoming")
        assert satellite_client(hass, "dev1") is None


def _hass_with_satellites(*entries: tuple[str, str, str | None]) -> MagicMock:
    """A hass with ESPHome entries (entry id, node name, project); device "dev1" belongs to entry "e1"."""
    esphome = [
        SimpleNamespace(
            entry_id=entry_id,
            state=ConfigEntryState.LOADED,
            runtime_data=SimpleNamespace(device_info=SimpleNamespace(name=name, project_name=project)),
        )
        for entry_id, name, project in entries
    ]
    hass = MagicMock()
    hass.config_entries.async_entries.side_effect = lambda domain: esphome if domain == "esphome" else []
    registry = MagicMock()
    registry.async_get.side_effect = lambda device_id: (
        SimpleNamespace(config_entries={"e1"}) if device_id == "dev1" else None
    )
    dr.async_get.return_value = registry
    return hass


class TestSatelliteNode:
    def test_the_satellite_that_heard_it(self):
        hass = _hass_with_satellites(("e1", "alfred-cube", SATELLITE_PROJECT), ("e2", "alfred-den", SATELLITE_PROJECT))
        assert satellite_node(hass, "dev1") == "alfred-cube"

    def test_other_esphome_devices_are_not_satellites(self):
        hass = _hass_with_satellites(("e1", "kitchen-pe", "esphome.voice-assistant"))
        assert satellite_node(hass, "dev1") is None
        assert satellite_node(hass, None) is None

    def test_typed_requests_use_the_only_satellite(self):
        hass = _hass_with_satellites(("e1", "alfred-cube", SATELLITE_PROJECT), ("e3", "kitchen-pe", None))
        assert satellite_node(hass, None) == "alfred-cube"

    def test_all_satellites_are_listed(self):
        hass = _hass_with_satellites(
            ("e1", "alfred-cube", SATELLITE_PROJECT), ("e2", "alfred-den", SATELLITE_PROJECT), ("e3", "kitchen-pe", None)
        )
        assert satellite_nodes(hass) == [("e1", "alfred-cube"), ("e2", "alfred-den")]

    def test_typed_requests_with_two_satellites_have_none(self):
        hass = _hass_with_satellites(("e1", "alfred-cube", SATELLITE_PROJECT), ("e2", "alfred-den", SATELLITE_PROJECT))
        assert satellite_node(hass, None) is None


class TestMirrorToSatellite:
    @pytest.mark.asyncio
    async def test_text_and_tool_calls_are_mirrored_and_passed_through(self):
        client = MagicMock()
        deltas = [
            {"role": "assistant"},
            {"content": "Lemme check. "},
            {"tool_calls": [ToolInput(tool_name="GetLiveContext", tool_args={}, id="t1", external=True)]},
            {"role": "tool_result", "tool_call_id": "t1", "tool_name": "GetLiveContext", "result": None},
            {"role": "assistant"},
            {"content": "It's off."},
        ]

        out = [d async for d in mirror_to_satellite(_stream(deltas), client)]

        assert out == deltas
        sent = [(c.args[0], c.args[1]) for c in client.send_voice_assistant_event.call_args_list]
        assert sent == [
            (INTENT_PROGRESS, {"alfred_text": "Lemme check. "}),
            (INTENT_PROGRESS, {"alfred_tool_start": "GetLiveContext"}),
            (INTENT_PROGRESS, {"alfred_tool_done": "GetLiveContext"}),
            (INTENT_PROGRESS, {"alfred_text": "It's off."}),
        ]

    @pytest.mark.asyncio
    async def test_a_failing_client_does_not_break_the_reply(self):
        client = MagicMock()
        client.send_voice_assistant_event.side_effect = ConnectionError("gone")
        deltas = [{"content": "Hi"}, {"content": " there"}]

        out = [d async for d in mirror_to_satellite(_stream(deltas), client)]

        assert out == deltas
