# Claude Agent SDK & Anthropic API - Technical Research

## Executive Summary

There are **two distinct approaches** to powering a Home Assistant conversation agent with Claude + MCP:

1. **Claude Agent SDK** (`claude-agent-sdk` package) - A high-level SDK wrapping the Claude Code CLI, providing built-in tools, agent loops, MCP integration, streaming, hooks, and session management. It runs Claude Code as a subprocess.

2. **Raw Anthropic API** (`anthropic` package) - The lower-level Messages API with manual tool dispatch. Offers maximum control over the agent loop, streaming, and conversation management. Supports an MCP connector feature (beta) for remote MCP servers, and a `tool_runner` (beta) for automated tool loops.

**Recommendation for Home Assistant**: The **Claude Agent SDK** is the better fit if we want MCP server integration with minimal code. The **raw Anthropic API** is the better fit if we need maximum control over streaming behavior and want to avoid running the Claude Code CLI as a subprocess. A hybrid approach is also viable.

---

## Part 1: Claude Agent SDK (claude-agent-sdk)

### Package Information

- **Package name**: `claude-agent-sdk`
- **Installation**: `pip install claude-agent-sdk`
- **Python requirement**: >= 3.10
- **Latest version**: 0.1.37 (as of Feb 16, 2026)
- **GitHub**: https://github.com/anthropics/claude-agent-sdk-python
- **Docs**: https://platform.claude.com/docs/en/agent-sdk/overview

### Architecture

The Claude Agent SDK wraps the **Claude Code CLI** binary, which is automatically bundled with the package. When you call `query()` or use `ClaudeSDKClient`, the SDK:

1. Spawns a Claude Code CLI subprocess
2. Communicates with it via stdin/stdout (JSON messages)
3. The CLI handles all API calls to Claude, tool execution, and MCP server management
4. Messages stream back through stdout as JSON-line events

This means:
- The Claude Code CLI is the actual runtime; the Python SDK is a wrapper
- MCP servers are managed by the CLI process (stdio servers become child processes of the CLI)
- All built-in tools (Read, Write, Bash, Glob, Grep, etc.) are provided by the CLI

### Two APIs: `query()` vs `ClaudeSDKClient`

| Feature             | `query()`                     | `ClaudeSDKClient`                  |
|---------------------|-------------------------------|------------------------------------|
| Session             | Creates new session each time | Reuses same session                |
| Conversation        | Single exchange               | Multiple exchanges in same context |
| Connection          | Managed automatically         | Manual control                     |
| Streaming Input     | Supported                     | Supported                          |
| Interrupts          | Not supported                 | Supported                          |
| Hooks               | Not supported                 | Supported                          |
| Custom Tools        | Not supported                 | Supported                          |
| Continue Chat       | New session each time         | Maintains conversation             |

**For Home Assistant**: `ClaudeSDKClient` is what we need because:
- Multi-turn conversation support (HA conversations are multi-turn)
- Hook support for intercepting tool calls
- Custom tool support for in-process MCP servers
- Interrupt capability for stopping long-running operations

### Agent Creation and Configuration

```python
from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions

options = ClaudeAgentOptions(
    # Model selection
    model="claude-sonnet-4-5-20250929",  # or any Claude model
    fallback_model="claude-haiku-4-5-20250929",

    # System prompt
    system_prompt="You are a helpful Home Assistant agent...",
    # Or use Claude Code's preset with additions:
    # system_prompt={"type": "preset", "preset": "claude_code", "append": "Extra instructions"},

    # Tool configuration
    allowed_tools=["mcp__hass__*", "mcp__calendar__*"],
    disallowed_tools=["Bash", "Write"],  # Block dangerous built-in tools

    # MCP server connections
    mcp_servers={
        "hass": {
            "type": "sse",
            "url": "http://localhost:8123/mcp/sse",
            "headers": {"Authorization": "Bearer LONG_LIVED_TOKEN"}
        },
        "calendar": {
            "command": "npx",
            "args": ["-y", "@anthropic/mcp-calendar"]
        }
    },

    # Permission handling
    permission_mode="bypassPermissions",  # For automation (no human approval)

    # Limits
    max_turns=10,
    max_budget_usd=0.50,

    # Streaming output (CRITICAL for our use case)
    include_partial_messages=True,

    # Working directory
    cwd="/config",

    # Environment variables
    env={"ANTHROPIC_API_KEY": "sk-ant-..."},
)

client = ClaudeSDKClient(options=options)
```

