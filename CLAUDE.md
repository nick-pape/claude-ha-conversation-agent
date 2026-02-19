# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Home Assistant custom integration that uses Anthropic's Claude as a conversation agent. Claude controls smart home devices exclusively through MCP (Model Context Protocol) servers -- it never uses HA's native intent system. The integration slots into HA's voice pipeline as the "brain" between STT and TTS.

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
pytest tests/test_agent.py::TestRunAgentLoop::test_simple_text_response -v
```

There is no build step, linter config, or type checker configured. The integration is pure Python loaded by Home Assistant at runtime.

## Architecture

### Request Flow

```
HA Voice Pipeline → ClaudeConversationEntity._async_handle_message()
  → run_agent_loop() (async generator, yields text deltas for TTS streaming)
    → Claude Messages API (streaming)
      → tool_use block? → MCPManager.call_tool() → MCP Server → tool result fed back
    → loops up to MAX_TOOL_ITERATIONS (10) rounds of tool calls
```

### Key Modules (`custom_components/claude_conversation_agent/`)

- **`agent.py`** -- `run_agent_loop()` async generator that streams Claude responses and dispatches tool calls. `ConversationStateManager` handles per-session message history with TTL-based expiry (5 min default).
- **`mcp_manager.py`** -- `MCPManager` connects to MCP servers, discovers tools, executes calls with 30s timeout. Tries Streamable HTTP first, falls back to SSE on HTTP 405.
- **`conversation.py`** -- `ClaudeConversationEntity` wires HA's conversation interface to the agent loop. Pipes async generator deltas into `ChatLog.async_add_delta_content_stream()`.
- **`config_flow.py`** -- Multi-step UI config: API key validation → conversation settings → advanced model params → MCP server config. Uses HA subentries (parent entry holds API key, subentries hold per-agent config).
- **`__init__.py`** -- Entry setup, creates `AsyncAnthropic` client. Uses Python 3.12+ `type` statement syntax.
- **`const.py`** -- All config keys, defaults, and limits.

### Key Patterns

- **MCP tool namespacing**: Tools registered as `{server_name}__{tool_name}` to avoid collisions across multiple MCP servers.
- **Streaming via async generator**: `run_agent_loop()` yields `{"role": "assistant"}` once, then `{"content": "..."}` dicts for each text delta. Tool use blocks are consumed internally and never yielded.
- **Config subentries**: One parent ConfigEntry (API key) with multiple conversation subentries (each an independent agent with its own prompt, model, and MCP config).

## Testing

Tests mock all Home Assistant modules via `conftest.py` stubs installed into `sys.modules` before any production code is imported. This allows tests to run without an actual HA installation. The `__init__.py` stub is pre-registered to avoid parsing its Python 3.12+ `type` syntax on older interpreters.

Key fixtures: `mock_anthropic_client`, `mock_mcp_session`, `mcp_manager`, `conversation_state`, `state_manager`.

## Dependencies

- `anthropic>=0.49.0` -- Anthropic Python SDK
- `mcp>=1.26.0` -- Model Context Protocol client
- Home Assistant 2025.7.0+ (runtime dependency, not pip-installed)
