# MCP Connectivity Patterns for Home Assistant Integration

## Table of Contents

1. [MCP Protocol Overview](#1-mcp-protocol-overview)
2. [Transport Mechanisms](#2-transport-mechanisms)
3. [Python MCP SDK Client API](#3-python-mcp-sdk-client-api)
4. [Home Assistant's MCP Server Component](#4-home-assistants-mcp-server-component)
5. [Home Assistant's MCP Client Component](#5-home-assistants-mcp-client-component)
6. [Claude Agent SDK MCP Integration](#6-claude-agent-sdk-mcp-integration)
7. [Connection Lifecycle Patterns](#7-connection-lifecycle-patterns)
8. [Recommended Architecture for Our Integration](#8-recommended-architecture-for-our-integration)
9. [Python Package Dependencies](#9-python-package-dependencies)

---

## 1. MCP Protocol Overview

The **Model Context Protocol (MCP)** is an open standard (Protocol Revision: 2025-06-18) for connecting AI agents to external tools and data sources. It uses **JSON-RPC** over various transports to enable bidirectional communication between clients and servers.

### Core Concepts

- **Client**: Initiates connections, discovers tools, calls tools on servers
- **Server**: Exposes tools, resources, and prompts to clients
- **Session**: A logically related set of interactions, beginning with initialization
- **Tool Discovery**: Clients call `list_tools()` to enumerate available server capabilities
- **Tool Execution**: Clients call `call_tool(name, arguments)` to invoke server-side operations

### Message Flow

```
Client                          Server
  |                               |
  |--- InitializeRequest -------->|
  |<-- InitializeResponse --------|
  |--- InitializedNotification -->|
  |                               |
  |--- tools/list --------------->|
  |<-- tools/list response -------|
  |                               |
  |--- tools/call --------------->|
  |<-- tools/call response -------|
```

---

## 2. Transport Mechanisms

MCP defines two standard transports. The choice of transport has significant implications for our integration.

### 2.1 stdio Transport

The client launches the MCP server as a **subprocess** and communicates via stdin/stdout.

**How it works:**
- Client spawns a child process
- JSON-RPC messages are written to the process's stdin
- Responses are read from the process's stdout
- Messages are newline-delimited
- stderr is available for logging

**Tradeoffs:**
- **Pros**: Simple, no network stack, good for local tools, low latency
- **Cons**: Requires process management, one client per server process, not suitable for remote servers, subprocess lifecycle must be managed within HA's event loop

**Use case**: Local tool servers (filesystem, database, etc.)

### 2.2 Streamable HTTP Transport (Current Standard)

Replaces the older HTTP+SSE transport from protocol version 2024-11-05.

**How it works:**
- Server exposes a single HTTP endpoint (e.g., `https://example.com/mcp`)
- Client sends JSON-RPC messages via HTTP POST
- Server responds with either `application/json` (single response) or `text/event-stream` (SSE stream)
- Client can open a GET-based SSE stream for server-initiated messages
- Session management via `Mcp-Session-Id` header

**Tradeoffs:**
- **Pros**: Works over network, supports multiple clients, stateless option available, supports streaming, session resumability
- **Cons**: More complex, HTTP overhead per request, requires proper auth handling

**Use case**: Remote servers, cloud-hosted tools, Home Assistant's own MCP server endpoint

### 2.3 Legacy SSE Transport (Deprecated)

The older SSE transport used two endpoints: one for establishing an SSE stream, another for sending messages. Still supported for backwards compatibility.

**How it works:**
- Client GETs an SSE endpoint to establish a stream
- Server sends an `endpoint` event with a URL for POSTing messages
- Client POSTs JSON-RPC messages to that URL
- Server pushes responses via the SSE stream

**Backwards Compatibility Strategy (from MCP spec):**
1. Try POST to server URL (Streamable HTTP)
2. If 405/404, fall back to GET for SSE stream establishment
3. This is exactly what Home Assistant's MCP client does (see Section 5)

### 2.4 Transport Comparison for HA Integration

| Factor | stdio | Streamable HTTP | Legacy SSE |
|--------|-------|----------------|------------|
| Remote servers | No | Yes | Yes |
| Process management | Required | Not needed | Not needed |
| HA event loop compat | Needs care | Native async | Native async |
| Multiple clients | No | Yes | Yes |
| Session persistence | Process lifetime | Session ID header | SSE connection |
| Auth support | N/A | Headers, OAuth | Headers |
| Recommended | For local tools | Primary choice | Fallback only |

---

## 3. Python MCP SDK Client API

### Package: `mcp` (PyPI)

- **Latest version**: 1.26.0 (January 2026)
- **Python support**: 3.10, 3.11, 3.12, 3.13
- **License**: MIT
- **Maintainer**: Anthropic (David Soria Parra)

### 3.1 Core Client Classes

```python
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client
```

### 3.2 ClientSession API

`ClientSession` is the primary interface for interacting with MCP servers. Key methods:

| Method | Description |
|--------|-------------|
| `initialize()` | Perform protocol handshake |
| `list_tools()` | Discover available tools |
| `call_tool(name, arguments)` | Execute a tool |
| `list_resources()` | List available resources |
| `read_resource(uri)` | Read a resource |
| `list_prompts()` | List available prompts |
| `get_prompt(name, arguments)` | Get a prompt template |

### 3.3 Connecting via stdio

```python
import asyncio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

server_params = StdioServerParameters(
    command="python",
    args=["my_server.py"],
    env={"SOME_VAR": "value"},
)

async def main():
    async with stdio_client(server_params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

            # Discover tools
            result = await session.list_tools()
            for tool in result.tools:
                print(f"Tool: {tool.name} - {tool.description}")
                print(f"  Schema: {tool.inputSchema}")

            # Call a tool
            result = await session.call_tool("my_tool", {"arg1": "value1"})
            print(result.content)

asyncio.run(main())
```

### 3.4 Connecting via Streamable HTTP

```python
import asyncio
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

async def main():
    async with streamable_http_client(
        url="https://example.com/api/mcp",
        headers={"Authorization": "Bearer my-token"},
    ) as (read_stream, write_stream, _session_info):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

            result = await session.list_tools()
            for tool in result.tools:
                print(f"Tool: {tool.name}")

asyncio.run(main())
```

### 3.5 Connecting via SSE (Legacy)

```python
import asyncio
from mcp import ClientSession
from mcp.client.sse import sse_client

async def main():
    async with sse_client(
        url="https://example.com/mcp/sse",
        headers={"Authorization": "Bearer my-token"},
    ) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            result = await session.list_tools()

asyncio.run(main())
```

### 3.6 Persistent Session with AsyncExitStack

For maintaining a connection across multiple tool calls (important for our integration):

```python
import asyncio
from typing import Optional
from contextlib import AsyncExitStack
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

class PersistentMCPClient:
    """Maintains a persistent MCP session across multiple tool calls."""

    def __init__(self, url: str, headers: dict[str, str] | None = None):
        self.url = url
        self.headers = headers or {}
        self.session: Optional[ClientSession] = None
        self._exit_stack = AsyncExitStack()

    async def connect(self) -> None:
        """Establish and persist the MCP connection."""
        read_stream, write_stream, _ = await self._exit_stack.enter_async_context(
            streamable_http_client(url=self.url, headers=self.headers)
        )
        self.session = await self._exit_stack.enter_async_context(
            ClientSession(read_stream, write_stream)
        )
        await self.session.initialize()

    async def list_tools(self):
        """List available tools from the connected server."""
        if not self.session:
            raise RuntimeError("Not connected")
        return await self.session.list_tools()

    async def call_tool(self, name: str, arguments: dict):
        """Call a tool on the connected server."""
        if not self.session:
            raise RuntimeError("Not connected")
        return await self.session.call_tool(name, arguments)

    async def disconnect(self) -> None:
        """Clean up all resources."""
        await self._exit_stack.aclose()
        self.session = None
```

---

## 4. Home Assistant's MCP Server Component

Source: `homeassistant/components/mcp_server/`

### 4.1 Architecture

HA exposes itself as an MCP server so that external AI agents (like Claude Desktop) can discover and call HA tools (control lights, query states, etc.).

### 4.2 Endpoints Exposed

| Endpoint | Transport | Purpose |
|----------|-----------|---------|
| `/api/mcp` | Streamable HTTP (POST) | Primary endpoint, stateless per-request |
| `/mcp_server/sse` | Legacy SSE (GET) | Establishes SSE session |
| `/mcp_server/messages/{session_id}` | Legacy SSE (POST) | Sends messages to SSE session |

### 4.3 Streamable HTTP Implementation (`/api/mcp`)

From `homeassistant/components/mcp_server/http.py`:

```python
class ModelContextProtocolStreamableView(HomeAssistantView):
    """Model Context Protocol Streamable HTTP endpoint."""
    url = "/api/mcp"

    async def post(self, request: web.Request) -> web.StreamResponse:
        """Process JSON-RPC messages for the Model Context Protocol."""
        # Validates Content-Type: application/json
        # Validates Accept header includes application/json
        # Parses JSON-RPC message
        # For notifications/responses: returns 202 Accepted
        # For requests: spins up MCP server, sends request, waits for response
        # Returns JSON response with 60-second timeout
        # Uses stateless=True mode (no session persistence)
```

Key implementation detail: **Each request creates a fresh MCP server instance**. The server runs as a background task for the duration of the request, using in-memory stream pairs as bridges between HTTP and the MCP SDK.

### 4.4 SSE Implementation (`/mcp_server/sse`)

The SSE endpoint maintains a long-lived connection:

```python
class ModelContextProtocolSSEView(HomeAssistantView):
    url = "/mcp_server/sse"

    async def get(self, request: web.Request) -> web.StreamResponse:
        # Creates MCP server and stream pairs
        # Establishes SSE response
        # Creates session with SessionManager
        # Sends endpoint URL to client
        # Runs sse_reader task to forward server messages to client
        # Server runs for lifetime of SSE connection
```

### 4.5 Session Management

```python
# In __init__.py:
async def async_setup_entry(hass, entry):
    entry.runtime_data = SessionManager()  # Manages SSE sessions

async def async_unload_entry(hass, entry):
    session_manager = entry.runtime_data
    await session_manager.close()  # Cleans up all sessions
```

### 4.6 Key Takeaway for Our Integration

When connecting TO Home Assistant's MCP server as a client, we should:
- Use the `/api/mcp` endpoint (Streamable HTTP)
- Include proper authentication headers (Long-Lived Access Token)
- Expect stateless responses (no session ID needed for HA's implementation)
- The server exposes HA's LLM API tools (entity control, state queries, etc.)

---

## 5. Home Assistant's MCP Client Component

Source: `homeassistant/components/mcp/`

This is the most directly relevant reference for our integration. HA already has a built-in MCP client that connects to external MCP servers.

### 5.1 Architecture Overview

```
                     ┌─────────────────────────────┐
                     │  HA Config Entry (per server)│
                     │  - URL                       │
                     │  - OAuth tokens (optional)   │
                     └──────────┬──────────────────┘
                                │
                     ┌──────────▼──────────────────┐
                     │ ModelContextProtocolCoordinator│
                     │ (DataUpdateCoordinator)      │
                     │ - Refreshes every 30 minutes │
                     │ - Discovers tools via list_tools│
                     └──────────┬──────────────────┘
                                │
                     ┌──────────▼──────────────────┐
                     │ ModelContextProtocolAPI       │
                     │ (llm.API)                    │
                     │ - Registered with HA LLM system│
                     │ - Exposes tools to conversation│
                     └──────────┬──────────────────┘
                                │
                     ┌──────────▼──────────────────┐
                     │ ModelContextProtocolTool      │
                     │ (llm.Tool)                   │
                     │ - Per-tool, opens NEW session │
                     │   for each call_tool invocation│
                     └─────────────────────────────┘
```

### 5.2 Connection Pattern: Connect-Per-Operation

The critical pattern HA uses is **connect-per-operation** -- a new MCP session is created for EACH operation (tool discovery or tool call), not maintained persistently.

From `coordinator.py`:

```python
@asynccontextmanager
async def mcp_client(
    hass: HomeAssistant,
    url: str,
    token_manager: TokenManager | None = None,
) -> AsyncGenerator[ClientSession]:
    """Create an MCP client."""
    headers: dict[str, str] = {}
    if token_manager is not None:
        token = await token_manager()
        headers["Authorization"] = f"Bearer {token}"

    try:
        # Try Streamable HTTP first
        async with (
            streamable_http_client(
                url=url,
                http_client=create_async_httpx_client(hass, headers=headers),
            ) as (read_stream, write_stream, _),
            ClientSession(read_stream, write_stream) as session,
        ):
            await session.initialize()
            yield session
    except ExceptionGroup as streamable_err:
        main_error = streamable_err.exceptions[0]
        if (
            isinstance(main_error, httpx.HTTPStatusError)
            and main_error.response.status_code == 405
        ) or isinstance(main_error, McpError):
            # Fall back to SSE transport
            try:
                async with (
                    sse_client(url=url, headers=headers) as streams,
                    ClientSession(*streams) as session,
                ):
                    await session.initialize()
                    yield session
            except ExceptionGroup as sse_err:
                raise sse_err.exceptions[0] from sse_err
        else:
            raise main_error from streamable_err
```

### 5.3 Transport Fallback Strategy

HA implements the exact fallback pattern recommended by the MCP specification:

1. **Try Streamable HTTP** first (`streamable_http_client`)
2. **If 405 or McpError**, fall back to **SSE** (`sse_client`)
3. This provides backwards compatibility with older MCP servers

### 5.4 Tool Discovery (Every 30 Minutes)

```python
class ModelContextProtocolCoordinator(DataUpdateCoordinator[list[llm.Tool]]):
    """Coordinator that periodically refreshes tool list."""

    def __init__(self, hass, config_entry, token_manager=None):
        super().__init__(
            hass,
            logger=_LOGGER,
            name=DOMAIN,
            config_entry=config_entry,
            update_interval=datetime.timedelta(minutes=30),  # Refresh interval
        )
        self.token_manager = token_manager

    async def _async_update_data(self) -> list[llm.Tool]:
        try:
            async with asyncio.timeout(10):  # 10-second timeout
                async with mcp_client(
                    self.hass, self.config_entry.data[CONF_URL], self.token_manager
                ) as session:
                    result = await session.list_tools()
        except TimeoutError as error:
            raise UpdateFailed(f"Timeout when listing tools: {error}") from error
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 401 and self.token_manager:
                raise ConfigEntryAuthFailed(...) from error
            raise UpdateFailed(...) from error

        tools = []
        for tool in result.tools:
            parameters = convert_to_voluptuous(tool.inputSchema)
            tools.append(ModelContextProtocolTool(
                tool.name, tool.description, parameters,
                self.config_entry.data[CONF_URL], self.token_manager,
            ))
        return tools
```

### 5.5 Tool Execution (New Connection Per Call)

```python
class ModelContextProtocolTool(llm.Tool):
    async def async_call(self, hass, tool_input, llm_context):
        try:
            async with asyncio.timeout(10):  # 10-second timeout
                async with mcp_client(
                    hass, self.server_url, self.token_manager
                ) as session:
                    result = await session.call_tool(
                        tool_input.tool_name, tool_input.tool_args
                    )
        except TimeoutError:
            raise HomeAssistantError("Timeout when calling tool")
        except httpx.HTTPStatusError:
            raise HomeAssistantError("Error when calling tool")
        return result.model_dump(exclude_unset=True, exclude_none=True)
```

### 5.6 OAuth/Authentication

HA's MCP client supports OAuth2 authentication:

```python
async def _create_token_manager(hass, entry):
    """Create an OAuth token manager if the server requires auth."""
    implementation = await async_get_config_entry_implementation(hass, entry)
    if not implementation:
        return None

    session = OAuth2Session(hass, entry, implementation)

    async def token_manager() -> str:
        await session.async_ensure_token_valid()
        return session.token["access_token"]

    return token_manager
```

### 5.7 Key Takeaways from HA's MCP Client

1. **Connect-per-operation**: No persistent connections. Each `list_tools()` or `call_tool()` opens a fresh connection.
2. **Transport fallback**: Streamable HTTP first, SSE as fallback.
3. **Uses HA's httpx client**: `create_async_httpx_client(hass, headers=headers)` for proper HA integration.
4. **DataUpdateCoordinator**: Tool discovery is periodic (30 min) with HA's standard coordinator pattern.
5. **10-second timeout**: Both discovery and tool calls have a 10-second timeout.
6. **Error handling**: Specific handling for 401 (auth failed), timeouts, and general HTTP errors.
7. **Schema conversion**: Tool input schemas are converted from JSON Schema to Voluptuous for HA validation.

---

## 6. Claude Agent SDK MCP Integration

### Package: `claude-agent-sdk` (PyPI)

- **Latest version**: 0.1.37 (February 2026)
- **Python support**: 3.10+
- **License**: MIT
- **Key dependency**: Bundles the Claude Code CLI internally

### 6.1 How the Agent SDK Manages MCP Connections

The Claude Agent SDK handles MCP connections **internally**. You do NOT pass pre-connected sessions. Instead, you provide server configuration and the SDK manages the full lifecycle:

```python
from claude_agent_sdk import query, ClaudeAgentOptions

options = ClaudeAgentOptions(
    mcp_servers={
        "my-server": {
            "type": "http",
            "url": "https://example.com/api/mcp",
            "headers": {"Authorization": "Bearer token123"},
        }
    },
    allowed_tools=["mcp__my-server__*"],
)

async for message in query(prompt="Use the server tools", options=options):
    print(message)
```

### 6.2 MCP Server Configuration Types

The SDK supports four server types:

#### stdio Server

```python
{
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-github"],
    "env": {"GITHUB_TOKEN": "ghp_xxx"},
}
# "type" field is optional for stdio (backwards compatible default)
```

#### HTTP Server (Streamable HTTP)

```python
{
    "type": "http",
    "url": "https://api.example.com/mcp",
    "headers": {"Authorization": "Bearer token"},
}
```

#### SSE Server

```python
{
    "type": "sse",
    "url": "https://api.example.com/mcp/sse",
    "headers": {"Authorization": "Bearer token"},
}
```

#### In-Process SDK Server

```python
from claude_agent_sdk import tool, create_sdk_mcp_server

@tool("my_tool", "Does something", {"input": str})
async def my_tool(args):
    return {"content": [{"type": "text", "text": f"Result: {args['input']}"}]}

server = create_sdk_mcp_server(name="my-server", tools=[my_tool])
# Config:
{
    "type": "sdk",
    "name": "my-server",
    "instance": server,  # In-process, no subprocess needed
}
```

### 6.3 Tool Naming Convention

MCP tools in the Agent SDK follow the pattern: `mcp__<server-name>__<tool-name>`

Example: Server named `"github"` with tool `list_issues` becomes `mcp__github__list_issues`.

### 6.4 Connecting to HA's MCP Server from Agent SDK

To connect the Claude Agent SDK to Home Assistant's MCP server:

```python
from claude_agent_sdk import query, ClaudeAgentOptions

options = ClaudeAgentOptions(
    mcp_servers={
        "home-assistant": {
            "type": "http",
            "url": "http://homeassistant.local:8123/api/mcp",
            "headers": {
                "Authorization": "Bearer YOUR_LONG_LIVED_ACCESS_TOKEN"
            },
        }
    },
    allowed_tools=["mcp__home-assistant__*"],
)

async for message in query(prompt="Turn off the living room lights", options=options):
    print(message)
```

### 6.5 query() vs ClaudeSDKClient

| Feature | `query()` | `ClaudeSDKClient` |
|---------|-----------|-------------------|
| Session | New session each time | Reuses same session |
| Conversation | Single exchange | Multiple exchanges in context |
| Connection | Managed automatically | Manual control |
| MCP Servers | Configured per call | Configured at init, persists |
| Custom Tools | Not supported | Supported |
| Hooks | Not supported | Supported |
| Use Case | One-off tasks | Continuous conversations |

For our HA conversation agent, `ClaudeSDKClient` is the better choice because:
- We need conversation continuity across turns
- We need hooks for permission control
- We may need custom tools for HA-specific operations

### 6.6 Error Handling

```python
async for message in query(prompt="...", options=options):
    if isinstance(message, SystemMessage) and message.subtype == "init":
        # Check MCP server connection status
        for server in message.data.get("mcp_servers", []):
            if server.get("status") != "connected":
                print(f"Failed: {server['name']}: {server.get('error')}")
```

### 6.7 In-Process SDK Server (Custom Tools)

For HA-specific tools that don't need an external MCP server:

```python
from claude_agent_sdk import tool, create_sdk_mcp_server

@tool("get_entity_state", "Get the state of a HA entity", {"entity_id": str})
async def get_entity_state(args):
    # This runs in-process, can directly access HA
    state = hass.states.get(args["entity_id"])
    return {
        "content": [{
            "type": "text",
            "text": f"{state.entity_id}: {state.state}"
        }]
    }

ha_tools_server = create_sdk_mcp_server(
    name="ha-tools",
    tools=[get_entity_state],
)

options = ClaudeAgentOptions(
    mcp_servers={
        "ha-tools": ha_tools_server,       # In-process custom tools
        "ha-mcp": {                         # HA's built-in MCP server
            "type": "http",
            "url": "http://localhost:8123/api/mcp",
            "headers": {"Authorization": "Bearer TOKEN"},
        },
    },
    allowed_tools=["mcp__ha-tools__*", "mcp__ha-mcp__*"],
)
```

---

## 7. Connection Lifecycle Patterns

### 7.1 Should Connections Persist Across Conversation Turns?

**Analysis of approaches:**

#### Option A: Connect-Per-Operation (HA's Current Approach)

```python
# Each tool call creates a new connection
async def call_tool(self, name, args):
    async with mcp_client(hass, url, token_manager) as session:
        return await session.call_tool(name, args)
```

**Pros:**
- Simple, no state management
- Naturally handles server restarts
- No connection leak risk
- HA's proven pattern

**Cons:**
- Connection overhead per call (HTTP handshake, MCP initialization)
- Not suitable for stdio servers (process startup cost)
- Latency per call

#### Option B: Persistent Session (AsyncExitStack Pattern)

```python
class MCPConnectionManager:
    def __init__(self):
        self._exit_stack = AsyncExitStack()
        self._session: ClientSession | None = None

    async def connect(self, url, headers):
        read, write, _ = await self._exit_stack.enter_async_context(
            streamable_http_client(url=url, headers=headers)
        )
        self._session = await self._exit_stack.enter_async_context(
            ClientSession(read, write)
        )
        await self._session.initialize()

    async def disconnect(self):
        await self._exit_stack.aclose()
        self._session = None
```

**Pros:**
- Lower latency for repeated calls
- Required for stdio servers
- Better for stateful MCP servers

**Cons:**
- Must handle reconnection on failure
- Connection leak risk if not properly cleaned up
- More complex lifecycle management

#### Option C: Hybrid (Recommended for Our Integration)

For our conversation agent:
- **Claude Agent SDK connections**: Managed by the SDK itself (no choice needed)
- **Direct MCP client connections**: Use connect-per-operation following HA's pattern
- **stdio servers**: Use persistent connections if needed

### 7.2 Reconnection Strategy

```python
import asyncio
from contextlib import asynccontextmanager
from mcp import ClientSession, McpError
from mcp.client.streamable_http import streamable_http_client
import httpx

MAX_RETRIES = 3
RETRY_DELAY = 1.0  # seconds

@asynccontextmanager
async def resilient_mcp_client(url: str, headers: dict | None = None):
    """MCP client with retry logic."""
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            async with (
                streamable_http_client(url=url, headers=headers or {})
                    as (read, write, _),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                yield session
                return
        except (httpx.HTTPError, McpError, TimeoutError) as e:
            last_error = e
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(RETRY_DELAY * (attempt + 1))
    raise last_error
```

### 7.3 stdio Server Process Management in HA

If we need stdio MCP servers within HA:

```python
import asyncio
from contextlib import AsyncExitStack
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

class StdioMCPManager:
    """Manages a stdio MCP server subprocess within HA."""

    def __init__(self, hass, command: str, args: list[str]):
        self.hass = hass
        self.params = StdioServerParameters(command=command, args=args)
        self._exit_stack = AsyncExitStack()
        self.session: ClientSession | None = None

    async def start(self):
        """Start the subprocess and establish MCP session."""
        read, write = await self._exit_stack.enter_async_context(
            stdio_client(self.params)
        )
        self.session = await self._exit_stack.enter_async_context(
            ClientSession(read, write)
        )
        await self.session.initialize()

    async def stop(self):
        """Stop the subprocess and clean up."""
        await self._exit_stack.aclose()
        self.session = None

    async def restart(self):
        """Restart after a failure."""
        await self.stop()
        await self.start()
```

**Important**: stdio processes must be managed carefully within HA's event loop. The `stdio_client` context manager handles subprocess spawning and cleanup, but we must ensure proper shutdown during HA restart/reload.

### 7.4 HTTP Session Management & Authentication

For HTTP-based MCP servers requiring authentication:

```python
class AuthenticatedMCPClient:
    """MCP client with token refresh support."""

    def __init__(self, url: str, token_provider):
        self.url = url
        self.token_provider = token_provider  # Callable that returns fresh token

    @asynccontextmanager
    async def session(self):
        """Get an authenticated MCP session."""
        token = await self.token_provider()
        headers = {"Authorization": f"Bearer {token}"}

        async with (
            streamable_http_client(url=self.url, headers=headers)
                as (read, write, _),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            yield session
```

---

## 8. Recommended Architecture for Our Integration

### 8.1 Overview

Our HA conversation agent needs to:
1. Connect to the Claude Agent SDK (which manages its own MCP connections)
2. Optionally connect to additional MCP servers directly
3. Expose HA's capabilities to the LLM

### 8.2 Primary Pattern: Agent SDK Manages MCP

The simplest and most robust approach is to let the Claude Agent SDK manage all MCP connections:

```python
from claude_agent_sdk import (
    ClaudeSDKClient,
    ClaudeAgentOptions,
    tool,
    create_sdk_mcp_server,
)

class ClaudeHAConversationAgent:
    """HA Conversation agent using Claude Agent SDK with MCP."""

    def __init__(self, hass, config_entry):
        self.hass = hass
        self.config_entry = config_entry

    def _build_options(self) -> ClaudeAgentOptions:
        """Build agent options with MCP server configs."""
        mcp_servers = {}

        # Connect to HA's own MCP server for entity control
        ha_url = self.config_entry.data.get("ha_mcp_url")
        ha_token = self.config_entry.data.get("ha_token")
        if ha_url:
            mcp_servers["home-assistant"] = {
                "type": "http",
                "url": ha_url,
                "headers": {"Authorization": f"Bearer {ha_token}"},
            }

        # Add any user-configured external MCP servers
        for name, server_config in self.config_entry.data.get("mcp_servers", {}).items():
            mcp_servers[name] = server_config

        # Build allowed tools list
        allowed_tools = []
        for name in mcp_servers:
            allowed_tools.append(f"mcp__{name}__*")

        return ClaudeAgentOptions(
            mcp_servers=mcp_servers,
            allowed_tools=allowed_tools,
            permission_mode="bypassPermissions",  # Or custom can_use_tool handler
        )

    async def async_process(self, user_input):
        """Process a conversation turn."""
        options = self._build_options()

        # Option A: One-shot query
        from claude_agent_sdk import query, ResultMessage
        async for message in query(prompt=user_input.text, options=options):
            if isinstance(message, ResultMessage) and message.subtype == "success":
                return message.result

        # Option B: Persistent client for multi-turn conversations
        # (See ClaudeSDKClient usage in Section 6.5)
```

### 8.3 Multi-Turn Conversation with ClaudeSDKClient

```python
class ConversationSession:
    """Manages a multi-turn conversation session."""

    def __init__(self, options: ClaudeAgentOptions):
        self.client = ClaudeSDKClient(options)
        self._connected = False

    async def start(self):
        await self.client.connect()
        self._connected = True

    async def send_message(self, text: str) -> str:
        """Send a message and get the response."""
        if not self._connected:
            await self.start()

        await self.client.query(text)

        result_text = ""
        async for message in self.client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        result_text += block.text
            if isinstance(message, ResultMessage):
                break

        return result_text

    async def end(self):
        if self._connected:
            await self.client.disconnect()
            self._connected = False
```

### 8.4 Direct MCP Client (If Needed Alongside Agent SDK)

If we need to call MCP tools directly (outside the Agent SDK flow), follow HA's pattern:

```python
from homeassistant.helpers.httpx_client import create_async_httpx_client

@asynccontextmanager
async def ha_mcp_client(hass, url, token=None):
    """Create an MCP client using HA's httpx client."""
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        async with (
            streamable_http_client(
                url=url,
                http_client=create_async_httpx_client(hass, headers=headers),
            ) as (read, write, _),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            yield session
    except ExceptionGroup as err:
        main_error = err.exceptions[0]
        if (
            isinstance(main_error, httpx.HTTPStatusError)
            and main_error.response.status_code == 405
        ) or isinstance(main_error, McpError):
            async with (
                sse_client(url=url, headers=headers) as streams,
                ClientSession(*streams) as session,
            ):
                await session.initialize()
                yield session
        else:
            raise main_error from err
```

---

## 9. Python Package Dependencies

### Required Packages

| Package | Version | Purpose |
|---------|---------|---------|
| `mcp` | >=1.26.0 | MCP Python SDK (client and server) |
| `claude-agent-sdk` | >=0.1.37 | Claude Agent SDK with built-in MCP support |
| `httpx` | >=0.27.0 | Async HTTP client (used by MCP SDK, also HA's preference) |
| `anyio` | >=4.0 | Async I/O abstraction (dependency of MCP SDK) |

### Already Available in HA

| Package | Notes |
|---------|-------|
| `httpx` | HA uses httpx for async HTTP; `create_async_httpx_client()` available |
| `voluptuous` | Schema validation; HA's standard |
| `aiohttp` | HA's web server framework |

### Installation

```
pip install mcp>=1.26.0 claude-agent-sdk>=0.1.37
```

Or in `manifest.json` for an HA custom component:

```json
{
    "requirements": [
        "mcp>=1.26.0",
        "claude-agent-sdk>=0.1.37"
    ]
}
```

### Compatibility Notes

- Both `mcp` and `claude-agent-sdk` require Python 3.10+
- HA currently runs Python 3.12 or 3.13, so compatibility is assured
- The `mcp` package uses `anyio` internally, which works with both asyncio and trio; HA uses asyncio
- The Claude Agent SDK bundles the Claude Code CLI; ensure the HA environment has Node.js available if using stdio MCP servers through the SDK

---

## Appendix A: Complete Working Example

### Connecting to HA's MCP Server and Listing Tools

```python
import asyncio
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

async def main():
    url = "http://homeassistant.local:8123/api/mcp"
    headers = {"Authorization": "Bearer YOUR_LONG_LIVED_ACCESS_TOKEN"}

    async with (
        streamable_http_client(url=url, headers=headers)
            as (read_stream, write_stream, _),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()

        # Discover what HA exposes
        tools_result = await session.list_tools()
        for tool in tools_result.tools:
            print(f"Tool: {tool.name}")
            print(f"  Description: {tool.description}")
            print(f"  Input Schema: {tool.inputSchema}")
            print()

        # Call a tool
        result = await session.call_tool(
            "HassTurnOn",
            {"entity_id": "light.living_room"}
        )
        print(f"Result: {result.content}")

asyncio.run(main())
```

### Claude Agent SDK with HA MCP Server

```python
import asyncio
from claude_agent_sdk import (
    ClaudeSDKClient,
    ClaudeAgentOptions,
    AssistantMessage,
    TextBlock,
    ResultMessage,
)

async def main():
    options = ClaudeAgentOptions(
        system_prompt="You are a helpful home assistant. Use the available tools to control the smart home.",
        mcp_servers={
            "home-assistant": {
                "type": "http",
                "url": "http://homeassistant.local:8123/api/mcp",
                "headers": {
                    "Authorization": "Bearer YOUR_LONG_LIVED_ACCESS_TOKEN"
                },
            }
        },
        allowed_tools=["mcp__home-assistant__*"],
    )

    async with ClaudeSDKClient(options=options) as client:
        await client.query("Turn off all the lights in the bedroom")

        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        print(f"Claude: {block.text}")
            if isinstance(message, ResultMessage):
                print(f"Done. Cost: ${message.total_cost_usd}")
                break

asyncio.run(main())
```

---

## Appendix B: References

- [MCP Transports Specification](https://modelcontextprotocol.io/docs/concepts/transports)
- [MCP Python SDK (GitHub)](https://github.com/modelcontextprotocol/python-sdk)
- [MCP Python SDK (PyPI)](https://pypi.org/project/mcp/)
- [Build an MCP Client Tutorial](https://modelcontextprotocol.io/docs/develop/build-client)
- [Claude Agent SDK (PyPI)](https://pypi.org/project/claude-agent-sdk/)
- [Claude Agent SDK MCP Documentation](https://platform.claude.com/docs/en/agent-sdk/mcp)
- [Claude Agent SDK Python Reference](https://platform.claude.com/docs/en/agent-sdk/python)
- [Claude Agent SDK GitHub](https://github.com/anthropics/claude-agent-sdk-python)
- [HA MCP Server Component](https://github.com/home-assistant/core/tree/dev/homeassistant/components/mcp_server)
- [HA MCP Client Component](https://github.com/home-assistant/core/tree/dev/homeassistant/components/mcp)
- [Cloudflare: Streamable HTTP MCP Servers](https://blog.cloudflare.com/streamable-http-mcp-servers-python/)
- [FastMCP Keep-Alive Discussion](https://github.com/punkpeye/fastmcp/issues/120)