### Multi-Turn Conversation Pattern

```python
from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions,
    AssistantMessage, ResultMessage, TextBlock, StreamEvent
)

async def handle_conversation():
    options = ClaudeAgentOptions(
        system_prompt="You are a Home Assistant agent...",
        mcp_servers={"hass": {"type": "sse", "url": "..."}},
        allowed_tools=["mcp__hass__*"],
        permission_mode="bypassPermissions",
        include_partial_messages=True,
    )

    async with ClaudeSDKClient(options=options) as client:
        # Turn 1
        await client.query("Turn on the living room lights")
        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        print(f"Claude: {block.text}")

        # Turn 2 - Claude remembers context
        await client.query("Now dim them to 50%")
        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        print(f"Claude: {block.text}")
```

### Session Resume Pattern (Alternative to ClaudeSDKClient)

```python
from claude_agent_sdk import query, ClaudeAgentOptions

# First query - capture session ID
session_id = None
async for message in query(
    prompt="Turn on the living room lights",
    options=ClaudeAgentOptions(
        mcp_servers={"hass": {"type": "sse", "url": "..."}},
        allowed_tools=["mcp__hass__*"],
    ),
):
    if hasattr(message, "subtype") and message.subtype == "init":
        session_id = message.data.get("session_id")
    if hasattr(message, "result"):
        print(message.result)

# Second query - resume session
async for message in query(
    prompt="Now dim them to 50%",
    options=ClaudeAgentOptions(resume=session_id),
):
    if hasattr(message, "result"):
        print(message.result)
```

### Agent Loop Lifecycle

A single `query()` or `client.query()` call triggers:

1. **Session init**: CLI starts, MCP servers connect, system message emitted with `subtype="init"`
2. **LLM call**: Claude receives the prompt + system prompt + tool definitions + conversation history
3. **Response streaming**: Claude's response streams back as `StreamEvent` messages (if `include_partial_messages=True`)
4. **Tool execution**: If Claude requests tool use, the CLI executes tools (MCP calls, built-in tools)
5. **Loop continuation**: Tool results are fed back to Claude; Claude may call more tools or produce final text
6. **Completion**: `ResultMessage` emitted when Claude finishes (or `max_turns` reached)

The agent loop is fully autonomous - the SDK handles all tool dispatch and result collection internally. You just consume the message stream.

### Streaming: Token-by-Token Output (CRITICAL FINDING)

**YES, the Agent SDK supports token-by-token streaming**, including during multi-step tool use. This is enabled with `include_partial_messages=True`.

#### How It Works

With `include_partial_messages=True`, the message flow is:

```
StreamEvent (message_start)
StreamEvent (content_block_start) - text block
StreamEvent (content_block_delta) - text chunks...     <-- Token-by-token text
StreamEvent (content_block_stop)
StreamEvent (content_block_start) - tool_use block
StreamEvent (content_block_delta) - tool input chunks... <-- Tool input streaming
StreamEvent (content_block_stop)
StreamEvent (message_delta)
StreamEvent (message_stop)
AssistantMessage - complete message with all content
... tool executes ...
... more streaming events for next turn ...
ResultMessage - final result
```

#### Streaming Code Example

```python
from claude_agent_sdk import query, ClaudeAgentOptions
from claude_agent_sdk.types import StreamEvent
import asyncio


async def stream_ha_response():
    options = ClaudeAgentOptions(
        include_partial_messages=True,
        mcp_servers={"hass": {"type": "sse", "url": "..."}},
        allowed_tools=["mcp__hass__*"],
        permission_mode="bypassPermissions",
    )

    in_tool = False
    accumulated_text = ""

    async for message in query(
        prompt="What's the temperature in the house?",
        options=options,
    ):
        if isinstance(message, StreamEvent):
            event = message.event
            event_type = event.get("type")

            if event_type == "content_block_start":
                content_block = event.get("content_block", {})
                if content_block.get("type") == "tool_use":
                    tool_name = content_block.get("name")
                    print(f"\n[Calling {tool_name}...]", end="", flush=True)
                    in_tool = True

            elif event_type == "content_block_delta":
                delta = event.get("delta", {})
                if delta.get("type") == "text_delta" and not in_tool:
                    text = delta.get("text", "")
                    accumulated_text += text
                    print(text, end="", flush=True)

            elif event_type == "content_block_stop":
                if in_tool:
                    print(" done", flush=True)
                    in_tool = False

    return accumulated_text
```

