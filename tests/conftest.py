"""Shared fixtures for Claude Conversation Agent tests.

This conftest mocks all Home Assistant-specific imports before importing
the integration modules, so that tests can run without an actual HA
installation.

The production __init__.py uses Python 3.12+ ``type X = Y`` syntax.
We pre-register it as a stub in sys.modules so that the real file
is never parsed by the interpreter (which may be 3.11).
"""

from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Install HA stub modules so our custom_component can be imported.
# These stubs must be in place *before* any production code is imported.
# ---------------------------------------------------------------------------

def _make_module(name: str, package: str | None = None) -> ModuleType:
    """Return a fresh empty module registered in sys.modules."""
    mod = ModuleType(name)
    mod.__package__ = package or name
    sys.modules[name] = mod
    return mod


def _install_ha_stubs() -> None:  # noqa: C901
    """Create thin HA stub modules with enough surface for import."""

    # --- homeassistant package hierarchy ---
    ha = _make_module("homeassistant")
    ha_core = _make_module("homeassistant.core")
    ha_core.HomeAssistant = MagicMock  # type: ignore[attr-defined]
    ha_core.callback = lambda f: f  # type: ignore[attr-defined]

    _make_module("homeassistant.const")
    sys.modules["homeassistant.const"].CONF_API_KEY = "api_key"  # type: ignore[attr-defined]
    sys.modules["homeassistant.const"].MATCH_ALL = "*"  # type: ignore[attr-defined]
    sys.modules["homeassistant.const"].Platform = MagicMock()  # type: ignore[attr-defined]
    sys.modules["homeassistant.const"].CONF_NAME = "name"  # type: ignore[attr-defined]

    # config_entries
    ha_ce = _make_module("homeassistant.config_entries")
    ha_ce.ConfigEntry = MagicMock  # type: ignore[attr-defined]
    ha_ce.ConfigSubentry = MagicMock  # type: ignore[attr-defined]
    ha_ce.ConfigFlow = MagicMock  # type: ignore[attr-defined]
    ha_ce.ConfigFlowResult = MagicMock  # type: ignore[attr-defined]
    ha_ce.ConfigSubentryFlow = MagicMock  # type: ignore[attr-defined]
    ha_ce.SubentryFlowResult = MagicMock  # type: ignore[attr-defined]

    # exceptions
    ha_exc = _make_module("homeassistant.exceptions")
    ha_exc.ConfigEntryAuthFailed = type("ConfigEntryAuthFailed", (Exception,), {})  # type: ignore[attr-defined]
    ha_exc.ConfigEntryNotReady = type("ConfigEntryNotReady", (Exception,), {})  # type: ignore[attr-defined]
    ha_exc.HomeAssistantError = type("HomeAssistantError", (Exception,), {})  # type: ignore[attr-defined]

    # helpers
    _make_module("homeassistant.helpers")
    _make_module("homeassistant.helpers.config_validation")
    sys.modules["homeassistant.helpers.config_validation"].config_entry_only_config_schema = MagicMock()  # type: ignore[attr-defined]
    _make_module("homeassistant.helpers.httpx_client")
    sys.modules["homeassistant.helpers.httpx_client"].get_async_client = MagicMock()  # type: ignore[attr-defined]
    _make_module("homeassistant.helpers.typing")
    sys.modules["homeassistant.helpers.typing"].ConfigType = dict  # type: ignore[attr-defined]
    _make_module("homeassistant.helpers.device_registry")
    dr = sys.modules["homeassistant.helpers.device_registry"]
    dr.DeviceInfo = MagicMock  # type: ignore[attr-defined]
    dr.DeviceEntryType = MagicMock()  # type: ignore[attr-defined]
    _make_module("homeassistant.helpers.entity")
    sys.modules["homeassistant.helpers.entity"].Entity = type("Entity", (), {})  # type: ignore[attr-defined]
    _make_module("homeassistant.helpers.entity_platform")
    sys.modules["homeassistant.helpers.entity_platform"].AddConfigEntryEntitiesCallback = MagicMock  # type: ignore[attr-defined]

    # helpers.selector (used by config_flow)
    _make_module("homeassistant.helpers.selector")
    sel = sys.modules["homeassistant.helpers.selector"]
    for attr in (
        "NumberSelector", "NumberSelectorConfig",
        "SelectOptionDict", "SelectSelector", "SelectSelectorConfig",
        "TemplateSelector",
        "TextSelector", "TextSelectorConfig", "TextSelectorType",
    ):
        setattr(sel, attr, MagicMock())

    # components.conversation (used by conversation.py & entity.py)
    _make_module("homeassistant.components")
    ha_conv = _make_module("homeassistant.components.conversation")
    ha_conv.ConversationEntity = type("ConversationEntity", (), {})  # type: ignore[attr-defined]
    ha_conv.AbstractConversationAgent = type("AbstractConversationAgent", (), {})  # type: ignore[attr-defined]
    ha_conv.ConversationEntityFeature = MagicMock()  # type: ignore[attr-defined]
    ha_conv.ConversationInput = MagicMock  # type: ignore[attr-defined]
    ha_conv.ChatLog = MagicMock  # type: ignore[attr-defined]
    ha_conv.ConversationResult = MagicMock  # type: ignore[attr-defined]
    ha_conv.ConverseError = type("ConverseError", (Exception,), {})  # type: ignore[attr-defined]
    ha_conv.async_get_result_from_chat_log = MagicMock()  # type: ignore[attr-defined]


def _install_integration_package_stubs() -> None:
    """Pre-register the custom_components package hierarchy.

    The real ``__init__.py`` uses Python 3.12+ ``type`` statement syntax,
    so we install a lightweight stub in sys.modules to prevent the real
    file from being parsed.  Sub-modules (const, agent) are then imported
    normally via importlib.
    """
    # Package: custom_components
    cc = _make_module("custom_components", package="custom_components")
    cc.__path__ = [
        str(Path(__file__).resolve().parent.parent / "custom_components")
    ]

    # Sub-package: custom_components.claude_conversation_agent
    pkg_name = "custom_components.claude_conversation_agent"
    pkg = _make_module(pkg_name, package=pkg_name)
    pkg.__path__ = [
        str(
            Path(__file__).resolve().parent.parent
            / "custom_components"
            / "claude_conversation_agent"
        )
    ]
    # Provide the ClaudeAgentConfigEntry attribute that other modules expect
    pkg.ClaudeAgentConfigEntry = MagicMock  # type: ignore[attr-defined]

    # Now import the real sub-modules using importlib
    importlib.import_module("custom_components.claude_conversation_agent.const")
    importlib.import_module("custom_components.claude_conversation_agent.agent")


# Install stubs at import time - before any test file is collected.
_install_ha_stubs()
_install_integration_package_stubs()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def conversation_state():
    """Return a fresh ConversationState."""
    from custom_components.claude_conversation_agent.agent import ConversationState
    return ConversationState()


@pytest.fixture
def state_manager():
    """Return a ConversationStateManager with a short TTL for testing."""
    from custom_components.claude_conversation_agent.agent import ConversationStateManager
    return ConversationStateManager(ttl_seconds=10.0)
