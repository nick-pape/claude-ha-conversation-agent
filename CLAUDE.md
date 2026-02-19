# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Home Assistant custom integration that uses Anthropic's Claude as a conversation agent, powered by the Claude Agent SDK. Claude controls smart home devices exclusively through MCP (Model Context Protocol) servers -- it never uses HA's native intent system. The integration slots into HA's voice pipeline as the "brain" between STT and TTS.

The Claude Agent SDK handles the full agent loop: MCP connections, tool discovery, tool execution, and multi-turn reasoning. The integration is a thin wrapper that feeds user text in and streams text deltas out.

## Commands

### Run tests
```bash
pytest tests/
```

### Run a single test file
```bash
pytest tests/test_agent.py
pytest tests/test_mcp_manager.py
```

### Run a single test
```bash
pytest tests/test_agent.py::TestRunAgentLoopSimpleText::test_simple_text_response -v
```

There is no build step, linter config, or type checker configured. The integration is pure Python loaded by Home Assistant at runtime.

## Architecture

### Request Flow

```
HA Voice Pipeline → ClaudeConversationEntity._async_handle_message()
  → run_agent_loop() (async generator, yields text deltas for TTS streaming)
    → Claude Agent SDK query() (handles MCP, tool calls, multi-turn internally)
      → streams back SystemMessage, StreamEvent, AssistantMessage, ResultMessage
    → extracts text deltas from StreamEvent, captures session_id from ResultMessage
```

### Key Modules (`custom_components/claude_conversation_agent/`)

- **`agent.py`** -- `run_agent_loop()` async generator that wraps the Claude Agent SDK's `query()`. Extracts streaming text deltas from `StreamEvent` messages and captures session IDs from `ResultMessage` for conversation continuity. `ConversationStateManager` handles per-session state with TTL-based expiry (5 min default).
- **`conversation.py`** -- `ClaudeConversationEntity` wires HA's conversation interface to the agent loop. Pipes async generator deltas into `ChatLog.async_add_delta_content_stream()`.
- **`config_flow.py`** -- Multi-step UI config: API key validation → conversation settings → advanced model params → MCP server config. Uses HA subentries (parent entry holds API key, subentries hold per-agent config).
- **`__init__.py`** -- Entry setup, creates `AsyncAnthropic` client. Uses Python 3.12+ `type` statement syntax.
- **`const.py`** -- All config keys, defaults, and limits.

### Key Patterns

- **Agent SDK delegation**: `run_agent_loop()` calls `claude_agent_sdk.query()` with `ClaudeAgentOptions`. The SDK manages the full agent loop (MCP connections, tool discovery, tool execution, multi-turn reasoning) internally.
- **Streaming via StreamEvent**: With `include_partial_messages=True`, the SDK yields raw Claude API stream events. We extract `content_block_start` (text type) and `content_block_delta` (text_delta type) events.
- **Session-based continuity**: `ConversationState` stores the SDK session ID. On follow-up turns, this is passed via `resume` to restore full context.
- **Config subentries**: One parent ConfigEntry (API key) with multiple conversation subentries (each an independent agent with its own prompt, model, and MCP config).

## Testing

Tests mock all Home Assistant modules via `conftest.py` stubs installed into `sys.modules` before any production code is imported. This allows tests to run without an actual HA installation. The `__init__.py` stub is pre-registered to avoid parsing its Python 3.12+ `type` syntax on older interpreters.

The `claude_agent_sdk` module is also stubbed in `conftest.py` with dataclass versions of `SystemMessage`, `AssistantMessage`, `ResultMessage`, `StreamEvent`, and `ClaudeAgentOptions`. Tests patch `claude_agent_sdk.query` at the source module level (not on the agent module) because the import happens inside the function body.

Key fixtures: `mock_anthropic_client`, `mock_mcp_session`, `mcp_manager`, `conversation_state`, `state_manager`.

## Dependencies

- `anthropic>=0.49.0` -- Anthropic Python SDK (API key validation, model listing)
- `claude-agent-sdk>=0.1.38` -- Claude Agent SDK (agent loop, MCP, tool calling)
- Home Assistant 2025.7.0+ (runtime dependency, not pip-installed)