#### Streaming Limitations

- **Extended thinking**: When `max_thinking_tokens` is set, `StreamEvent` messages are NOT emitted. Only complete messages are received.
- **Structured output**: JSON results appear only in the final `ResultMessage.structured_output`, not as streaming deltas.

### Hooks System

Hooks run custom code at key points in the agent lifecycle. Available only with `ClaudeSDKClient`.

**Available hooks:**
- `PreToolUse` - Before tool execution (can block, modify input, deny)
- `PostToolUse` - After tool execution (can log, modify output)
- `UserPromptSubmit` - When user submits a prompt
- `Stop` - When execution stops
- `SubagentStop` - When a subagent stops
- `PreCompact` - Before message compaction

**NOT available in Python SDK:**
- `SessionStart`, `SessionEnd`, `Notification`

```python
from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions, HookMatcher, HookContext
from typing import Any


async def log_tool_use(
    input_data: dict[str, Any],
    tool_use_id: str | None,
    context: HookContext
) -> dict[str, Any]:
    """Log all tool usage for HA audit trail."""
    tool_name = input_data.get("tool_name", "unknown")
    tool_input = input_data.get("tool_input", {})
    print(f"[HA Audit] Tool: {tool_name}, Input: {tool_input}")
    return {}


async def block_dangerous_tools(
    input_data: dict[str, Any],
    tool_use_id: str | None,
    context: HookContext
) -> dict[str, Any]:
    """Block dangerous HA actions."""
    tool_name = input_data.get("tool_name", "unknown")
    tool_input = input_data.get("tool_input", {})

    # Block dangerous automation modifications
    if "delete" in tool_name.lower() or "remove" in tool_name.lower():
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "Destructive actions not allowed",
            }
        }
    return {}


options = ClaudeAgentOptions(
    hooks={
        "PreToolUse": [
            HookMatcher(hooks=[log_tool_use]),  # All tools
            HookMatcher(matcher="mcp__hass__*", hooks=[block_dangerous_tools]),
        ],
        "PostToolUse": [
            HookMatcher(hooks=[log_tool_use]),
        ],
    }
)
```

### Custom Permission Handler

```python
from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny


async def ha_permission_handler(
    tool_name: str, input_data: dict, context: dict
) -> PermissionResultAllow | PermissionResultDeny:
    """Custom permission logic for HA tools."""

    # Allow all MCP tool reads
    if "get" in tool_name.lower() or "list" in tool_name.lower():
        return PermissionResultAllow(updated_input=input_data)

    # Block certain tools entirely
    if tool_name in ["Bash", "Write", "Edit"]:
        return PermissionResultDeny(
            message="Built-in code tools not allowed", interrupt=False
        )

    # Allow everything else
    return PermissionResultAllow(updated_input=input_data)


options = ClaudeAgentOptions(
    can_use_tool=ha_permission_handler,
    allowed_tools=["mcp__hass__*"],
)
```

### MCP Server Integration

#### Transport Types

```python
# 1. stdio - Local process (stdin/stdout communication)
mcp_servers = {
    "hass": {
        "command": "python",
        "args": ["-m", "hass_mcp_server"],
        "env": {"HASS_TOKEN": "..."}
    }
}

# 2. SSE - Server-Sent Events (remote, streaming)
mcp_servers = {
    "hass": {
        "type": "sse",
        "url": "http://localhost:8123/mcp/sse",
        "headers": {"Authorization": "Bearer TOKEN"}
    }
}

# 3. HTTP - Streamable HTTP (remote, request/response)
mcp_servers = {
    "hass": {
        "type": "http",
        "url": "http://localhost:8123/mcp",
        "headers": {"Authorization": "Bearer TOKEN"}
    }
}

# 4. SDK (in-process) - Python functions as MCP tools
from claude_agent_sdk import tool, create_sdk_mcp_server

@tool("get_state", "Get entity state", {"entity_id": str})
async def get_state(args):
    # Direct Python call to HA API
    state = await hass_api.get_state(args["entity_id"])
    return {"content": [{"type": "text", "text": str(state)}]}

my_server = create_sdk_mcp_server(
    name="hass_custom",
    version="1.0.0",
    tools=[get_state]
)

mcp_servers = {"hass_custom": my_server}
```

#### Tool Naming Convention

MCP tools follow the pattern: `mcp__<server-name>__<tool-name>`

Examples:
- `mcp__hass__get_state`
- `mcp__hass__call_service`
- `mcp__calendar__list_events`

Wildcards are supported: `mcp__hass__*` allows all tools from the `hass` server.

#### Connection Lifecycle

1. **Connect**: When `ClaudeSDKClient.connect()` is called (or `query()` starts), the CLI spawns MCP server processes / connects to remote servers.
2. **Tool Discovery**: The CLI calls `listTools()` on each MCP server and registers the tools in Claude's context.
3. **Tool Execution**: During the agent loop, tool calls are dispatched to the appropriate MCP server.
4. **Disconnect**: When the client disconnects or the `query()` completes, MCP server connections are cleaned up.

#### Verifying MCP Connection

```python
from claude_agent_sdk import query, ClaudeAgentOptions, SystemMessage

async for message in query(prompt="Hello", options=options):
    if isinstance(message, SystemMessage) and message.subtype == "init":
        mcp_servers = message.data.get("mcp_servers", [])
        for server in mcp_servers:
            print(f"Server: {server.get('name')} - Status: {server.get('status')}")
            # "connected" = good, anything else = failed
```

#### Config File Alternative

Instead of inline config, you can point to a `.mcp.json` file:

```python
options = ClaudeAgentOptions(
    mcp_servers="/path/to/.mcp.json"
)
```

```json
{
  "mcpServers": {
    "hass": {
      "type": "sse",
      "url": "http://localhost:8123/mcp/sse",
      "headers": {
        "Authorization": "Bearer ${HASS_TOKEN}"
      }
    }
  }
}
```

### Step-by-Step Execution

**Can you run the agent loop step-by-step?** Partially.

With `ClaudeSDKClient`:
- You call `client.query(prompt)` to start a turn
- You iterate `client.receive_response()` to get messages
- You can `client.interrupt()` to stop mid-execution
- But you CANNOT pause between tool calls within a single turn

With hooks:
- `PreToolUse` fires before every tool call (you can block, modify, or log)
- `PostToolUse` fires after every tool call (you can log results)
- But hooks cannot "pause" the loop for external input

**There is no mechanism to intercept between steps for manual control within a single turn.**
The agent loop runs to completion (or until interrupted) for each `query()` call.

### Structured Output

```python
options = ClaudeAgentOptions(
    output_format={
        "type": "json_schema",
        "schema": {
            "type": "object",
            "properties": {
                "response_text": {"type": "string"},
                "actions_taken": {"type": "array", "items": {"type": "string"}},
                "confidence": {"type": "number"}
            },
            "required": ["response_text"]
        }
    }
)
```

---

## Part 2: Raw Anthropic API (anthropic package)

### Package Information

- **Package name**: `anthropic`
- **Installation**: `pip install anthropic`
- **Docs**: https://platform.claude.com/docs/en/api/messages-streaming

### Manual Agent Loop Pattern

This is the traditional approach - you handle the tool loop yourself:

```python
import anthropic

client = anthropic.Anthropic()

# Define tools manually
tools = [
    {
        "name": "get_entity_state",
        "description": "Get the current state of a Home Assistant entity",
        "input_schema": {
            "type": "object",
            "properties": {
                "entity_id": {
                    "type": "string",
                    "description": "The entity ID, e.g. light.living_room"
                }
            },
            "required": ["entity_id"]
        }
    },
    {
        "name": "call_service",
        "description": "Call a Home Assistant service",
        "input_schema": {
            "type": "object",
            "properties": {
                "domain": {"type": "string"},
                "service": {"type": "string"},
                "entity_id": {"type": "string"},
                "data": {"type": "object"}
            },
            "required": ["domain", "service"]
        }
    }
]

messages = [{"role": "user", "content": "Turn on the living room lights"}]

# The agent loop
while True:
    response = client.messages.create(
        model="claude-sonnet-4-5-20250929",
        max_tokens=4096,
        system="You are a Home Assistant agent...",
        tools=tools,
        messages=messages,
    )

    # Add assistant response to history
    messages.append({"role": "assistant", "content": response.content})

    # Check if done
    if response.stop_reason == "end_turn":
        # Extract final text
        for block in response.content:
            if block.type == "text":
                print(f"Final: {block.text}")
        break

    # Handle tool use
    if response.stop_reason == "tool_use":
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                # Execute the tool
                result = await execute_tool(block.name, block.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": str(result),
                })

        # Add tool results and continue
        messages.append({"role": "user", "content": tool_results})
```

### Manual Agent Loop WITH Streaming

This is the most flexible approach - full streaming control with tool dispatch:

```python
import anthropic
import json

client = anthropic.Anthropic()
messages = [{"role": "user", "content": "Turn on the living room lights and tell me the temperature"}]

async def agent_loop_streaming(messages, tools, system_prompt):
    """Agent loop with token-by-token streaming."""

    while True:
        accumulated_text = ""
        tool_calls = []
        current_tool = None
        tool_input_json = ""

        # Stream the response
        with client.messages.stream(
            model="claude-sonnet-4-5-20250929",
            max_tokens=4096,
            system=system_prompt,
            tools=tools,
            messages=messages,
        ) as stream:
            for event in stream:
                if event.type == "content_block_start":
                    if event.content_block.type == "tool_use":
                        current_tool = {
                            "id": event.content_block.id,
                            "name": event.content_block.name,
                        }
                        tool_input_json = ""
                    elif event.content_block.type == "text":
                        current_tool = None

                elif event.type == "content_block_delta":
                    if event.delta.type == "text_delta":
                        # YIELD TEXT TOKEN-BY-TOKEN HERE
                        yield {"type": "text", "text": event.delta.text}
                        accumulated_text += event.delta.text

                    elif event.delta.type == "input_json_delta":
                        tool_input_json += event.delta.partial_json

                elif event.type == "content_block_stop":
                    if current_tool:
                        current_tool["input"] = json.loads(tool_input_json)
                        tool_calls.append(current_tool)
                        yield {"type": "tool_start", "tool": current_tool["name"]}
                        current_tool = None

            # Get the final message for history
            final_message = stream.get_final_message()

        # Add assistant message to history
        messages.append({"role": "assistant", "content": final_message.content})

        # If no tool calls, we're done
        if final_message.stop_reason == "end_turn":
            yield {"type": "done", "text": accumulated_text}
            return

        # Execute tools and collect results
        tool_results = []
        for tc in tool_calls:
            result = await execute_tool(tc["name"], tc["input"])
            yield {"type": "tool_result", "tool": tc["name"], "result": str(result)}
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tc["id"],
                "content": str(result),
            })

        # Add tool results
        messages.append({"role": "user", "content": tool_results})

        # Loop continues - next LLM call will also stream
```

### Tool Runner (Beta) - Automated Agent Loop

The `anthropic` package now includes a `tool_runner` that automates the agent loop:

```python
import anthropic
from anthropic import beta_tool

client = anthropic.Anthropic()


@beta_tool
def get_entity_state(entity_id: str) -> str:
    """Get the current state of a Home Assistant entity.

    Args:
        entity_id: The entity ID, e.g. light.living_room
    """
    # Call HA API
    return json.dumps({"state": "on", "brightness": 255})


@beta_tool
def call_service(domain: str, service: str, entity_id: str = "", data: dict = None) -> str:
    """Call a Home Assistant service.

    Args:
        domain: Service domain, e.g. 'light'
        service: Service name, e.g. 'turn_on'
        entity_id: Target entity ID
        data: Additional service data
    """
    # Call HA API
    return json.dumps({"success": True})


# Automated tool loop
runner = client.beta.messages.tool_runner(
    model="claude-sonnet-4-5-20250929",
    max_tokens=4096,
    tools=[get_entity_state, call_service],
    messages=[{"role": "user", "content": "Turn on the living room lights"}],
)

# Iterate to see each step
for message in runner:
    print(message.content)

# Or get final result directly
# final = runner.until_done()
```

#### Tool Runner WITH Streaming

```python
runner = client.beta.messages.tool_runner(
    model="claude-sonnet-4-5-20250929",
    max_tokens=4096,
    tools=[get_entity_state, call_service],
    messages=[{"role": "user", "content": "Turn on the living room lights"}],
    stream=True,  # Enable streaming
)

for message_stream in runner:
    for event in message_stream:
        # Process streaming events
        if event.type == "content_block_delta" and event.delta.type == "text_delta":
            print(event.delta.text, end="", flush=True)
    final = message_stream.get_final_message()
    print(f"\nStop reason: {final.stop_reason}")
```

### MCP Connector (Beta) - Direct API with Remote MCP

The Messages API has a beta feature to connect directly to remote MCP servers without a client:

```python
import anthropic

client = anthropic.Anthropic()

response = client.beta.messages.create(
    model="claude-sonnet-4-5-20250929",
    max_tokens=1000,
    messages=[{"role": "user", "content": "Turn on the living room lights"}],
    mcp_servers=[
        {
            "type": "url",
            "url": "https://your-ha-instance.duckdns.org/mcp/sse",
            "name": "hass",
            "authorization_token": "YOUR_HA_TOKEN",
        }
    ],
    tools=[
        {
            "type": "mcp_toolset",
            "mcp_server_name": "hass",
        }
    ],
    betas=["mcp-client-2025-11-20"],
)
```

**Limitations of MCP Connector:**
- **Beta feature** - requires `betas=["mcp-client-2025-11-20"]` header
- **HTTPS only** - server must be publicly exposed through HTTPS
- **Remote only** - local stdio servers cannot be connected directly
- **Tools only** - only MCP tool calls are supported (no resources, no prompts in current beta)
- **NOT covered by Zero Data Retention** arrangements
- **Not available on Bedrock/Vertex**
- **Response types**: `mcp_tool_use` and `mcp_tool_result` content blocks (different from standard `tool_use`)
- **Single turn only** - you still need to manage the agent loop yourself for multi-step tool use

### Conversation History Management

With the raw API, you manage conversation history manually:

```python
# Conversation history is just a list of messages
conversation_history = []

def add_user_message(text: str):
    conversation_history.append({"role": "user", "content": text})

def add_assistant_message(content):
    conversation_history.append({"role": "assistant", "content": content})

def add_tool_results(results: list):
    conversation_history.append({"role": "user", "content": results})

# Each API call gets the full history
response = client.messages.create(
    model="claude-sonnet-4-5-20250929",
    max_tokens=4096,
    system="Your system prompt",
    messages=conversation_history,
    tools=tools,
)
```

---

## Part 3: Comparison for Home Assistant Use Case

### Feature Comparison

| Feature | Agent SDK | Raw API | Raw API + MCP Connector |
|---------|-----------|---------|------------------------|
| MCP server support | All transports (stdio, SSE, HTTP, in-process) | Manual dispatch only | Remote HTTPS only |
| Token streaming | Yes (include_partial_messages) | Yes (messages.stream) | Yes (with stream=True) |
| Streaming during tool use | Yes | Yes (manual) | Not applicable (single turn) |
| Multi-turn conversation | Yes (ClaudeSDKClient or session resume) | Yes (manual history) | Yes (manual history) |
| Tool loop automation | Fully automated | Manual or tool_runner beta | Manual |
| Hook/callback system | Yes (PreToolUse, PostToolUse, etc.) | None (you write the logic) | None |
| Step-by-step control | Limited (hooks only) | Full control | Full control |
| Model selection | Yes | Yes | Yes |
| System prompt | Yes | Yes | Yes |
| Process overhead | CLI subprocess | None | None |
| Local MCP servers | Yes (stdio) | Manual MCP client | No (HTTPS only) |

### Recommended Architecture for Home Assistant

#### Option A: Claude Agent SDK (Recommended)

**Pros:**
- MCP server management is automatic
- Built-in tool execution loop
- Token-by-token streaming with `include_partial_messages=True`
- Session management for multi-turn conversations
- Hooks for auditing/blocking tool calls

**Cons:**
- Runs Claude Code CLI as a subprocess (extra process)
- Less control over individual steps
- Includes many built-in tools we do not need (Bash, Write, etc.)
- Need to explicitly disable dangerous tools

```python
# Conceptual HA integration
from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions
from claude_agent_sdk.types import StreamEvent


class ClaudeConversationAgent:
    """HA Conversation Agent powered by Claude Agent SDK."""

    def __init__(self, hass_mcp_url: str, hass_token: str, api_key: str):
        self.options = ClaudeAgentOptions(
            system_prompt="You are a Home Assistant voice agent...",
            model="claude-sonnet-4-5-20250929",
            mcp_servers={
                "hass": {
                    "type": "sse",
                    "url": hass_mcp_url,
                    "headers": {"Authorization": f"Bearer {hass_token}"}
                }
            },
            allowed_tools=["mcp__hass__*"],
            disallowed_tools=["Bash", "Write", "Edit", "Read", "Glob", "Grep"],
            permission_mode="bypassPermissions",
            include_partial_messages=True,
            max_turns=5,
            env={"ANTHROPIC_API_KEY": api_key},
        )
        self._clients: dict[str, ClaudeSDKClient] = {}

    async def process_message(self, conversation_id: str, user_input: str):
        """Process a user message, yielding streaming text chunks."""
        client = self._clients.get(conversation_id)
        if not client:
            client = ClaudeSDKClient(self.options)
            await client.connect()
            self._clients[conversation_id] = client

        await client.query(user_input)

        async for message in client.receive_response():
            if isinstance(message, StreamEvent):
                event = message.event
                if event.get("type") == "content_block_delta":
                    delta = event.get("delta", {})
                    if delta.get("type") == "text_delta":
                        yield delta.get("text", "")
```

#### Option B: Raw Anthropic API with Manual MCP

**Pros:**
- Maximum control over every step
- No subprocess overhead
- Can custom-build the exact streaming behavior needed
- Can intercept between any step
- Simpler dependency (just `anthropic` package)

**Cons:**
- Must implement MCP client yourself (using `mcp` Python package)
- Must implement the agent loop yourself
- Must manage tool definitions and dispatch
- More code to write and maintain

```python
# Conceptual HA integration
import anthropic
from mcp import ClientSession
from mcp.client.sse import sse_client


class ClaudeConversationAgent:
    """HA Conversation Agent powered by raw Anthropic API + MCP."""

    def __init__(self, model: str, system_prompt: str):
        self.client = anthropic.AsyncAnthropic()
        self.model = model
        self.system_prompt = system_prompt
        self.mcp_session: ClientSession | None = None
        self.tools = []
        self.conversations: dict[str, list] = {}

    async def connect_mcp(self, url: str, headers: dict):
        """Connect to HA MCP server and discover tools."""
        read_stream, write_stream = await sse_client(url, headers=headers)
        self.mcp_session = ClientSession(read_stream, write_stream)
        await self.mcp_session.initialize()

        # Discover tools
        tools_result = await self.mcp_session.list_tools()
        self.tools = [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.inputSchema,
            }
            for t in tools_result.tools
        ]

    async def execute_tool(self, name: str, arguments: dict) -> str:
        """Execute an MCP tool call."""
        result = await self.mcp_session.call_tool(name, arguments)
        return str(result.content)

    async def process_message(self, conversation_id: str, user_input: str):
        """Process user message with streaming."""
        if conversation_id not in self.conversations:
            self.conversations[conversation_id] = []

        messages = self.conversations[conversation_id]
        messages.append({"role": "user", "content": user_input})

        while True:
            accumulated_text = ""
            tool_calls = []

            async with self.client.messages.stream(
                model=self.model,
                max_tokens=4096,
                system=self.system_prompt,
                tools=self.tools,
                messages=messages,
            ) as stream:
                async for event in stream:
                    # ... handle streaming events, yield text chunks
                    pass

                final = await stream.get_final_message()

            messages.append({"role": "assistant", "content": final.content})

            if final.stop_reason == "end_turn":
                yield {"type": "done", "text": accumulated_text}
                return

            if final.stop_reason == "tool_use":
                tool_results = []
                for block in final.content:
                    if block.type == "tool_use":
                        result = await self.execute_tool(block.name, block.input)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
                        })
                messages.append({"role": "user", "content": tool_results})
```

#### Option C: Hybrid Approach

Use the **raw Anthropic API** for the LLM calls (full streaming control) but use the **`mcp` Python package** for MCP server connections (no need to implement the protocol yourself).

This gives you:
- Full control over the agent loop and streaming
- No subprocess overhead
- Proper MCP protocol handling for tool discovery and execution
- The ability to intercept between any step

---

## Part 4: Key Answers to Research Questions

### 1. Can the agent loop yield intermediate text/status while tool calls are in progress?

**Agent SDK**: YES. With `include_partial_messages=True`, you get `StreamEvent` messages with text deltas as Claude generates them, including text that appears before tool calls (e.g., "Let me check the lights for you..."). Between tool call rounds, you also get Claude's intermediate text. However, during actual tool execution (the time the MCP server is processing), no text is generated because Claude is waiting for the result.

**Raw API**: YES, and with more control. You get the same streaming events and can add your own status messages (e.g., "Calling the lights service...") during tool execution since you control the loop.

### 2. Is there a streaming mode?

**Agent SDK**: Yes. Set `include_partial_messages=True` in `ClaudeAgentOptions`. Stream events include `content_block_delta` with `text_delta` for token-by-token text.

**Raw API**: Yes. Use `client.messages.stream()` instead of `client.messages.create()`. The `text_stream` property gives you a simple iterator of text chunks.

### 3. Can you run the agent loop step-by-step?

**Agent SDK**: No, not truly step-by-step. The agent loop runs autonomously. You can hook into tool calls via `PreToolUse`/`PostToolUse` hooks, and you can interrupt with `client.interrupt()`, but you cannot pause between steps.

**Raw API**: YES, fully. Since you write the loop, you can pause, inspect, modify, or redirect at any point between tool calls.

### 4. Can you intercept between steps?

**Agent SDK**: Partially, via hooks. `PreToolUse` can block or modify tool calls. `PostToolUse` can log results. But you cannot inject new messages or redirect the conversation mid-loop.

**Raw API**: Fully. Between each tool call and the next LLM call, you have complete control. You can modify messages, add context, skip tool calls, etc.

### 5. How does the agent discover MCP tools?

**Agent SDK**: Automatically. When the CLI connects to an MCP server, it calls `listTools()` and registers them with Claude. You just need to add the server to `mcp_servers` and allow the tools via `allowed_tools`.

**Raw API with MCP Connector**: The API calls `list_tools` on the remote server and handles tool execution server-side.

**Raw API with manual MCP**: You call `session.list_tools()` yourself and convert the results to Claude tool definitions.

### 6. What's the best streaming pattern for Home Assistant?

For HA, we need to stream the final text response to the user while keeping tool calls silent (or showing brief status messages). The ideal pattern:

```
User: "What's the temperature in the house?"

[Stream to user]: "Let me check that for you."
[Silent tool call]: get_entity_state(sensor.indoor_temperature)
[Stream to user]: "The temperature in the house is currently 72 degrees Fahrenheit."
```

Both approaches support this. The Agent SDK does it with `include_partial_messages=True` and filtering `StreamEvent` for text deltas. The raw API does it with `messages.stream()` and the same filtering logic.

### 7. Process overhead considerations

**Agent SDK**: Spawns a Node.js CLI subprocess for each `ClaudeSDKClient` instance. This is significant overhead for a Home Assistant addon. However, one `ClaudeSDKClient` can handle many conversations via session management.

**Raw API**: No subprocess. Just HTTP calls to the Anthropic API. Lighter weight.

---

## Part 5: Message Types Reference

### Agent SDK Message Types

```python
from claude_agent_sdk import (
    # Message types
    UserMessage,          # User input
    AssistantMessage,     # Claude's response (complete)
    SystemMessage,        # System messages (init, etc.)
    ResultMessage,        # Final result with cost/usage info
)
from claude_agent_sdk.types import (
    StreamEvent,          # Partial streaming event (when include_partial_messages=True)
)

# Content block types (inside AssistantMessage.content)
from claude_agent_sdk import (
    TextBlock,            # text content
    ThinkingBlock,        # extended thinking content
    ToolUseBlock,         # tool call request (id, name, input)
    ToolResultBlock,      # tool execution result
)
```

### ResultMessage Fields

```python
@dataclass
class ResultMessage:
    subtype: str              # "success" or "error_during_execution"
    duration_ms: int          # Total duration
    duration_api_ms: int      # API call duration
    is_error: bool
    num_turns: int
    session_id: str
    total_cost_usd: float | None
    usage: dict | None        # Token usage
    result: str | None        # Final text result
    structured_output: Any    # If output_format was specified
```

---

## Part 6: Dependencies and Considerations

### Claude Agent SDK Dependencies
- `claude-agent-sdk` (includes bundled Claude Code CLI - Node.js binary)
- Requires Node.js runtime for the CLI subprocess
- Python >= 3.10

### Raw Anthropic API Dependencies
- `anthropic` (the official Python client)
- `mcp` (if using MCP servers - the official MCP Python SDK)
- Python >= 3.8

### Home Assistant Specific Considerations

1. **HA already has an MCP server**: Home Assistant 2024.12+ includes a built-in MCP server that exposes entities, services, and automations via the MCP protocol. This means we do not need to implement any HA-specific tools - we just connect to the MCP server.

2. **Connection type**: HA's MCP server supports SSE transport (`http://localhost:8123/mcp/sse`). Both the Agent SDK and raw API can connect to this.

3. **Authentication**: HA MCP requires a Long-Lived Access Token in the Authorization header.

4. **Conversation agent integration**: HA conversation agents receive text input and return text output. Streaming is supported via async generators in newer HA versions.

5. **Resource constraints**: HA runs on various hardware (Raspberry Pi to full servers). The Agent SDK's CLI subprocess adds overhead. The raw API approach is lighter.

6. **Latency**: For voice assistants, latency matters. Streaming the first text chunk as soon as possible is important. Both approaches support this.
